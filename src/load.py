"""Load: write cleaned data into SQLite with idempotent upserts, and log pipeline runs.

Idempotent = running the same load twice leaves the database in the same state.
The composite primary key decides what "the same row" means, and
INSERT ... ON CONFLICT DO UPDATE replaces it instead of adding a duplicate.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from src import config


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(db_path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(config.SCHEMA_PATH.read_text())
    conn.executemany(
        """INSERT INTO regions (region_code, region_name, timezone) VALUES (?, ?, ?)
           ON CONFLICT(region_code) DO UPDATE SET
               region_name = excluded.region_name, timezone = excluded.timezone""",
        [(code, name, config.REGION_TZ[code]) for code, name in config.REGIONS.items()],
    )
    conn.commit()


def _records(df: pd.DataFrame, columns: list[str]) -> list[tuple]:
    """DataFrame -> list of tuples, with NaN turned into SQL NULL."""
    clean = df[columns].astype(object).where(df[columns].notna(), None)
    return list(clean.itertuples(index=False, name=None))


def upsert_demand(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    now = utc_now()
    rows = [r + (now,) for r in _records(df, ["region_code", "period_utc", "demand_mwh", "forecast_mwh"])]
    # COALESCE keeps an existing value if a re-pull comes back NULL for that field,
    # but a real new value (EIA revises data) always wins.
    conn.executemany(
        """INSERT INTO hourly_demand (region_code, period_utc, demand_mwh, forecast_mwh, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(region_code, period_utc) DO UPDATE SET
               demand_mwh   = COALESCE(excluded.demand_mwh, hourly_demand.demand_mwh),
               forecast_mwh = COALESCE(excluded.forecast_mwh, hourly_demand.forecast_mwh),
               updated_at   = excluded.updated_at""",
        rows,
    )
    return len(rows)


def upsert_generation(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    now = utc_now()
    rows = [r + (now,) for r in _records(df, ["region_code", "period_utc", "fuel_type", "generation_mwh"])]
    conn.executemany(
        """INSERT INTO hourly_generation (region_code, period_utc, fuel_type, generation_mwh, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(region_code, period_utc, fuel_type) DO UPDATE SET
               generation_mwh = COALESCE(excluded.generation_mwh, hourly_generation.generation_mwh),
               updated_at     = excluded.updated_at""",
        rows,
    )
    return len(rows)


def load_batch(conn: sqlite3.Connection, demand: pd.DataFrame, generation: pd.DataFrame) -> int:
    """Load both tables in ONE transaction: either everything lands or nothing does."""
    with conn:  # commits on success, rolls back on any exception
        return upsert_demand(conn, demand) + upsert_generation(conn, generation)


# ------------------------------------------------------------------ run log
def start_run(conn: sqlite3.Connection, window_start: date, window_end: date) -> int:
    with conn:
        cur = conn.execute(
            """INSERT INTO pipeline_runs (started_at, window_start, window_end, status)
               VALUES (?, ?, ?, 'running')""",
            (utc_now(), window_start.isoformat(), window_end.isoformat()),
        )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str,
               rows_loaded: int = 0, error: str | None = None) -> None:
    with conn:
        conn.execute(
            """UPDATE pipeline_runs SET finished_at = ?, status = ?, rows_loaded = ?, error_message = ?
               WHERE run_id = ?""",
            (utc_now(), status, rows_loaded, error, run_id),
        )
