"""Orchestrates extract -> transform -> quality -> load for a date range.

Usage:
    python -m src.pipeline                                  # yesterday (UTC)
    python -m src.pipeline --start 2025-11-01 --end 2026-11-20   # backfill
    python -m src.pipeline --from-raw                       # re-run transform/load from saved raw JSON

Each step takes and returns small things (dates, file paths), so the same
functions can be called one per task from the Airflow DAG in dags/.
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src import anomalies, config, extract, load, quality, transform, weather

log = logging.getLogger("grid_pulse")


# ------------------------------------------------------------------ steps
def extract_step(start: date, end: date) -> dict[str, str]:
    paths = extract.extract_window(start, end)
    return {k: str(v) for k, v in paths.items()}


def raw_paths_for(start: date, end: date) -> dict[str, str]:
    """Locate previously saved raw files for this window (used by --from-raw)."""
    name = f"{start.isoformat()}_{end.isoformat()}.json"
    paths = {k: config.RAW_DIR / k / name for k in ("demand", "generation")}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"No raw files for this window: {missing}")
    return {k: str(v) for k, v in paths.items()}


def transform_step(raw_paths: dict[str, str], start: date, end: date) -> dict[str, str]:
    tag = f"{start.isoformat()}_{end.isoformat()}"
    d = transform.transform_demand(transform.load_raw(Path(raw_paths["demand"])))
    g = transform.transform_generation(transform.load_raw(Path(raw_paths["generation"])))
    clean_demand, bad = quality.split_out_of_range(d.df)
    if not bad.empty:
        log.warning("quarantined %d demand values: %s", len(bad), bad["reason"].value_counts().to_dict())
    d.df = clean_demand
    for name, res in (("demand", d), ("generation", g)):
        log.info("transform %s: %d raw rows -> %d clean rows (%d rejected, %d duplicates dropped)",
                 name, res.rows_in, len(res.df), res.rows_rejected, res.duplicates_dropped)
        for note in res.notes:
            log.warning("transform %s: %s", name, note)
    return {
        "demand": str(transform.stage(d.df, "demand", tag)),
        "generation": str(transform.stage(g.df, "generation", tag)),
        "quarantine": str(transform.stage(bad, "quarantine", tag)),
    }


def quality_step(staged: dict[str, str], start: date, end: date) -> None:
    demand = transform.read_staged(Path(staged["demand"]))
    generation = transform.read_staged(Path(staged["generation"]))
    quality.enforce(batch_results(staged, demand, generation, start, end))


def batch_results(staged: dict[str, str], demand, generation, start: date, end: date):
    """All batch checks, including how much had to be quarantined."""
    bad = transform.read_staged(Path(staged["quarantine"])) if staged.get("quarantine") else None
    n_bad = 0 if bad is None else len(bad)
    return quality.run_batch_checks(demand, generation, start, end) + \
        [quality.check_quarantine_share(n_bad, len(demand))]


def load_step(staged: dict[str, str], db_path: Path | None = None) -> int:
    conn = load.connect(db_path)
    try:
        load.init_db(conn)
        return load.load_batch(conn,
                               transform.read_staged(Path(staged["demand"])),
                               transform.read_staged(Path(staged["generation"])))
    finally:
        conn.close()


# ------------------------------------------------------------------ orchestration
def chunks(start: date, end: date, size: int = config.CHUNK_DAYS):
    """Split [start, end] into consecutive windows of at most `size` days."""
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=size - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def run_window(start: date, end: date, from_raw: bool = False, db_path: Path | None = None) -> int:
    """Run one window end to end and record it in pipeline_runs."""
    conn = load.connect(db_path)
    load.init_db(conn)
    run_id = load.start_run(conn, start, end)
    try:
        raw = raw_paths_for(start, end) if from_raw else extract_step(start, end)
        staged = transform_step(raw, start, end)
        demand = transform.read_staged(Path(staged["demand"]))
        results = batch_results(staged, demand, transform.read_staged(Path(staged["generation"])), start, end)
        load.record_checks(conn, run_id, results)   # history is kept even when the run fails
        load.record_quarantine(conn, run_id, transform.read_staged(Path(staged["quarantine"])))
        quality.enforce(results)                    # raises before anything is loaded
        rows = load_step(staged, db_path)
        load.finish_run(conn, run_id, "success", rows)
        log.info("window %s..%s: loaded %d rows", start, end, rows)
        return rows
    except Exception as exc:
        load.finish_run(conn, run_id, "failed", 0, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        conn.close()


def run(start: date, end: date, from_raw: bool = False, check_freshness: bool | None = None,
        db_path: Path | None = None) -> int:
    # A failed chunk is logged and skipped so one bad month doesn't block a year-long backfill;
    # the run still fails at the end (re-raising the first error) so the problem can't go unnoticed.
    total, errors = 0, []
    for s, e in chunks(start, end):
        try:
            total += run_window(s, e, from_raw, db_path)
        except Exception as exc:
            log.error("window %s..%s failed: %s", s, e, exc)
            errors.append(exc)
    conn = load.connect(db_path)
    try:
        weather.refresh(conn, start, end)   # enrichment: never fails the run
        anomalies.refresh(conn)
    finally:
        conn.close()
    if errors:
        log.error("%d of %d windows failed; see pipeline_runs.error_message", len(errors),
                  len(list(chunks(start, end))))
        raise errors[0]
    # Freshness only makes sense for a scheduled run that should reach (almost) today.
    today = datetime.now(timezone.utc).date()
    if check_freshness is None:
        check_freshness = (today - end).days <= 3
    if check_freshness:
        conn = load.connect(db_path)
        try:
            quality.enforce([quality.check_freshness(conn)])
        finally:
            conn.close()
    return total


def parse_args(argv=None) -> argparse.Namespace:
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    p = argparse.ArgumentParser(description="grid-pulse: EIA grid + battery data pipeline")
    p.add_argument("--start", type=date.fromisoformat, default=yesterday, help="first UTC date (YYYY-MM-DD)")
    p.add_argument("--end", type=date.fromisoformat, default=None, help="last UTC date (default: --start)")
    p.add_argument("--from-raw", action="store_true", help="skip the API; reuse saved raw JSON")
    p.add_argument("--db", type=Path, default=None, help="SQLite path (default: data/grid_pulse.db)")
    args = p.parse_args(argv)
    args.end = args.end or args.start
    if args.end < args.start:
        p.error("--end must be on or after --start")
    return args


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    rows = run(args.start, args.end, from_raw=args.from_raw, db_path=args.db)
    log.info("done: %d rows loaded for %s..%s", rows, args.start, args.end)


if __name__ == "__main__":
    main()
