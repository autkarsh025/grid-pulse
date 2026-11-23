from datetime import date

import pandas as pd
import pytest

from fakes import fake_demand_records, fake_generation_records
from src import carbon, load, transform

DAY = date(2026, 9, 1)


def _conn():
    conn = load.connect()
    load.init_db(conn)
    load.load_batch(conn,
                    transform.transform_demand(fake_demand_records(DAY, DAY)).df,
                    transform.transform_generation(fake_generation_records(DAY, DAY)).df)
    return conn


def test_intensity_matches_hand_calculation():
    hourly = carbon.hourly_intensity(_conn())
    # At night the fake grid is gas only (12,000 MWh), so intensity = the gas factor.
    night = hourly[(hourly["region_code"] == "CISO") & (hourly["period_utc"] == "2026-09-01 10:00:00")]
    assert night["intensity_kg_per_mwh"].iloc[0] == pytest.approx(443.0)
    # Midday solar dilutes it: 12,000 gas / (12,000 gas + solar) of 443.
    noon = hourly[(hourly["region_code"] == "CISO") & (hourly["period_utc"] == "2026-09-01 20:00:00")]
    assert noon["intensity_kg_per_mwh"].iloc[0] < 443.0


def test_batteries_excluded_from_intensity():
    hourly = carbon.hourly_intensity(_conn())
    # 03:00 UTC = 19:00 local in the fake CISO grid: no solar, batteries discharging 3,000 MWh.
    # Battery output must not dilute intensity the way solar does.
    evening = hourly[(hourly["region_code"] == "CISO") & (hourly["period_utc"] == "2026-09-01 03:00:00")]
    assert evening["battery_mwh"].iloc[0] == 3000
    assert evening["intensity_kg_per_mwh"].iloc[0] == pytest.approx(443.0)


def test_battery_shift_hand_example():
    hourly = pd.DataFrame({
        "region_code": ["X", "X"],
        "period_utc": ["2026-09-01 12:00:00", "2026-09-01 19:00:00"],
        "battery_mwh": [-100.0, 90.0],                 # charge 100 at noon, deliver 90 in the evening
        "intensity_kg_per_mwh": [200.0, 450.0],
    })
    out = carbon.battery_shift(hourly).iloc[0]
    assert out["cost_kg"] == 100 * 200
    assert out["credit_kg"] == 90 * 450
    assert out["avoided_tonnes"] == pytest.approx((90 * 450 - 100 * 200) / 1000)


def test_fake_grid_batteries_avoid_co2():
    out = carbon.battery_shift(carbon.hourly_intensity(_conn()))
    ciso = out[out["region_code"] == "CISO"].iloc[0]
    assert ciso["charge_intensity"] < ciso["discharge_intensity"]
