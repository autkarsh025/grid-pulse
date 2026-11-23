from datetime import date

from fakes import fake_demand_records, fake_generation_records
from src import load, transform

DAY = date(2026, 11, 10)


def _batch():
    d = transform.transform_demand(fake_demand_records(DAY, DAY)).df
    g = transform.transform_generation(fake_generation_records(DAY, DAY)).df
    return d, g


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_loading_twice_is_idempotent():
    conn = load.connect()
    load.init_db(conn)
    d, g = _batch()
    load.load_batch(conn, d, g)
    first = (_count(conn, "hourly_demand"), _count(conn, "hourly_generation"))
    load.load_batch(conn, d, g)  # rerun the same day
    second = (_count(conn, "hourly_demand"), _count(conn, "hourly_generation"))
    assert first == second == (48, 48 * 3)


def test_revised_values_overwrite_old_ones():
    conn = load.connect()
    load.init_db(conn)
    d, g = _batch()
    load.load_batch(conn, d, g)
    d2 = d.copy()
    d2.loc[0, "demand_mwh"] = 12345.0  # EIA revised this hour
    load.load_batch(conn, d2, g)
    val = conn.execute(
        "SELECT demand_mwh FROM hourly_demand WHERE region_code=? AND period_utc=?",
        (d2.loc[0, "region_code"], d2.loc[0, "period_utc"]),
    ).fetchone()[0]
    assert val == 12345.0


def test_null_in_rerun_does_not_erase_existing_value():
    conn = load.connect()
    load.init_db(conn)
    d, g = _batch()
    load.load_batch(conn, d, g)
    d2 = d.copy()
    d2.loc[0, "forecast_mwh"] = None
    load.load_batch(conn, d2, g)
    val = conn.execute(
        "SELECT forecast_mwh FROM hourly_demand WHERE region_code=? AND period_utc=?",
        (d.loc[0, "region_code"], d.loc[0, "period_utc"]),
    ).fetchone()[0]
    assert val == d.loc[0, "forecast_mwh"]


def test_failed_batch_rolls_back():
    conn = load.connect()
    load.init_db(conn)
    d, g = _batch()
    g = g.copy()
    g["region_code"] = "XXXX"  # unknown region -> foreign key error mid-transaction
    try:
        load.load_batch(conn, d, g)
    except Exception:
        pass
    assert _count(conn, "hourly_demand") == 0, "demand rows must roll back with the failed generation load"
