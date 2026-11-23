"""Central configuration: paths, API routes, regions and data-quality thresholds.

Everything tunable lives here so the other modules stay free of magic numbers.
"""
from __future__ import annotations

import os
from pathlib import Path

# Load variables from a local .env file (EIA_API_KEY) if python-dotenv is installed.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional
    pass

# ---------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("GRID_PULSE_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"          # API responses exactly as received
STAGING_DIR = DATA_DIR / "staging"  # cleaned CSVs waiting to be loaded
DB_PATH = Path(os.getenv("GRID_PULSE_DB", DATA_DIR / "grid_pulse.db"))
SCHEMA_PATH = ROOT / "sql" / "schema.sql"

# ---------------------------------------------------------------- EIA API
EIA_API_KEY_ENV = "EIA_API_KEY"
EIA_BASE_URL = "https://api.eia.gov/v2"
# Verify these in the EIA API browser (eia.gov/opendata/browser) before your first run.
REGION_ROUTE = "electricity/rto/region-data/data/"     # demand, forecast, net gen, interchange
FUEL_ROUTE = "electricity/rto/fuel-type-data/data/"    # generation by fuel type
PAGE_SIZE = 5000          # EIA's maximum rows per request
REQUEST_TIMEOUT_S = 60
CHUNK_DAYS = 30           # backfills are split into chunks of this many days

# Balancing authorities to track: the seven U.S. ISOs/RTOs, which together serve most of
# the country's electricity demand. Codes are EIA "respondent" IDs.
REGIONS: dict[str, str] = {
    "CISO": "California ISO",
    "ERCO": "Electric Reliability Council of Texas",
    "PJM": "PJM Interconnection (Mid-Atlantic & Ohio Valley)",
    "MISO": "Midcontinent ISO (Midwest & South)",
    "SWPP": "Southwest Power Pool (Great Plains)",
    "NYIS": "New York ISO",
    "ISNE": "ISO New England",
}
# Local time zones (analysis only; storage is always UTC). Multi-zone grids use their main zone.
REGION_TZ: dict[str, str] = {
    "CISO": "America/Los_Angeles",
    "ERCO": "America/Chicago",
    "PJM": "America/New_York",
    "MISO": "America/Chicago",
    "SWPP": "America/Chicago",
    "NYIS": "America/New_York",
    "ISNE": "America/New_York",
}
# One representative load-center city per grid, for the weather join.
REGION_WEATHER_POINT: dict[str, tuple[str, float, float]] = {
    "CISO": ("Los Angeles", 34.05, -118.24),
    "ERCO": ("Dallas", 32.78, -96.80),
    "PJM": ("Philadelphia", 39.95, -75.17),
    "MISO": ("Minneapolis", 44.98, -93.27),
    "SWPP": ("Oklahoma City", 35.47, -97.52),
    "NYIS": ("New York City", 40.71, -74.01),
    "ISNE": ("Boston", 42.36, -71.06),
}

# region-data "type" codes we keep, and the column each becomes.
# NG = net generation inside the grid; TI = total interchange with neighbors (EIA sign
# convention: positive = net exports, negative = net imports). Identity: D ~= NG - TI.
DEMAND_TYPES: dict[str, str] = {"D": "demand_mwh", "DF": "forecast_mwh",
                                "NG": "net_generation_mwh", "TI": "interchange_mwh"}

# Fuel codes. Confirm the battery code in the API browser (fueltype facet).
SOLAR_CODE = "SUN"
WIND_CODE = "WND"
BATTERY_CODE = "BAT"

# Balancing authorities don't all report batteries the same way. ERCOT uses 'BAT'.
# CAISO reports no 'BAT' code; its batteries sit inside 'OTH' (EIA folds categories into
# "Other" for confidentiality). Our data profile proved it: CAISO OTH averages about
# -7,100 MWh at noon (charging) and +5,000 MWh at 7pm (discharging), swinging +/-11 GW,
# which is the CAISO battery fleet. So CAISO OTH is mapped to BAT at transform time.
# Caveat: CAISO "BAT" therefore also includes a small amount of other generation.
#
# ERCOT briefly reported part of its battery CHARGING under 'UES' ("unknown energy storage"):
# ~490 hours in late 2025, every value negative. Left separate, it made Dec 2025 round-trip
# efficiency read 164% (charging undercounted). Mapped to BAT and summed with it.
FUEL_CODE_OVERRIDES: dict[tuple[str, str], str] = {
    ("CISO", "OTH"): "BAT",
    ("ERCO", "UES"): "BAT",
}

# Direct CO2 emissions per MWh generated, by fuel (kg CO2 / MWh).
# Coal 2,257 lb/MWh and gas 976 lb/MWh are EIA's 2019 U.S. averages
# (eia.gov/todayinenergy/detail.php?id=48296). Oil is an approximation; it's a tiny
# share in CAISO and ERCOT. Solar, wind, hydro and nuclear emit nothing directly.
# Batteries are excluded: their emissions belong to whatever charged them.
EMISSION_FACTORS_KG_PER_MWH: dict[str, float] = {
    "COL": 1024.0,   # 2,257 lb/MWh
    "NG": 443.0,     # 976 lb/MWh
    "OIL": 1000.0,   # approximate
}

# ---------------------------------------------------------------- data quality
MAX_NULL_SHARE = 0.02          # fail if more than 2% of a value column is missing
MIN_COMPLETENESS = 0.95        # fail if fewer than 95% of expected hours arrived
MAX_DEMAND_MWH: dict[str, float] = {  # sanity ceilings, well above record peaks
    "CISO": 70_000,
    "ERCO": 120_000,
    "PJM": 220_000,
    "MISO": 180_000,
    "SWPP": 90_000,
    "NYIS": 50_000,
    "ISNE": 45_000,
}
# Reconciliation: demand should equal net generation minus interchange. Warn if the
# median hourly gap exceeds this share of demand.
MAX_BALANCE_GAP = 0.03
SOLAR_NIGHT_MAX_SHARE = 0.05   # warn if night solar > 5% of the batch's peak solar
FRESHNESS_HOURS = 72

# ---------------------------------------------------------------- weather (enrichment)
WEATHER_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
WEATHER_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"   # fills the archive's last few days           # EIA publishes with a lag, so allow a few days
