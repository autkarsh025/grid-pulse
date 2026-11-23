"""Spark job must agree with the SQL version. Skipped where PySpark isn't installed (CI installs it)."""
from datetime import date

import pytest

pyspark = pytest.importorskip("pyspark")

from fakes import fake_demand_records, fake_generation_records  # noqa: E402
from src import config, extract, pipeline  # noqa: E402


def test_spark_matches_sql(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "x")
    monkeypatch.setattr(extract, "fetch_hourly", lambda route, facets, s, e, k, session=None:
                        fake_demand_records(s, e) if route == config.REGION_ROUTE else fake_generation_records(s, e))
    pipeline.run(date(2026, 8, 1), date(2026, 8, 20), check_freshness=False)

    from spark import net_peak_spark as job
    spark = job.spark_session()
    try:
        counts = job.export_lake(spark, config.DB_PATH)
        assert counts["hourly_demand"] == 20 * 24 * 2
        assert (config.DATA_DIR / "lake" / "hourly_generation" / "region_code=CISO").is_dir()  # partitioned
        result = job.validate(job.net_peak(spark).toPandas(), job.sql_net_peak(config.DB_PATH))
        assert result["ok"], result
        assert result["rows_spark"] == result["rows_sql"] > 0
        assert "PartitionFilters" in job.show_partition_pruning(spark)
    finally:
        spark.stop()
