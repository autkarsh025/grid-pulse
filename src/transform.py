"""Transform: turn raw EIA rows into tidy, typed DataFrames that match the SQL schema.

Transform fixes *shape and types* (parsing timestamps, casting numbers, removing
exact duplicates). It does NOT judge whether values are plausible; that's the
job of quality.py, so bad values get flagged instead of silently "fixed".
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from src import config

log = logging.getLogger(__name__)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"  # how timestamps are stored in SQLite
DEMAND_COLUMNS = ["region_code", "period_utc", "demand_mwh", "forecast_mwh"]
GENERATION_COLUMNS = ["region_code", "period_utc", "fuel_type", "generation_mwh"]


@dataclass
class TransformResult:
    df: pd.DataFrame
    rows_in: int
    rows_rejected: int = 0
    duplicates_dropped: int = 0
    notes: list[str] = field(default_factory=list)


def load_raw(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text())


def _base_clean(records: list[dict], code_col: str) -> tuple[pd.DataFrame, int, list[str]]:
    """Steps shared by both datasets: required columns, UTC timestamps, numeric values."""
    df = pd.DataFrame(records)
    notes: list[str] = []
    required = {"period", "respondent", code_col, "value"}
    missing = required - set(df.columns)
    if df.empty:
        return pd.DataFrame(columns=["region_code", "period_utc", code_col, "value"]), 0, notes
    if missing:
        raise ValueError(f"Raw data is missing columns {sorted(missing)}; did the API change?")

    # EIA hourly periods look like '2026-11-20T08' and are in UTC.
    df["period_utc"] = pd.to_datetime(df["period"], format="%Y-%m-%dT%H", errors="coerce", utc=True)
    df["region_code"] = df["respondent"].astype("string").str.strip().str.upper()
    df[code_col] = df[code_col].astype("string").str.strip().str.upper()
    # EIA returns values as strings; anything non-numeric becomes NaN (counted by quality checks).
    df["value"] = pd.to_numeric(df["value"], errors="coerce").astype("float64")

    if "value-units" in df.columns:
        units = set(df["value-units"].dropna().str.lower())
        if units - {"megawatthours", "mwh"}:
            notes.append(f"unexpected units: {sorted(units)}")

    bad = df["period_utc"].isna() | df["region_code"].isna() | df[code_col].isna()
    if bad.any():
        notes.append(f"rejected {int(bad.sum())} rows with unparseable period/region/code")
    df = df.loc[~bad, ["region_code", "period_utc", code_col, "value"]]
    return df, int(bad.sum()), notes


def transform_demand(records: list[dict]) -> TransformResult:
    df, rejected, notes = _base_clean(records, "type")
    rows_in = len(records)
    if df.empty:
        return TransformResult(pd.DataFrame(columns=DEMAND_COLUMNS), rows_in, rejected, 0, notes)
    df = df[df["type"].isin(list(config.DEMAND_TYPES))]

    keys = ["region_code", "period_utc", "type"]
    dupes = int(df.duplicated(keys).sum())
    df = df.drop_duplicates(keys, keep="last")

    # Long -> wide: one row per region-hour with demand and forecast columns.
    wide = (
        df.pivot(index=["region_code", "period_utc"], columns="type", values="value")
        .rename(columns=config.DEMAND_TYPES)
        .reindex(columns=list(config.DEMAND_TYPES.values()))
        .reset_index()
    )
    wide.columns.name = None
    wide["period_utc"] = wide["period_utc"].dt.strftime(TS_FORMAT)
    wide = wide[DEMAND_COLUMNS].sort_values(["region_code", "period_utc"], ignore_index=True)
    return TransformResult(wide, rows_in, rejected, dupes, notes)


def transform_generation(records: list[dict]) -> TransformResult:
    df, rejected, notes = _base_clean(records, "fueltype")
    rows_in = len(records)
    if df.empty:
        return TransformResult(pd.DataFrame(columns=GENERATION_COLUMNS), rows_in, rejected, 0, notes)
    df = df.rename(columns={"fueltype": "fuel_type", "value": "generation_mwh"})

    # Map region-specific codes to a common one (e.g. CAISO reports batteries under OTH).
    for (region, raw_code), code in config.FUEL_CODE_OVERRIDES.items():
        mask = (df["region_code"] == region) & (df["fuel_type"] == raw_code)
        if mask.any():
            df.loc[mask, "fuel_type"] = code
            notes.append(f"mapped {int(mask.sum())} {region} {raw_code} rows to {code}")

    keys = ["region_code", "period_utc", "fuel_type"]
    dupes = int(df.duplicated(keys).sum())
    df = df.drop_duplicates(keys, keep="last")

    # Negative battery values mean charging; they're real and are kept as-is.
    df["period_utc"] = df["period_utc"].dt.strftime(TS_FORMAT)
    df = df[GENERATION_COLUMNS].sort_values(keys, ignore_index=True)
    return TransformResult(df, rows_in, rejected, dupes, notes)


def stage(df: pd.DataFrame, name: str, tag: str) -> Path:
    """Write a cleaned DataFrame to data/staging/<name>_<tag>.csv for the load step."""
    config.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    path = config.STAGING_DIR / f"{name}_{tag}.csv"
    df.to_csv(path, index=False)
    return path


def read_staged(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"region_code": "string", "period_utc": "string", "fuel_type": "string"})
