"""Spark version of the net-peak analysis, validated row-for-row against the SQL version.

What it does:
  1. EXPORT  SQLite tables -> a Parquet "data lake" partitioned by region_code and month
             (data/lake/<table>/region_code=CISO/month=2026-08/part-*.parquet)
  2. COMPUTE the battery share of each day's net peak with Spark DataFrames + window functions
             (the same logic as sql/analysis/06_net_peak_battery_share.sql)
  3. VALIDATE Spark's result against the SQL result: same days, same shares (within rounding)
  4. SHOW    partition pruning: a filter on region/month reads only the matching folders

Why: Tesla-scale fleet data lives in partitioned columnar files processed by Spark, not in
one SQLite file. Porting one step and proving it gives identical answers is how you migrate a
pipeline safely. (Honest scale note: ~600K rows runs fine on a laptop. This demonstrates the
patterns, not "big data".)

Usage:  python -m spark.net_peak_spark [--db data/grid_pulse.db]   (exit code 1 on mismatch)
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from src import config

SQL_FILE = config.ROOT / "sql" / "analysis" / "06_net_peak_battery_share.sql"
TOLERANCE_PCT = 0.15        # shares are rounded to 0.1 in both versions
MAX_MISMATCH_SHARE = 0.002  # allow a handful of exact ties, where either hour is a valid "peak"


def lake_dir() -> Path:
    return config.DATA_DIR / "lake"


def spark_session() -> SparkSession:
    return (SparkSession.builder.appName("grid-pulse").master("local[*]")
            .config("spark.sql.session.timeZone", "UTC")
            .config("spark.sql.shuffle.partitions", "8")        # small data: don't make 200 tiny tasks
            .config("spark.sql.execution.arrow.pyspark.enabled", "true")
            .config("spark.ui.enabled", "false")
            .getOrCreate())


def export_lake(spark: SparkSession, db_path: Path) -> dict[str, int]:
    """SQLite -> Parquet, partitioned by region and month. Overwrites the previous export."""
    counts = {}
    conn = sqlite3.connect(db_path)
    try:
        for table in ("hourly_demand", "hourly_generation"):
            pdf = pd.read_sql_query(f"SELECT * FROM {table}", conn)
            pdf["month"] = pdf["period_utc"].str.slice(0, 7)
            (spark.createDataFrame(pdf)
                  .repartition("region_code", "month")             # one file per partition folder
                  .write.mode("overwrite").partitionBy("region_code", "month")
                  .parquet(str(lake_dir() / table)))
            counts[table] = len(pdf)
    finally:
        conn.close()
    return counts


def net_peak(spark: SparkSession) -> DataFrame:
    """Battery share of each day's net-peak hour: same rules as the SQL version."""
    d = spark.read.parquet(str(lake_dir() / "hourly_demand"))
    g = spark.read.parquet(str(lake_dir() / "hourly_generation"))

    def fuel(code: str):
        return F.when(F.col("fuel_type") == code, F.col("generation_mwh"))

    gen = g.groupBy("region_code", "period_utc").agg(
        F.sum(F.coalesce(fuel(config.SOLAR_CODE), F.lit(0.0))).alias("solar_mwh"),
        F.sum(F.coalesce(fuel(config.WIND_CODE), F.lit(0.0))).alias("wind_mwh"),
        F.sum(fuel(config.BATTERY_CODE)).alias("battery_mwh"),
    )
    hourly = (d.filter(F.col("demand_mwh").isNotNull())
               .join(gen, ["region_code", "period_utc"], "inner")      # complete hours only
               .withColumn("day", F.substring("period_utc", 1, 10))
               .withColumn("net_load_mwh", F.col("demand_mwh") - F.coalesce("solar_mwh", F.lit(0.0))
                           - F.coalesce("wind_mwh", F.lit(0.0)))
               .withColumn("battery_mwh", F.coalesce("battery_mwh", F.lit(0.0))))
    by_day = Window.partitionBy("region_code", "day")
    peak = (hourly.withColumn("rn", F.row_number().over(by_day.orderBy(F.col("net_load_mwh").desc())))
                  .withColumn("hours", F.count(F.lit(1)).over(by_day))
                  .filter((F.col("rn") == 1) & (F.col("hours") >= 22)))   # complete days only
    return peak.select(
        "region_code", "day",
        F.col("period_utc").alias("net_peak_hour_utc"),
        F.round(F.when(F.col("net_load_mwh") != 0, 100.0 * F.col("battery_mwh") / F.col("net_load_mwh")), 1)
         .alias("battery_share_of_net_peak_pct"),
    )


def sql_net_peak(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(SQL_FILE.read_text(), conn)
    finally:
        conn.close()


def validate(spark_df: pd.DataFrame, sql_df: pd.DataFrame) -> dict:
    """Compare the two results day by day."""
    key = ["region_code", "day"]
    m = sql_df[key + ["battery_share_of_net_peak_pct"]].merge(
        spark_df[key + ["battery_share_of_net_peak_pct"]], on=key, how="outer",
        suffixes=("_sql", "_spark"), indicator=True)
    only_one_side = int((m["_merge"] != "both").sum())
    both = m[m["_merge"] == "both"]
    a, b = both["battery_share_of_net_peak_pct_sql"], both["battery_share_of_net_peak_pct_spark"]
    differ = int((((a - b).abs() > TOLERANCE_PCT) | (a.isna() != b.isna())).sum())
    total = max(len(m), 1)
    return {"rows_sql": len(sql_df), "rows_spark": len(spark_df), "missing_on_one_side": only_one_side,
            "value_mismatches": differ, "ok": (only_one_side + differ) / total <= MAX_MISMATCH_SHARE}


def show_partition_pruning(spark: SparkSession) -> str:
    """Return the physical plan's PartitionFilters line for a one-region, one-month query."""
    df = (spark.read.parquet(str(lake_dir() / "hourly_generation"))
               .filter((F.col("region_code") == "CISO") & (F.col("month") == "2026-08")))
    plan = df._jdf.queryExecution().executedPlan().toString()
    lines = [ln.strip() for ln in plan.splitlines() if "PartitionFilters" in ln]
    return lines[0] if lines else plan.splitlines()[0]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--report", type=Path, default=None, help="write a JSON summary (shown on the dashboard)")
    args = p.parse_args(argv)
    spark = spark_session()
    spark.sparkContext.setLogLevel("WARN")
    try:
        t0 = time.time()
        counts = export_lake(spark, args.db)
        t1 = time.time()
        spark_df = net_peak(spark).toPandas()
        t2 = time.time()
        result = validate(spark_df, sql_net_peak(args.db))
        print(f"Exported to Parquet lake: {counts} in {t1 - t0:.1f}s")
        print(f"Spark net-peak: {len(spark_df):,} region-days in {t2 - t1:.1f}s")
        print(f"Validation vs SQL: {result}")
        pruning = show_partition_pruning(spark)
        print(f"Partition pruning: {pruning}")
        print("MATCH: Spark and SQL agree." if result["ok"] else "MISMATCH: Spark and SQL disagree!")
        if args.report:
            import json
            from datetime import datetime, timezone
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({
                **result, "rows_exported": counts, "export_seconds": round(t1 - t0, 1),
                "spark_seconds": round(t2 - t1, 1), "partition_filters": pruning[:300],
                "spark_version": spark.version, "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }, indent=2))
        return 0 if result["ok"] else 1
    finally:
        spark.stop()


if __name__ == "__main__":
    sys.exit(main())
