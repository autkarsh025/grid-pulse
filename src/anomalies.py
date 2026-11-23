"""Anomaly detection on the loaded data: the same idea as finding failing hardware in fleet data.

Two detectors, both simple enough to explain on a whiteboard:

1. Spike/drop (robust z-score). Grid data has a strong daily shape, so a value is
   compared with the SAME local hour on the previous 28 days. "Expected" is the
   median of those days and "spread" is the MAD (median absolute deviation).
   Median and MAD are robust: one bad day doesn't distort the baseline the way
   a mean and standard deviation would.
       score = 0.6745 * (value - median) / MAD      ->  flag if |score| >= 4
   The baseline uses only PAST days (shift(1)), so a spike can't hide itself.

2. Flatline. A reading stuck at the exact same non-zero value for 8+ hours in a
   row usually means a frozen sensor or a stale feed, not real physics.

Results go into the `anomalies` table. It's a derived table, so it is fully
rebuilt each run (delete + insert in one transaction); simple and always
consistent with the latest (possibly revised) source data.
"""
from __future__ import annotations

import logging
import sqlite3

import pandas as pd

from src import config, load

log = logging.getLogger(__name__)

Z_THRESHOLD = 4.0
BASELINE_DAYS = 28
MIN_BASELINE_DAYS = 14
FLATLINE_HOURS = 8

# metric name -> SQL that returns region_code, period_utc, value
METRICS: dict[str, str] = {
    "demand": "SELECT region_code, period_utc, demand_mwh AS value FROM hourly_demand",
    "battery": f"SELECT region_code, period_utc, generation_mwh AS value FROM hourly_generation "
               f"WHERE fuel_type = '{config.BATTERY_CODE}'",
    "solar": f"SELECT region_code, period_utc, generation_mwh AS value FROM hourly_generation "
             f"WHERE fuel_type = '{config.SOLAR_CODE}'",
}
COLUMNS = ["region_code", "period_utc", "metric", "kind", "value", "expected", "score"]


def detect_spikes(series: pd.DataFrame, tz: str) -> pd.DataFrame:
    """series: columns period_utc, value for ONE region and metric."""
    df = series.dropna(subset=["value"]).copy()
    if df.empty:
        return pd.DataFrame(columns=["period_utc", "kind", "value", "expected", "score"])
    ts = pd.to_datetime(df["period_utc"], utc=True)
    df["local_hour"] = ts.dt.tz_convert(tz).dt.hour.values
    df = df.assign(_ts=ts.values).sort_values("_ts")

    def baseline(g: pd.DataFrame) -> pd.DataFrame:
        past = g["value"].shift(1)  # only previous days
        roll = past.rolling(BASELINE_DAYS, min_periods=MIN_BASELINE_DAYS)
        med = roll.median()
        mad = (past - med).abs().rolling(BASELINE_DAYS, min_periods=MIN_BASELINE_DAYS).median()
        return g.assign(expected=med, mad=mad)

    df = pd.concat([baseline(g) for _, g in df.groupby("local_hour")])
    # Floor the spread so tiny absolute wiggles don't score as huge outliers. Without this,
    # solar at night (expected 0, MAD 0) turned a 28 MWh reading into "z = 19". The floor is
    # the larger of 5% of the expected value and 2% of the series' typical peak (99th percentile).
    scale = float(df["value"].abs().quantile(0.99)) or 1.0
    floor = (df["expected"].abs() * 0.05).clip(lower=max(0.02 * scale, 1.0))
    spread = df["mad"].where(df["mad"] > floor, floor)
    df["score"] = 0.6745 * (df["value"] - df["expected"]) / spread
    hits = df[df["score"].abs() >= Z_THRESHOLD].copy()
    hits["kind"] = hits["score"].map(lambda s: "spike" if s > 0 else "drop")
    return hits[["period_utc", "kind", "value", "expected", "score"]]


def detect_flatlines(series: pd.DataFrame, min_hours: int = FLATLINE_HOURS) -> pd.DataFrame:
    df = series.dropna(subset=["value"]).sort_values("period_utc").reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=["period_utc", "kind", "value", "expected", "score"])
    ts = pd.to_datetime(df["period_utc"])
    # A new run starts when the value changes OR an hour is missing.
    new_run = (df["value"] != df["value"].shift()) | (ts.diff() != pd.Timedelta(hours=1))
    run_id = new_run.cumsum()
    run_len = df.groupby(run_id)["value"].transform("size")
    stuck = df[(run_len >= min_hours) & (df["value"] != 0)]
    # Report one row per stuck run (its first hour); score = run length in hours.
    first = stuck.groupby(run_id[stuck.index]).head(1)
    return pd.DataFrame({
        "period_utc": first["period_utc"], "kind": "flatline", "value": first["value"],
        "expected": None, "score": run_len[first.index].astype(float),
    })


def detect_all(conn: sqlite3.Connection) -> pd.DataFrame:
    found = []
    for metric, sql in METRICS.items():
        data = pd.read_sql_query(sql, conn)
        for region, g in data.groupby("region_code"):
            tz = config.REGION_TZ.get(region, "UTC")
            for hits in (detect_spikes(g, tz), detect_flatlines(g)):
                if not hits.empty:
                    found.append(hits.assign(region_code=region, metric=metric))
    if not found:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(found, ignore_index=True)[COLUMNS]


def refresh(conn: sqlite3.Connection) -> int:
    """Rebuild the anomalies table from the current data. Returns the number of anomalies."""
    load.init_db(conn)
    found = detect_all(conn)
    now = load.utc_now()
    rows = [tuple(None if pd.isna(v) else v for v in r) + (now,)
            for r in found.itertuples(index=False, name=None)]
    with conn:
        conn.execute("DELETE FROM anomalies")
        conn.executemany(
            """INSERT INTO anomalies (region_code, period_utc, metric, kind, value, expected, score, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    log.info("anomaly detection: %d anomalies flagged", len(rows))
    return len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = load.connect()
    try:
        refresh(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
