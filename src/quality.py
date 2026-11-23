"""Data-quality checks. Run on each cleaned batch BEFORE it's loaded, so bad data never lands.

Each check returns a CheckResult. 'error' checks stop the pipeline; 'warn' checks
are logged and recorded but let the run continue.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from src import config

log = logging.getLogger(__name__)


class DataQualityError(RuntimeError):
    pass


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    severity: str = "error"  # 'error' fails the run, 'warn' only logs

    def __str__(self) -> str:
        mark = "PASS" if self.passed else ("FAIL" if self.severity == "error" else "WARN")
        return f"[{mark}] {self.name}: {self.detail}"


# ------------------------------------------------------------------ batch checks
def check_not_empty(df: pd.DataFrame, name: str) -> CheckResult:
    return CheckResult(f"{name}_not_empty", len(df) > 0, f"{len(df)} rows")


def check_unique_keys(df: pd.DataFrame, keys: list[str], name: str) -> CheckResult:
    dupes = int(df.duplicated(keys).sum())
    return CheckResult(f"{name}_unique_keys", dupes == 0, f"{dupes} duplicate keys on {keys}")


def check_null_share(df: pd.DataFrame, column: str, name: str,
                     max_share: float = config.MAX_NULL_SHARE) -> CheckResult:
    share = float(df[column].isna().mean()) if len(df) else 0.0
    return CheckResult(
        f"{column}_nulls", share <= max_share,
        f"{share:.2%} missing (limit {max_share:.0%})",
    )


def check_completeness(df: pd.DataFrame, start: date, end: date,
                       min_ratio: float = config.MIN_COMPLETENESS) -> CheckResult:
    """Every region should have one row per UTC hour. In UTC every day has exactly 24 hours;
    in local time, daylight-saving days have 23 or 25. That's why we store UTC."""
    expected_hours = ((end - start).days + 1) * 24
    worst_region, worst_ratio = None, 1.0
    for region in config.REGIONS:
        got = df.loc[df["region_code"] == region, "period_utc"].nunique()
        ratio = got / expected_hours
        if ratio < worst_ratio:
            worst_region, worst_ratio = region, ratio
    detail = (f"lowest: {worst_region} at {worst_ratio:.1%} of {expected_hours} expected hours"
              if worst_region else f"all regions have {expected_hours}/{expected_hours} hours")
    return CheckResult("demand_completeness", worst_ratio >= min_ratio, detail)


def check_demand_range(df: pd.DataFrame) -> CheckResult:
    """Demand must be positive and below a sane per-region ceiling."""
    problems = []
    for region, ceiling in config.MAX_DEMAND_MWH.items():
        vals = df.loc[df["region_code"] == region, "demand_mwh"].dropna()
        n_bad = int(((vals <= 0) | (vals > ceiling)).sum())
        if n_bad:
            problems.append(f"{region}: {n_bad} values outside (0, {ceiling:,.0f}]")
    return CheckResult("demand_range", not problems, "; ".join(problems) or "all values in range")


def check_balance(df: pd.DataFrame, max_gap: float = config.MAX_BALANCE_GAP) -> CheckResult:
    """Reconciliation: demand should equal net generation minus interchange (exports).
    Three numbers reported separately by each grid must agree; a gap means a broken feed."""
    cols = {"demand_mwh", "net_generation_mwh", "interchange_mwh"}
    if not cols <= set(df.columns) or df[list(cols)].dropna().empty:
        return CheckResult("energy_balance", True, "net generation / interchange not in batch", "warn")
    problems = []
    for region, r in df.dropna(subset=list(cols)).groupby("region_code"):
        gap = ((r["net_generation_mwh"] - r["interchange_mwh"] - r["demand_mwh"]).abs() / r["demand_mwh"].abs()).median()
        if gap > max_gap:
            problems.append(f"{region}: median gap {gap:.1%}")
    return CheckResult("energy_balance", not problems,
                       "; ".join(problems) or f"demand = generation - exports within {max_gap:.0%}", "warn")


def check_solar_at_night(gen: pd.DataFrame) -> CheckResult:
    """Solar output between midnight and 4am local time should be near zero."""
    solar = gen[gen["fuel_type"] == config.SOLAR_CODE]
    if solar.empty:
        return CheckResult("solar_at_night", True, "no solar rows in batch", "warn")
    problems = []
    for region, tz in config.REGION_TZ.items():
        r = solar[solar["region_code"] == region]
        if r.empty:
            continue
        local_hour = pd.to_datetime(r["period_utc"], utc=True).dt.tz_convert(tz).dt.hour
        peak = r["generation_mwh"].max()
        night_max = r.loc[local_hour.between(0, 3).values, "generation_mwh"].max()
        if pd.notna(peak) and peak > 0 and pd.notna(night_max) \
                and night_max > config.SOLAR_NIGHT_MAX_SHARE * peak:
            problems.append(f"{region}: night max {night_max:,.0f} vs peak {peak:,.0f} MWh")
    return CheckResult("solar_at_night", not problems,
                       "; ".join(problems) or "night solar near zero", "warn")


def run_batch_checks(demand: pd.DataFrame, generation: pd.DataFrame,
                     start: date, end: date) -> list[CheckResult]:
    return [
        check_not_empty(demand, "demand"),
        check_not_empty(generation, "generation"),
        check_unique_keys(demand, ["region_code", "period_utc"], "demand"),
        check_unique_keys(generation, ["region_code", "period_utc", "fuel_type"], "generation"),
        check_null_share(demand, "demand_mwh", "demand"),
        check_null_share(generation, "generation_mwh", "generation"),
        check_completeness(demand, start, end),
        check_demand_range(demand),
        check_balance(demand),
        check_solar_at_night(generation),
    ]


# ------------------------------------------------------------------ database checks
def check_freshness(conn: sqlite3.Connection, max_age_hours: int = config.FRESHNESS_HOURS,
                    now: datetime | None = None) -> CheckResult:
    """The newest hour in the database should be recent. Catches a silently stalled pipeline."""
    now = now or datetime.now(timezone.utc)
    latest = conn.execute("SELECT MAX(period_utc) FROM hourly_demand").fetchone()[0]
    if latest is None:
        return CheckResult("freshness", False, "database has no demand rows")
    latest_dt = datetime.strptime(latest, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    age = now - latest_dt
    return CheckResult("freshness", age <= timedelta(hours=max_age_hours),
                       f"latest hour {latest} UTC is {age.total_seconds() / 3600:.0f}h old "
                       f"(limit {max_age_hours}h)")


def enforce(results: list[CheckResult]) -> None:
    """Log every result and raise if any error-level check failed."""
    for r in results:
        (log.info if r.passed else log.warning)(str(r))
    failures = [r for r in results if not r.passed and r.severity == "error"]
    if failures:
        raise DataQualityError("; ".join(str(f) for f in failures))
