"""End-to-end test with the API mocked out: extract -> transform -> quality -> load."""
from datetime import date

import pytest

from fakes import fake_demand_records, fake_generation_records
from src import config, extract, load, pipeline, quality


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "test-key")

    def fake_fetch(route, facets, start, end, api_key, session=None):
        if route == config.REGION_ROUTE:
            return fake_demand_records(start, end)
        return fake_generation_records(start, end)

    monkeypatch.setattr(extract, "fetch_hourly", fake_fetch)


def test_chunks_cover_range_without_gaps():
    windows = list(pipeline.chunks(date(2026, 1, 1), date(2026, 3, 15), size=30))
    assert windows[0][0] == date(2026, 1, 1) and windows[-1][1] == date(2026, 3, 15)
    for (_, a_end), (b_start, _) in zip(windows, windows[1:]):
        assert (b_start - a_end).days == 1


def test_backfill_end_to_end(fake_api):
    rows = pipeline.run(date(2026, 10, 1), date(2026, 10, 7), check_freshness=False)
    conn = load.connect()
    assert conn.execute("SELECT COUNT(*) FROM hourly_demand").fetchone()[0] == 7 * 24 * 2
    status = conn.execute("SELECT status FROM pipeline_runs").fetchall()
    assert status == [("success",)]
    assert rows == 7 * 24 * 2 * (1 + 3)


def test_quality_failure_blocks_load_and_is_logged(fake_api, monkeypatch):
    def broken_fetch(route, facets, start, end, api_key, session=None):
        recs = fake_demand_records(start, end) if route == config.REGION_ROUTE \
            else fake_generation_records(start, end)
        return recs[: len(recs) // 2]  # half the hours are missing

    monkeypatch.setattr(extract, "fetch_hourly", broken_fetch)
    with pytest.raises(quality.DataQualityError):
        pipeline.run(date(2026, 10, 1), date(2026, 10, 1), check_freshness=False)
    conn = load.connect()
    assert conn.execute("SELECT COUNT(*) FROM hourly_demand").fetchone()[0] == 0
    status, err = conn.execute("SELECT status, error_message FROM pipeline_runs").fetchone()
    assert status == "failed" and "completeness" in err


def test_from_raw_replays_without_api(fake_api, monkeypatch):
    day = date(2026, 10, 2)
    pipeline.run(day, day, check_freshness=False)

    def no_network(*a, **k):
        raise AssertionError("API should not be called with --from-raw")

    monkeypatch.setattr(extract, "fetch_hourly", no_network)
    pipeline.run(day, day, from_raw=True, check_freshness=False)  # idempotent replay
    conn = load.connect()
    assert conn.execute("SELECT COUNT(*) FROM hourly_demand").fetchone()[0] == 48


def test_analysis_queries_run(fake_api):
    """Every SQL file in sql/analysis should execute against a loaded database."""
    pipeline.run(date(2026, 9, 1), date(2026, 10, 31), check_freshness=False)
    conn = load.connect()
    for sql_file in sorted((config.ROOT / "sql" / "analysis").glob("*.sql")):
        rows = conn.execute(sql_file.read_text()).fetchall()
        assert rows, f"{sql_file.name} returned no rows"


def test_one_bad_chunk_does_not_block_the_rest_of_a_backfill(fake_api, monkeypatch):
    def flaky_fetch(route, facets, start, end, api_key, session=None):
        recs = fake_demand_records(start, end) if route == config.REGION_ROUTE \
            else fake_generation_records(start, end)
        return recs[: len(recs) // 2] if start.month == 9 else recs  # September is broken

    monkeypatch.setattr(extract, "fetch_hourly", flaky_fetch)
    monkeypatch.setattr(config, "CHUNK_DAYS", 30)
    with pytest.raises(quality.DataQualityError):
        pipeline.run(date(2026, 9, 1), date(2026, 10, 30), check_freshness=False)
    conn = load.connect()
    statuses = [s for (s,) in conn.execute("SELECT status FROM pipeline_runs ORDER BY run_id")]
    assert statuses == ["failed", "success"]
    assert conn.execute("SELECT COUNT(*) FROM hourly_demand").fetchone()[0] == 30 * 24 * 2


def test_impossible_values_are_quarantined_not_loaded(fake_api, monkeypatch):
    def spiky(route, facets, start, end, api_key, session=None):
        recs = fake_demand_records(start, end) if route == config.REGION_ROUTE else fake_generation_records(start, end)
        if route == config.REGION_ROUTE:
            demand_rows = [r for r in recs if r["type"] == "D" and r["respondent"] == "CISO"]
            demand_rows[5]["value"] = "999999"   # impossible spike
            demand_rows[9]["value"] = "-3"       # impossible negative
        return recs

    monkeypatch.setattr(extract, "fetch_hourly", spiky)
    pipeline.run(date(2026, 10, 1), date(2026, 10, 30), check_freshness=False)   # must NOT fail (2 of 1,440 rows bad)
    conn = load.connect()
    q = conn.execute("SELECT field, value, reason FROM quarantine ORDER BY value").fetchall()
    assert [v for _, v, _ in q] == [-3.0, 999999.0]
    assert conn.execute("SELECT MAX(demand_mwh) FROM hourly_demand").fetchone()[0] < 100_000  # spike not loaded
    assert conn.execute("SELECT status FROM pipeline_runs").fetchone()[0] == "success"


def test_too_much_quarantine_fails_the_batch():
    assert not quality.check_quarantine_share(30, 1000).passed   # 3% > 1% limit
    assert quality.check_quarantine_share(6, 1000).passed


def test_net_peak_ignores_hours_where_generation_has_not_arrived(fake_api):
    """Generation lags demand: the newest hours have demand but no generation yet."""
    pipeline.run(date(2026, 10, 1), date(2026, 10, 10), check_freshness=False)
    conn = load.connect()
    before = conn.execute((config.ROOT / "sql/analysis/06_net_peak_battery_share.sql").read_text()).fetchall()
    # Simulate lag: delete the last 6 hours of generation for CISO.
    conn.execute("""DELETE FROM hourly_generation WHERE region_code='CISO' AND period_utc >=
                    (SELECT datetime(MAX(period_utc), '-5 hours') FROM hourly_generation WHERE region_code='CISO')""")
    conn.commit()
    after = conn.execute((config.ROOT / "sql/analysis/06_net_peak_battery_share.sql").read_text()).fetchall()
    ciso_after = [r for r in after if r[0] == "CISO"]
    # The incomplete last day is dropped instead of reporting a fake ~0% battery share.
    assert ciso_after[-1][1] < [r for r in before if r[0] == "CISO"][-1][1]
