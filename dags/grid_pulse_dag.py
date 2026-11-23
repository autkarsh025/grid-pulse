"""Airflow DAG: runs the grid-pulse pipeline once a day, one task per stage.

extract >> transform >> quality >> load >> detect_anomalies

Tasks pass small values (file paths) through XCom, never DataFrames: XCom lives in
Airflow's metadata database and isn't meant for bulk data. The real data moves
through files in data/raw and data/staging.

Each run processes the data interval Airflow assigns it (one UTC day), so a
backfill is just: airflow dags backfill grid_pulse -s 2026-01-01 -e 2026-11-20
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Make the repo's src/ importable when the repo is mounted into Airflow.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # Airflow 3
    from airflow.sdk import dag, get_current_context, task
except ImportError:  # Airflow 2.x
    from airflow.decorators import dag, task
    from airflow.operators.python import get_current_context

from src import pipeline


def _window() -> tuple[date, date]:
    """The UTC day this run is responsible for."""
    ctx = get_current_context()
    day = ctx["data_interval_start"].date()
    return day, day


@dag(
    dag_id="grid_pulse",
    schedule="0 14 * * *",          # 14:00 UTC daily, after EIA has published the previous day
    start_date=datetime(2026, 11, 1),
    catchup=False,                  # use `airflow dags backfill` for history instead
    max_active_runs=1,              # SQLite allows one writer at a time
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["energy", "eia"],
)
def grid_pulse():
    @task
    def extract() -> dict[str, str]:
        start, end = _window()
        return pipeline.extract_step(start, end)

    @task
    def transform(raw_paths: dict[str, str]) -> dict[str, str]:
        start, end = _window()
        return pipeline.transform_step(raw_paths, start, end)

    @task
    def quality(staged: dict[str, str]) -> dict[str, str]:
        start, end = _window()
        pipeline.quality_step(staged, start, end)  # raises -> task fails -> load never runs
        return staged

    @task
    def load(staged: dict[str, str]) -> int:
        return pipeline.load_step(staged)

    @task
    def detect_anomalies(_rows_loaded: int) -> int:
        conn = pipeline.load.connect()
        try:
            return pipeline.anomalies.refresh(conn)
        finally:
            conn.close()

    detect_anomalies(load(quality(transform(extract()))))


grid_pulse()
