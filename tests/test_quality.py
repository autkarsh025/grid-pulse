from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from fakes import fake_demand_records, fake_generation_records
from src import load, quality, transform

DAY = date(2026, 11, 10)


@pytest.fixture
def batch():
    d = transform.transform_demand(fake_demand_records(DAY, DAY)).df
    g = transform.transform_generation(fake_generation_records(DAY, DAY)).df
    return d, g


def test_clean_batch_passes(batch):
    d, g = batch
    results = quality.run_batch_checks(d, g, DAY, DAY)
    assert all(r.passed for r in results), [str(r) for r in results if not r.passed]
    quality.enforce(results)  # must not raise


def test_missing_hours_fail_completeness(batch):
    d, g = batch
    d = d.iloc[10:]  # drop 10 hours of CISO
    result = quality.check_completeness(d, DAY, DAY)
    assert not result.passed
    assert "CISO" in result.detail


def test_too_many_nulls_fail(batch):
    d, _ = batch
    d = d.copy()
    d.loc[d.index[:5], "demand_mwh"] = np.nan  # 5/48 > 2%
    assert not quality.check_null_share(d, "demand_mwh", "demand").passed


def test_out_of_range_demand_fails(batch):
    d, _ = batch
    d = d.copy()
    d.loc[d.index[0], "demand_mwh"] = -5
    assert not quality.check_demand_range(d).passed


def test_duplicate_keys_fail(batch):
    d, _ = batch
    doubled = pd.concat([d, d.iloc[[0]]])
    assert not quality.check_unique_keys(doubled, ["region_code", "period_utc"], "demand").passed


def test_enforce_raises_on_error_but_not_on_warning(batch):
    d, g = batch
    warn = quality.CheckResult("x", False, "just a warning", severity="warn")
    quality.enforce([warn])  # warnings don't stop the run
    with pytest.raises(quality.DataQualityError):
        quality.enforce([quality.check_completeness(d.iloc[30:], DAY, DAY)])


def test_solar_at_night_warns(batch):
    _, g = batch
    g = g.copy()
    g.loc[(g["fuel_type"] == "SUN"), "generation_mwh"] = 5_000  # flat solar, incl. night
    result = quality.check_solar_at_night(g)
    assert not result.passed and result.severity == "warn"


def test_freshness(batch, isolated_data_dir):
    d, g = batch
    conn = load.connect()
    load.init_db(conn)
    load.load_batch(conn, d, g)
    fresh_now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    stale_now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    assert quality.check_freshness(conn, now=fresh_now).passed
    assert not quality.check_freshness(conn, now=stale_now).passed
    conn.close()
