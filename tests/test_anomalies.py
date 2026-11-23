from datetime import date

import pandas as pd

from fakes import fake_demand_records, fake_generation_records
from src import anomalies, load, transform

START, END = date(2026, 8, 1), date(2026, 9, 30)


def _loaded_conn(demand_records, gen_records):
    conn = load.connect()
    load.init_db(conn)
    load.load_batch(conn,
                    transform.transform_demand(demand_records).df,
                    transform.transform_generation(gen_records).df)
    return conn


def test_regular_data_has_no_spikes():
    conn = _loaded_conn(fake_demand_records(START, END), fake_generation_records(START, END))
    found = anomalies.detect_all(conn)
    assert found[found["kind"].isin(["spike", "drop"])].empty


def test_injected_demand_spike_is_caught():
    demand = fake_demand_records(START, END)
    target = next(r for r in demand if r["respondent"] == "CISO" and r["type"] == "D"
                  and r["period"] == "2026-09-20T02")
    target["value"] = str(float(target["value"]) * 1.6)  # +60%: a real-world outlier
    conn = _loaded_conn(demand, fake_generation_records(START, END))
    found = anomalies.detect_all(conn)
    hit = found[(found["metric"] == "demand") & (found["region_code"] == "CISO")]
    assert list(hit["period_utc"]) == ["2026-09-20 02:00:00"]
    assert hit["kind"].iloc[0] == "spike"


def test_stuck_battery_reading_is_flagged_as_flatline():
    series = pd.DataFrame({
        "period_utc": pd.date_range("2026-09-01", periods=24, freq="h").strftime("%Y-%m-%d %H:%M:%S"),
        "value": [100.0] * 4 + [777.0] * 10 + list(range(10)),
    })
    flat = anomalies.detect_flatlines(series)
    assert len(flat) == 1
    assert flat["value"].iloc[0] == 777.0 and flat["score"].iloc[0] == 10


def test_zero_runs_are_not_flatlines():
    series = pd.DataFrame({
        "period_utc": pd.date_range("2026-09-01", periods=12, freq="h").strftime("%Y-%m-%d %H:%M:%S"),
        "value": [0.0] * 12,  # batteries idle overnight: normal
    })
    assert anomalies.detect_flatlines(series).empty


def test_refresh_is_repeatable():
    conn = _loaded_conn(fake_demand_records(START, END), fake_generation_records(START, END))
    first = anomalies.refresh(conn)
    second = anomalies.refresh(conn)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0] == second
