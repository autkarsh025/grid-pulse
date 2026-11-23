"""Query optimization: time every analysis query and print SQLite's execution plan for it.

Usage:  python -m analysis.explain_queries [--db data/grid_pulse.db]

How to read a plan:
  SCAN <table>                    reads every row (fine for whole-table aggregates, slow for lookups)
  SEARCH <table> USING INDEX ...  jumps straight to matching rows via an index
  USE TEMP B-TREE FOR ORDER BY    sorts in a temporary structure (window functions, ORDER BY)

Talking point: hourly_generation has an index on (fuel_type, period_utc) because most
analyses ask "all battery rows" or "all solar rows"; the plans show those queries SEARCH that
index instead of scanning ~500K rows. Primary keys (region_code, period_utc[, fuel_type])
double as indexes for the joins between demand and generation.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from src import config

SQL_DIR = config.ROOT / "sql" / "analysis"


def explain(conn: sqlite3.Connection, sql: str) -> list[str]:
    return [row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]


def time_query(conn: sqlite3.Connection, sql: str, repeats: int = 3) -> float:
    best = float("inf")
    for _ in range(repeats):
        t = time.perf_counter()
        conn.execute(sql).fetchall()
        best = min(best, time.perf_counter() - t)
    return best


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    args = p.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        for f in sorted(SQL_DIR.glob("*.sql")):
            sql = f.read_text()
            secs = time_query(conn, sql)
            plan = explain(conn, sql)
            uses_index = any("USING INDEX" in s or "USING COVERING INDEX" in s or "PRIMARY KEY" in s for s in plan)
            print(f"\n=== {f.name}: {secs * 1000:,.1f} ms (best of 3) · {'uses an index' if uses_index else 'full scans only'}")
            for step in plan:
                print("   ", step)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
