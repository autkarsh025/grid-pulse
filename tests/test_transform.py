from datetime import date

import pandas as pd
import pytest

from fakes import fake_demand_records, fake_generation_records
from src import transform

DAY = date(2026, 11, 1)


def test_demand_pivots_to_one_row_per_region_hour():
    res = transform.transform_demand(fake_demand_records(DAY, DAY))
    assert list(res.df.columns) == transform.DEMAND_COLUMNS
    assert len(res.df) == 2 * 24  # 2 regions x 24 hours
    assert res.df["demand_mwh"].dtype.kind == "f"  # strings were cast to numbers


def test_timestamps_are_utc_strings():
    res = transform.transform_demand(fake_demand_records(DAY, DAY))
    assert res.df["period_utc"].iloc[0] == "2026-11-01 00:00:00"


def test_dst_day_still_has_24_utc_hours():
    # Nov 1, 2026 is the US fall-back day: 25 local hours, but exactly 24 UTC hours.
    res = transform.transform_demand(fake_demand_records(DAY, DAY, regions=("CISO",)))
    assert res.df["period_utc"].nunique() == 24


def test_bad_rows_are_rejected_and_counted():
    records = fake_demand_records(DAY, DAY)
    records[0]["period"] = "not-a-date"
    res = transform.transform_demand(records)
    assert res.rows_rejected == 1
    assert any("rejected" in n for n in res.notes)


def test_non_numeric_value_becomes_null_not_dropped():
    records = fake_demand_records(DAY, DAY, regions=("CISO",))
    records[0]["value"] = "n/a"
    res = transform.transform_demand(records)
    assert res.df["demand_mwh"].isna().sum() == 1  # quality.py decides if this is acceptable


def test_duplicates_are_dropped_keeping_last():
    records = fake_demand_records(DAY, DAY, regions=("CISO",))
    dup = dict(records[0], value="99999")
    res = transform.transform_demand(records + [dup])
    assert res.duplicates_dropped == 1
    first = res.df[res.df["period_utc"] == "2026-11-01 00:00:00"]
    assert first["demand_mwh"].iloc[0] == 99999


def test_battery_negatives_are_preserved():
    res = transform.transform_generation(fake_generation_records(DAY, DAY))
    bat = res.df[res.df["fuel_type"] == "BAT"]["generation_mwh"]
    assert (bat < 0).any(), "charging (negative) values must survive the transform"


def test_missing_column_raises():
    records = [{"period": "2026-11-01T00", "respondent": "CISO", "value": "1"}]  # no 'type'
    with pytest.raises(ValueError, match="missing columns"):
        transform.transform_demand(records)


def test_empty_input_returns_empty_frame():
    res = transform.transform_generation([])
    assert res.df.empty
    assert isinstance(res.df, pd.DataFrame)
