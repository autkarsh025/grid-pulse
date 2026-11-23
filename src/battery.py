"""Battery fleet analytics: dispatch heatmap, full-power hours, and records.

Full-power hours (a utilization metric):
    daily discharge MWh / fleet peak discharge MW
"How many hours would the fleet have to run flat out to deliver today's energy?"
The fleet's peak is approximated by the highest hourly discharge in the trailing
30 days, because EIA's hourly data doesn't include installed capacity. A 4-hour
battery fleet cycling once a day would score close to 4.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from src import config


def battery_hourly(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT region_code, period_utc, generation_mwh AS mwh FROM hourly_generation WHERE fuel_type = ?",
        conn, params=(config.BATTERY_CODE,),
    )
    df["ts"] = pd.to_datetime(df["period_utc"], utc=True)
    return df


def to_local(df: pd.DataFrame, region: str) -> pd.DataFrame:
    r = df[df["region_code"] == region].copy()
    r["local"] = r["ts"].dt.tz_convert(config.REGION_TZ[region])
    return r


def dispatch_heatmap(df: pd.DataFrame, region: str) -> dict:
    """Average battery MWh by ISO week (rows) x local hour (columns). + discharge, - charge."""
    r = to_local(df, region)
    if r.empty:
        return {"weeks": [], "hours": list(range(24)), "z": []}
    r["week"] = r["local"].dt.tz_localize(None).dt.to_period("W-SUN").dt.start_time.dt.strftime("%Y-%m-%d")
    r["hour"] = r["local"].dt.hour
    grid = r.pivot_table(index="week", columns="hour", values="mwh", aggfunc="mean").reindex(columns=range(24))
    z = [[None if pd.isna(v) else round(float(v)) for v in row] for row in grid.to_numpy()]
    return {"weeks": grid.index.tolist(), "hours": list(range(24)), "z": z}


def full_power_hours(df: pd.DataFrame, region: str) -> pd.DataFrame:
    """Daily discharge / trailing-30-day peak hourly discharge."""
    r = to_local(df, region)
    if r.empty:
        return pd.DataFrame(columns=["day", "discharge_mwh", "peak_mw", "full_power_hours"])
    r["day"] = r["local"].dt.date
    daily = r.groupby("day").agg(discharge_mwh=("mwh", lambda s: s.clip(lower=0).sum()),
                                 day_peak=("mwh", "max"), hours=("mwh", "size")).reset_index()
    daily = daily[daily["hours"] >= 22]   # partial first/last days would fake a drop
    daily["peak_mw"] = daily["day_peak"].clip(lower=0).rolling(30, min_periods=7).max()
    daily["full_power_hours"] = (daily["discharge_mwh"] / daily["peak_mw"].where(daily["peak_mw"] > 0)).round(2)
    daily["day"] = daily["day"].astype(str)
    return daily[["day", "discharge_mwh", "peak_mw", "full_power_hours"]]


def records(conn: sqlite3.Connection, region: str) -> list[dict]:
    """All-time records in the database for one region (times shown in local time)."""
    tz = config.REGION_TZ[region]
    out: list[dict] = []

    def add(label: str, sql: str, unit: str, params=()):
        row = conn.execute(sql, (region, *params)).fetchone()
        if row and row[0] is not None:
            when = pd.Timestamp(row[1], tz="UTC").tz_convert(tz)
            out.append({"label": label, "value": round(float(row[0]), 1), "unit": unit,
                        "when": when.strftime("%b %d, %Y · %-I %p")})

    add("Biggest battery discharge hour",
        "SELECT generation_mwh, period_utc FROM hourly_generation WHERE region_code=? AND fuel_type=? "
        "ORDER BY generation_mwh DESC LIMIT 1", "MWh", (config.BATTERY_CODE,))
    add("Biggest battery charging hour",
        "SELECT -generation_mwh, period_utc FROM hourly_generation WHERE region_code=? AND fuel_type=? "
        "ORDER BY generation_mwh ASC LIMIT 1", "MWh", (config.BATTERY_CODE,))
    add("Biggest solar hour",
        "SELECT generation_mwh, period_utc FROM hourly_generation WHERE region_code=? AND fuel_type=? "
        "ORDER BY generation_mwh DESC LIMIT 1", "MWh", (config.SOLAR_CODE,))
    add("Highest demand hour",
        "SELECT demand_mwh, period_utc FROM hourly_demand WHERE region_code=? "
        "ORDER BY demand_mwh DESC LIMIT 1", "MWh")
    return out
