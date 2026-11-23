from datetime import date

import pandas as pd
import pytest
import requests

from fakes import fake_demand_records
from src import config, load, transform, weather


class FakeResp:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload


class FakeSession:
    """Returns an hourly temperature series; records which endpoint was used."""
    def __init__(self, temp_fn): self.temp_fn, self.urls = temp_fn, []

    def get(self, url, params, timeout):
        self.urls.append(url)
        hours = pd.date_range(params["start_date"], pd.Timestamp(params["end_date"]) + pd.Timedelta(hours=23), freq="h")
        return FakeResp({"hourly": {"time": hours.strftime("%Y-%m-%dT%H:%M").tolist(),
                                    "temperature_2m": [self.temp_fn(t) for t in hours]}})


def test_archive_and_recent_are_stitched():
    s = FakeSession(lambda t: 70.0)
    df = weather.fetch_region("CISO", date(2026, 11, 1), date(2026, 11, 20), s, today=date(2026, 11, 21))
    assert config.WEATHER_ARCHIVE_URL in s.urls and config.WEATHER_FORECAST_URL in s.urls
    assert df["period_utc"].is_unique and len(df) == 20 * 24


def test_weather_failure_never_breaks_the_pipeline():
    class Broken:
        def get(self, *a, **k): raise requests.ConnectionError("open-meteo down")
    conn = load.connect(); load.init_db(conn)
    assert weather.refresh(conn, date(2026, 11, 1), date(2026, 11, 2), Broken()) == 0  # no exception


def test_cooling_sensitivity_recovers_known_slope():
    # Build 60 hot days where daily peak demand = 20,000 + 500 MW per degF above 75.
    start = date(2026, 7, 1)
    days = pd.date_range(start, periods=60, freq="D")
    tmax = {d.date(): 76 + (i % 20) for i, d in enumerate(days)}
    conn = load.connect(); load.init_db(conn)
    recs = fake_demand_records(start, days[-1].date(), regions=("CISO",))
    d = transform.transform_demand(recs).df
    local = pd.to_datetime(d["period_utc"], utc=True).dt.tz_convert("America/Los_Angeles")
    d["demand_mwh"] = [20_000 + 500 * (tmax.get(ld.date(), 76) - 75) if ld.hour == 17 else 10_000 for ld in local]
    load.load_batch(conn, d, transform.transform_generation([]).df)
    w = pd.DataFrame({"region_code": "CISO", "period_utc": d["period_utc"],
                      "temp_f": [float(tmax.get(ld.date(), 76)) if ld.hour == 17 else 60.0 for ld in local]})
    load.upsert_weather(conn, w)
    out = weather.demand_temperature(conn, "CISO")
    assert out["cooling_mw_per_f"] == pytest.approx(500, rel=0.05)
