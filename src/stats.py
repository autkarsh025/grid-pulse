"""Cross-grid statistics: one row per grid, used for the 'Compare grids' league table.

Most numbers are straightforward aggregates over the database; a few reuse other modules
(battery share of net peak, carbon, weather sensitivities). Missing data -> None, never 0,
so a grid without batteries shows a dash instead of a misleading zero.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from src import config


def _f(v, nd=1):
    return None if v is None or pd.isna(v) else round(float(v), nd)


def interchange_profile(conn: sqlite3.Connection, region: str, days: int = 90) -> dict:
    """Average net interchange by local hour (+ exports, - imports), last `days` days, in GW."""
    df = pd.read_sql_query(
        """SELECT period_utc, interchange_mwh, demand_mwh FROM hourly_demand
           WHERE region_code = ? AND interchange_mwh IS NOT NULL
             AND period_utc >= datetime((SELECT MAX(period_utc) FROM hourly_demand WHERE region_code = ?), ?)""",
        conn, params=(region, region, f"-{days} days"))
    if df.empty:
        return {"hour": [], "gw": []}
    h = pd.to_datetime(df["period_utc"], utc=True).dt.tz_convert(config.REGION_TZ[region]).dt.hour
    prof = (df.groupby(h)["interchange_mwh"].mean() / 1000).reindex(range(24))
    return {"hour": list(range(24)), "gw": [_f(v, 2) for v in prof]}


def league_table(conn: sqlite3.Connection, extras: dict) -> list[dict]:
    """extras: per-region values computed elsewhere (net-peak share, RTE, carbon, EV, weather)."""
    dem = pd.read_sql_query(
        """SELECT region_code,
                  AVG(demand_mwh) AS avg_mw, MAX(demand_mwh) AS peak_mw,
                  AVG(CASE WHEN interchange_mwh < 0 THEN 1.0 WHEN interchange_mwh >= 0 THEN 0.0 END) AS import_hours,
                  AVG(ABS(forecast_mwh - demand_mwh) / demand_mwh) * 100 AS mape
           FROM hourly_demand WHERE demand_mwh > 0 GROUP BY region_code""", conn).set_index("region_code")
    mix = pd.read_sql_query(
        """SELECT region_code, fuel_type, SUM(generation_mwh) AS mwh FROM hourly_generation
           WHERE generation_mwh > 0 AND fuel_type <> 'BAT' GROUP BY region_code, fuel_type""", conn)
    rows = []
    for region, name in config.REGIONS.items():
        d = dem.loc[region] if region in dem.index else None
        m = mix[mix["region_code"] == region]
        total = m["mwh"].sum()
        # A fuel the grid doesn't report at all is None ("not reported"), never 0%.
        share = lambda code: _f(100 * m.loc[m["fuel_type"] == code, "mwh"].sum() / total) \
            if total and (m["fuel_type"] == code).any() else None
        e = extras.get(region, {})
        rows.append({
            "region": region, "name": name,
            "avg_demand_gw": _f(d["avg_mw"] / 1000) if d is not None else None,
            "peak_demand_gw": _f(d["peak_mw"] / 1000) if d is not None else None,
            "solar_share": share(config.SOLAR_CODE),
            "wind_share": share(config.WIND_CODE),
            "battery_net_peak": e.get("battery_net_peak"),
            "rte": e.get("rte"),
            "carbon": e.get("carbon"),
            "ev_midday": e.get("ev_midday"),
            "import_hours": _f(100 * d["import_hours"]) if d is not None else None,
            "cooling": e.get("cooling"),
            "heating": e.get("heating"),
            "mape": _f(d["mape"], 2) if d is not None else None,
        })
    return rows
