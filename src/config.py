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

# Balancing authorities to track. CISO = California ISO, ERCO = ERCOT (Texas).
REGIONS: dict[str, str] = {
    "CISO": "California ISO",
    "ERCO": "Electric Reliability Council of Texas",
}
# Local time zones, used only for analysis and the solar-at-night check.
REGION_TZ: dict[str, str] = {
    "CISO": "America/Los_Angeles",
    "ERCO": "America/Chicago",
}

# region-data "type" codes we keep, and the column each becomes.
DEMAND_TYPES: dict[str, str] = {"D": "demand_mwh", "DF": "forecast_mwh"}

# Fuel codes. Confirm the battery code in the API browser (fueltype facet).
SOLAR_CODE = "SUN"
BATTERY_CODE = "BAT"

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
}
SOLAR_NIGHT_MAX_SHARE = 0.05   # warn if night solar > 5% of the batch's peak solar
FRESHNESS_HOURS = 72           # EIA publishes with a lag, so allow a few days
