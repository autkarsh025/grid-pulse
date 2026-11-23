"""Carbon intensity of the grid, and how much CO2 battery storage shifts.

Carbon intensity (kg CO2 per MWh) for one region-hour:
    sum(generation_mwh x emission_factor)  /  sum(generation_mwh)
over the fuels that generated in that hour. Batteries are left out of both sums,
because a battery only re-delivers energy that some other source produced.

Battery CO2 shift, the headline metric:
    avoided = sum(discharge_mwh x intensity at discharge hour)
            - sum(charge_mwh    x intensity at charge hour)
Batteries charge on clean midday solar and discharge in the dirtier evening, so
the energy they deliver displaces more CO2 than charging "cost." Round-trip
losses are included automatically, because charge MWh > discharge MWh.

Simplification worth saying out loud in an interview: this uses the AVERAGE
intensity of each hour. The textbook-correct measure is MARGINAL intensity
(which plant actually ramps up or down), which needs dispatch data we don't have.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from src import config


def hourly_intensity(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per region-hour: total generation, CO2 emitted, intensity, battery MWh."""
    factors = config.EMISSION_FACTORS_KG_PER_MWH
    values = ", ".join(f"('{fuel}', {kg})" for fuel, kg in factors.items()) or "('NONE', 0)"
    sql = f"""
    WITH factors(fuel_type, kg_per_mwh) AS (VALUES {values}),
    hourly AS (
        SELECT
            g.region_code,
            g.period_utc,
            SUM(CASE WHEN g.fuel_type <> '{config.BATTERY_CODE}' AND g.generation_mwh > 0
                     THEN g.generation_mwh END)                                   AS generation_mwh,
            SUM(CASE WHEN g.fuel_type <> '{config.BATTERY_CODE}' AND g.generation_mwh > 0
                     THEN g.generation_mwh * COALESCE(f.kg_per_mwh, 0) END)        AS co2_kg,
            SUM(CASE WHEN g.fuel_type = '{config.BATTERY_CODE}' THEN g.generation_mwh END) AS battery_mwh
        FROM hourly_generation AS g
        LEFT JOIN factors AS f USING (fuel_type)
        GROUP BY g.region_code, g.period_utc
    )
    SELECT region_code, period_utc, generation_mwh, co2_kg, battery_mwh,
           co2_kg / NULLIF(generation_mwh, 0) AS intensity_kg_per_mwh
    FROM hourly
    ORDER BY region_code, period_utc
    """
    return pd.read_sql_query(sql, conn)


def battery_shift(hourly: pd.DataFrame) -> pd.DataFrame:
    """Per region: battery MWh charged/discharged, the CO2 'cost' and 'credit', and the net avoided."""
    df = hourly.dropna(subset=["intensity_kg_per_mwh"]).copy()
    bat = df["battery_mwh"].fillna(0)
    df["discharge_mwh"] = bat.clip(lower=0)
    df["charge_mwh"] = (-bat).clip(lower=0)
    df["credit_kg"] = df["discharge_mwh"] * df["intensity_kg_per_mwh"]
    df["cost_kg"] = df["charge_mwh"] * df["intensity_kg_per_mwh"]
    out = df.groupby("region_code")[["discharge_mwh", "charge_mwh", "credit_kg", "cost_kg"]].sum()
    out["avoided_tonnes"] = (out["credit_kg"] - out["cost_kg"]) / 1000
    out["charge_intensity"] = out["cost_kg"] / out["charge_mwh"].where(out["charge_mwh"] > 0)
    out["discharge_intensity"] = out["credit_kg"] / out["discharge_mwh"].where(out["discharge_mwh"] > 0)
    return out.reset_index()


def local_hour_profile(hourly: pd.DataFrame, region: str) -> pd.DataFrame:
    """Average intensity by local hour of day for one region."""
    r = hourly[hourly["region_code"] == region].dropna(subset=["intensity_kg_per_mwh"]).copy()
    r["local_hour"] = (pd.to_datetime(r["period_utc"], utc=True)
                       .dt.tz_convert(config.REGION_TZ[region]).dt.hour)
    return r.groupby("local_hour")["intensity_kg_per_mwh"].mean().round(1).reset_index()
