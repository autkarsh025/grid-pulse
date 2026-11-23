from datetime import date

import pytest

from fakes import fake_demand_records, fake_generation_records
from src import battery, config, extract, load, pipeline, transform

START, END = date(2026, 8, 1), date(2026, 8, 31)


@pytest.fixture
def conn():
    c = load.connect()
    load.init_db(c)
    load.load_batch(c, transform.transform_demand(fake_demand_records(START, END)).df,
                    transform.transform_generation(fake_generation_records(START, END)).df)
    return c


def test_round_trip_sql(conn):
    rows = conn.execute((config.ROOT / "sql/analysis/07_battery_round_trip.sql").read_text()).fetchall()
    ciso = [r for r in rows if r[0] == "CISO"][0]
    # Fake fleet: charges 2,000 MWh x 6 h, discharges 3,000 MWh x 5 h per day -> 15,000 / 12,000 = 125%
    assert ciso[4] == pytest.approx(125.0)


def test_full_power_hours(conn):
    fph = battery.full_power_hours(battery.battery_hourly(conn), "CISO").dropna()
    # Daily discharge 15,000 MWh at a 3,000 MW peak = 5 full-power hours.
    assert fph["full_power_hours"].median() == pytest.approx(5.0)


def test_heatmap_shape(conn):
    hm = battery.dispatch_heatmap(battery.battery_hourly(conn), "CISO")
    assert len(hm["hours"]) == 24 and all(len(row) == 24 for row in hm["z"])
    assert len(hm["weeks"]) >= 4


def test_records(conn):
    recs = {r["label"]: r for r in battery.records(conn, "CISO")}
    assert recs["Biggest battery discharge hour"]["value"] == 3000
    assert recs["Biggest battery charging hour"]["value"] == 2000


def test_quality_results_are_recorded_even_on_failure(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "x")

    def half(route, facets, start, end, api_key, session=None):
        recs = fake_demand_records(start, end) if route == config.REGION_ROUTE else fake_generation_records(start, end)
        return recs[: len(recs) // 2]

    monkeypatch.setattr(extract, "fetch_hourly", half)
    with pytest.raises(Exception):
        pipeline.run(START, START, check_freshness=False)
    c = load.connect()
    rows = dict(c.execute("SELECT check_name, passed FROM quality_results").fetchall())
    assert rows["demand_completeness"] == 0 and rows["demand_unique_keys"] == 1
