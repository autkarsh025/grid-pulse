import pandas as pd
import pytest

from src import ev


def _hourly(region="CISO", days=10):
    """Synthetic intensity: 300 g/kWh at night, 100 g/kWh from 10:00-14:59 local (UTC-8 in winter)."""
    ts = pd.date_range("2026-01-01", periods=24 * days, freq="h", tz="UTC")
    local_hour = ts.tz_convert("America/Los_Angeles").hour
    intensity = [100.0 if 10 <= h <= 14 else 300.0 for h in local_hour]
    return pd.DataFrame({"region_code": region, "period_utc": ts.strftime("%Y-%m-%d %H:%M:%S"),
                         "intensity_kg_per_mwh": intensity})


def test_gas_baseline_matches_epa():
    assert ev.GAS_G_PER_MILE == pytest.approx(400.3, abs=0.1)  # 8,887 g/gal / 22.2 mpg


def test_profile_and_windows():
    prof = ev.hourly_profile(_hourly(), "CISO", 90)
    assert prof.set_index("local_hour").loc[12, "g_per_kwh"] == 100
    w = ev.window_intensity(prof)
    assert w["midday"] == 100 and w["overnight"] == 300 and w["smart"] == 100


def test_annual_math():
    # 30 mi/day x 365 x 0.28 kWh/mi x 100 g/kWh = 306.6 kg
    assert ev.annual_kg(100, 30, 0.28) == pytest.approx(306.6)
    assert ev.gas_annual_kg(30) == pytest.approx(400.3 * 30 * 365 / 1000)


def test_payload_cleanest_hours():
    p = ev.payload(_hourly())
    assert set(p["regions"]["CISO"]["cleanest_hours_7d"]) <= {10, 11, 12, 13, 14}
    assert p["regions"]["ERCO"]["g_per_kwh_90d"] == []  # no data for that region -> empty, not an error
