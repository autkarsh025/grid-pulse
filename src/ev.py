"""EV charging carbon: when should an electric car charge to pollute the least?

Unit trick worth knowing: grid intensity in kg CO2/MWh equals g CO2/kWh. So
    EV grams CO2 per mile = grid intensity (g/kWh) x car efficiency (kWh/mile)

Gas baseline (EPA): 8,887 g CO2 per gallon of gasoline, average on-road fuel economy
~22.2 mpg  ->  ~400 g CO2 per mile (tailpipe).
EV efficiency default 0.28 kWh/mile, in the range of EPA "kWh/100 mi" label ratings,
which are measured at the wall, so charging losses are already included.

Fair-comparison caveat: the gas figure is tailpipe only and the EV figure is power-plant
only (both exclude upstream fuel production), and grid intensity is an hourly AVERAGE.
"""
from __future__ import annotations

import pandas as pd

from src import config

GAS_G_PER_GALLON = 8887.0
GAS_MPG = 22.2
GAS_G_PER_MILE = round(GAS_G_PER_GALLON / GAS_MPG, 1)   # ~400
DEFAULT_KWH_PER_MILE = 0.28

# Charging windows, in local hours (start inclusive). "Smart" = the cleanest N hours.
WINDOWS: dict[str, tuple[int, ...]] = {
    "overnight": (0, 1, 2, 3, 4, 5),       # plug in at night, the most common habit
    "evening": (18, 19, 20, 21),           # plug in right after getting home
    "midday": (10, 11, 12, 13, 14),        # charge at work or a daytime charger
}
SMART_HOURS = 4


def hourly_profile(hourly: pd.DataFrame, region: str, days: int) -> pd.DataFrame:
    """Average grid intensity (g CO2/kWh) by local hour over the last `days` days."""
    r = hourly[hourly["region_code"] == region].dropna(subset=["intensity_kg_per_mwh"]).copy()
    if r.empty:
        return pd.DataFrame(columns=["local_hour", "g_per_kwh"])
    ts = pd.to_datetime(r["period_utc"], utc=True)
    r = r[ts >= ts.max() - pd.Timedelta(days=days)]
    r["local_hour"] = pd.to_datetime(r["period_utc"], utc=True).dt.tz_convert(config.REGION_TZ[region]).dt.hour
    prof = r.groupby("local_hour")["intensity_kg_per_mwh"].mean().reindex(range(24))
    return pd.DataFrame({"local_hour": range(24), "g_per_kwh": prof.round(1).to_numpy()})


def window_intensity(profile: pd.DataFrame) -> dict[str, float | None]:
    """Average intensity for each charging window, plus 'smart' (cleanest hours)."""
    g = profile.set_index("local_hour")["g_per_kwh"]
    out: dict[str, float | None] = {}
    for name, hours in WINDOWS.items():
        vals = g.reindex(hours).dropna()
        out[name] = round(float(vals.mean()), 1) if len(vals) else None
    clean = g.dropna().nsmallest(SMART_HOURS)
    out["smart"] = round(float(clean.mean()), 1) if len(clean) else None
    return out


def annual_kg(g_per_kwh: float, miles_per_day: float, kwh_per_mile: float = DEFAULT_KWH_PER_MILE) -> float:
    """Yearly CO2 (kg) for an EV charged at a given average grid intensity."""
    return g_per_kwh * kwh_per_mile * miles_per_day * 365 / 1000


def gas_annual_kg(miles_per_day: float) -> float:
    return GAS_G_PER_MILE * miles_per_day * 365 / 1000


def payload(hourly: pd.DataFrame) -> dict:
    out = {"gas_g_per_mile": GAS_G_PER_MILE, "default_kwh_per_mile": DEFAULT_KWH_PER_MILE,
           "windows": {k: list(v) for k, v in WINDOWS.items()}, "smart_hours": SMART_HOURS, "regions": {}}
    for region in config.REGIONS:
        p90 = hourly_profile(hourly, region, 90)
        p7 = hourly_profile(hourly, region, 7)
        week = p7.dropna()
        cleanest = week.nsmallest(3, "g_per_kwh")["local_hour"].astype(int).tolist() if not week.empty else []
        dirtiest = week.nlargest(3, "g_per_kwh")["local_hour"].astype(int).tolist() if not week.empty else []
        nulls = lambda s: [None if pd.isna(v) else float(v) for v in s]
        out["regions"][region] = {
            "hour": list(range(24)),
            "g_per_kwh_90d": nulls(p90["g_per_kwh"]) if not p90.empty else [],
            "g_per_kwh_7d": nulls(p7["g_per_kwh"]) if not p7.empty else [],
            "windows_90d": window_intensity(p90) if not p90.empty else {},
            "cleanest_hours_7d": cleanest,
            "dirtiest_hours_7d": dirtiest,
        }
    return out
