"""Weather enrichment: hourly temperature at each grid's load-center city (Open-Meteo, free, no key).

Why: temperature drives demand (air conditioning, electric heating). Joining weather to
demand lets us measure how many extra megawatts each degree of heat or cold costs a grid.

Design choice: weather is ENRICHMENT, not core data. If Open-Meteo is down, the pipeline
logs a warning and carries on; the grid data still loads. Core data failing = stop;
enrichment failing = degrade gracefully.

The archive API lags real time by several days, so recent days come from the forecast
API's recorded past instead. Upserts mean later runs overwrite them with archive values.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

from src import config, load
from src.extract import make_session

log = logging.getLogger(__name__)
ARCHIVE_LAG_DAYS = 6


def _fetch(url: str, lat: float, lon: float, start: date, end: date, session) -> pd.DataFrame:
    params = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m", "temperature_unit": "fahrenheit",
              "timezone": "GMT", "start_date": start.isoformat(), "end_date": end.isoformat()}
    resp = session.get(url, params=params, timeout=config.WEATHER_TIMEOUT_S)
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})
    df = pd.DataFrame({"time": hourly.get("time", []), "temp_f": hourly.get("temperature_2m", [])})
    if df.empty:
        return pd.DataFrame(columns=["period_utc", "temp_f"])
    df["period_utc"] = pd.to_datetime(df["time"], format="%Y-%m-%dT%H:%M").dt.strftime("%Y-%m-%d %H:%M:%S")
    df["temp_f"] = pd.to_numeric(df["temp_f"], errors="coerce")
    return df[["period_utc", "temp_f"]]


def fetch_region(region: str, start: date, end: date, session=None, today: date | None = None) -> pd.DataFrame:
    """Hourly temperature for one grid over [start, end], stitched from archive + recent forecast data."""
    session = session or make_session()
    _, lat, lon = config.REGION_WEATHER_POINT[region]
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=ARCHIVE_LAG_DAYS)
    parts = []
    if start <= cutoff:
        parts.append(_fetch(config.WEATHER_ARCHIVE_URL, lat, lon, start, min(end, cutoff), session))
    if end > cutoff:
        parts.append(_fetch(config.WEATHER_FORECAST_URL, lat, lon, max(start, cutoff + timedelta(days=1)), end, session))
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["period_utc", "temp_f"])
    df = df.drop_duplicates("period_utc", keep="first")  # archive first, so archive wins on overlap
    return df.assign(region_code=region)


def refresh(conn, start: date, end: date, session=None) -> int:
    """Fetch and upsert weather for every grid. Never raises: failures are logged per region."""
    total = 0
    for region in config.REGIONS:
        if region not in config.REGION_WEATHER_POINT:
            continue
        try:
            df = fetch_region(region, start, end, session)
            total += load.upsert_weather(conn, df)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("weather for %s skipped (enrichment only): %s", region, exc)
    log.info("weather: %d hourly rows upserted", total)
    return total


def demand_temperature(conn, region: str) -> dict:
    """Daily peak demand vs. daily max/min temperature (local days), with heat and cold sensitivities.

    Cooling sensitivity: slope of daily peak demand vs. daily MAX temp on hot days (max >= 75F).
    Heating sensitivity: slope vs. daily MIN temp on cold days (min <= 45F), reported as MW per
    degree COLDER. Simple least-squares lines: easy to explain, good enough to compare grids.
    """
    df = pd.read_sql_query(
        """SELECT d.period_utc, d.demand_mwh, w.temp_f FROM hourly_demand d
           JOIN weather_hourly w USING (region_code, period_utc)
           WHERE d.region_code = ? AND d.demand_mwh IS NOT NULL AND w.temp_f IS NOT NULL""",
        conn, params=(region,))
    empty = {"days": [], "peak_mw": [], "tmax": [], "tmin": [], "cooling_mw_per_f": None, "heating_mw_per_f": None,
             "city": config.REGION_WEATHER_POINT.get(region, ("", 0, 0))[0]}
    if df.empty:
        return empty
    local = pd.to_datetime(df["period_utc"], utc=True).dt.tz_convert(config.REGION_TZ[region])
    df["day"] = local.dt.date.astype(str)
    daily = df.groupby("day").agg(peak_mw=("demand_mwh", "max"), tmax=("temp_f", "max"),
                                  tmin=("temp_f", "min"), n=("demand_mwh", "size"))
    daily = daily[daily["n"] >= 20].reset_index()  # skip partial days

    def slope(x: pd.Series, y: pd.Series) -> float | None:
        if len(x) < 15 or x.nunique() < 5:
            return None
        xm, ym = x.mean(), y.mean()
        return round(float(((x - xm) * (y - ym)).sum() / ((x - xm) ** 2).sum()), 1)

    hot = daily[daily["tmax"] >= 75]
    cold = daily[daily["tmin"] <= 45]
    heating = slope(cold["tmin"], cold["peak_mw"])
    return {
        "days": daily["day"].tolist(), "peak_mw": daily["peak_mw"].round().tolist(),
        "tmax": daily["tmax"].round(1).tolist(), "tmin": daily["tmin"].round(1).tolist(),
        "cooling_mw_per_f": slope(hot["tmax"], hot["peak_mw"]),
        "heating_mw_per_f": None if heating is None else -heating,  # positive = MW added per degree colder
        "city": config.REGION_WEATHER_POINT[region][0],
    }
