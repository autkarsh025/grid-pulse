"""Build docs/index.html: an interactive, single-file dashboard that GitHub Pages can host for free.

Usage:  python -m analysis.build_dashboard          (reads data/grid_pulse.db)

The page embeds the query results as JSON and draws them with Plotly.js from a CDN,
so there's no server: open the file locally or publish the docs/ folder with Pages.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

from src import carbon, config

SQL_DIR = config.ROOT / "sql" / "analysis"
OUT = config.ROOT / "docs" / "index.html"
TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def nulls(values) -> list:
    """List with NaN replaced by None (JSON null). Series.where(..., None) is not enough:
    on a float column pandas silently turns None back into NaN."""
    return [None if pd.isna(v) else (float(v) if isinstance(v, (int, float)) else v) for v in values]


def q(conn: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:
    """Run a query; NaN becomes None so the result serializes to valid JSON (null)."""
    df = pd.read_sql_query(sql, conn, params=params)
    return df.astype(object).where(df.notna(), None)


def duck_profiles(conn) -> dict:
    wide = q(conn, (SQL_DIR / "02_hourly_wide.sql").read_text())
    out = {}
    for region, r in wide.groupby("region_code"):
        r = r.copy()
        r["h"] = pd.to_datetime(r["period_utc"], utc=True).dt.tz_convert(config.REGION_TZ[region]).dt.hour
        prof = (r.groupby("h")[["demand_mwh", "solar_mwh", "battery_mwh"]].mean() / 1000).round(2)
        out[region] = {"hour": prof.index.tolist(),
                       **{c.replace("_mwh", ""): nulls(prof[c]) for c in prof}}
    return out


def recent_series(conn, days: int = 30) -> dict:
    """Hourly demand and battery for the last N days, per region, for the anomaly explorer."""
    latest = conn.execute("SELECT MAX(period_utc) FROM hourly_demand").fetchone()[0]
    out = {}
    for metric, sql in [
        ("demand", "SELECT region_code, period_utc, demand_mwh AS v FROM hourly_demand WHERE period_utc >= datetime(?, ?)"),
        ("battery", f"SELECT region_code, period_utc, generation_mwh AS v FROM hourly_generation "
                    f"WHERE fuel_type = '{config.BATTERY_CODE}' AND period_utc >= datetime(?, ?)"),
        ("solar", f"SELECT region_code, period_utc, generation_mwh AS v FROM hourly_generation "
                  f"WHERE fuel_type = '{config.SOLAR_CODE}' AND period_utc >= datetime(?, ?)"),
    ]:
        df = q(conn, sql, (latest, f"-{days} days"))
        for region, r in df.groupby("region_code"):
            out.setdefault(region, {})[metric] = {"t": r["period_utc"].tolist(),
                                                  "v": nulls(r["v"])}
    return out


def kpis(conn, monthly: pd.DataFrame, peaks: pd.DataFrame) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()
    first, last = one("SELECT MIN(period_utc), MAX(period_utc) FROM hourly_demand")
    hours = one("SELECT (SELECT COUNT(*) FROM hourly_demand) + (SELECT COUNT(*) FROM hourly_generation)")[0]
    runs_ok, runs_all = one("SELECT SUM(status = 'success'), COUNT(*) FROM pipeline_runs")
    last_run = one("SELECT status, finished_at FROM pipeline_runs ORDER BY run_id DESC LIMIT 1")
    growth = {}
    for region, r in monthly.groupby("region_code"):
        full = r[r["hours"] >= 24 * 28].dropna(subset=["discharge_mwh"])
        if len(full) >= 2 and full.iloc[0].discharge_mwh:
            growth[region] = {"from": full.iloc[0].month, "to": full.iloc[-1].month,
                              "pct": round(100 * (full.iloc[-1].discharge_mwh / full.iloc[0].discharge_mwh - 1))}
    peak_share = {reg: round(float(r["battery_share_pct"].mean()), 1) for reg, r in peaks.groupby("region_code")}
    return {
        "first": first, "last": last, "rows": int(hours or 0),
        "runs_ok": int(runs_ok or 0), "runs_all": int(runs_all or 0),
        "last_run": {"status": last_run[0], "at": last_run[1]} if last_run else None,
        "growth": growth, "peak_share": peak_share,
    }


def carbon_payload(hourly: pd.DataFrame, shift: pd.DataFrame) -> dict:
    """Intensity by local hour, plus the battery CO2-shift summary, per region."""
    out = {}
    for region in config.REGIONS:
        prof = carbon.local_hour_profile(hourly, region) if not hourly.empty else pd.DataFrame()
        row = shift[shift["region_code"] == region] if not shift.empty else pd.DataFrame()
        summary = None
        if not row.empty:
            r = row.iloc[0]
            clean = lambda v: None if pd.isna(v) else round(float(v), 1)
            summary = {"avoided_tonnes": clean(r["avoided_tonnes"]),
                       "charge_intensity": clean(r["charge_intensity"]),
                       "discharge_intensity": clean(r["discharge_intensity"])}
        cleanest = None
        if not prof.empty:
            best = prof.loc[prof["intensity_kg_per_mwh"].idxmin()]
            cleanest = {"hour": int(best["local_hour"]), "kg": float(best["intensity_kg_per_mwh"])}
        out[region] = {
            "hour": prof["local_hour"].tolist() if not prof.empty else [],
            "kg": nulls(prof["intensity_kg_per_mwh"]) if not prof.empty else [],
            "shift": summary, "cleanest": cleanest,
        }
    return out


def net_payload(net: pd.DataFrame) -> dict:
    """Battery share of each day's net peak, smoothed with a 7-day rolling mean, per region."""
    out = {}
    for region, r in net.groupby("region_code"):
        r = r.sort_values("day")
        share = pd.to_numeric(r["battery_share_of_net_peak_pct"], errors="coerce")
        roll = share.rolling(7, min_periods=3).mean().round(2)
        recent = share.tail(30).mean()
        out[region] = {"day": r["day"].tolist(), "share_7d": nulls(roll),
                       "recent_30d": None if pd.isna(recent) else round(float(recent), 1)}
    return out


def build(db_path: Path) -> Path:
    conn = sqlite3.connect(db_path)
    try:
        monthly = q(conn, (SQL_DIR / "01_battery_monthly_growth.sql").read_text())
        peaks = q(conn, (SQL_DIR / "03_peak_day_battery_share.sql").read_text())
        err = q(conn, (SQL_DIR / "04_forecast_error.sql").read_text())
        anomalies = q(conn, "SELECT region_code, period_utc, metric, kind, ROUND(value) AS value, "
                            "ROUND(expected) AS expected, ROUND(score, 1) AS score FROM anomalies "
                            "ORDER BY period_utc DESC LIMIT 500")
        hourly_ci = carbon.hourly_intensity(conn)
        shift = carbon.battery_shift(hourly_ci) if not hourly_ci.empty else pd.DataFrame()
        mix = q(conn, (SQL_DIR / "05_fuel_mix_monthly.sql").read_text())
        net = q(conn, (SQL_DIR / "06_net_peak_battery_share.sql").read_text())
        runs = q(conn, "SELECT run_id, started_at, window_start, window_end, rows_loaded, status, "
                       "error_message FROM pipeline_runs ORDER BY run_id DESC LIMIT 20")
        data = {
            "regions": {k: v for k, v in config.REGIONS.items()},
            "kpi": kpis(conn, monthly, peaks),
            "duck": duck_profiles(conn),
            "monthly": monthly.to_dict(orient="list"),
            "peaks": peaks.to_dict(orient="records"),
            "err": err[["region_code", "day", "mape_7d_avg_pct"]].to_dict(orient="list"),
            "recent": recent_series(conn),
            "anomalies": anomalies.to_dict(orient="records"),
            "runs": runs.to_dict(orient="records"),
            "carbon": carbon_payload(hourly_ci, shift),
            "mix": mix.to_dict(orient="list"),
            "net": net_payload(net),
        }
    finally:
        conn.close()

    payload = json.dumps(data, default=str, allow_nan=False).replace("</", "<\\/")  # safe inside <script>
    html = TEMPLATE.read_text().replace("__DATA__", payload)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html)
    return OUT


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    args = p.parse_args(argv)
    print(f"Dashboard written to {build(args.db)}")


if __name__ == "__main__":
    main()
