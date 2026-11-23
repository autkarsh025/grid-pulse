"""Run the SQL analyses, save result tables as CSV, and draw the README charts.

Usage:  python -m analysis.run_analysis            (uses data/grid_pulse.db)
Output: analysis/output/*.csv and docs/img/*.png
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # draw to files, no window needed
import matplotlib.pyplot as plt
import pandas as pd

from src import carbon, config

SQL_DIR = config.ROOT / "sql" / "analysis"
OUT_DIR = config.ROOT / "analysis" / "output"
IMG_DIR = config.ROOT / "docs" / "img"


def query(conn: sqlite3.Connection, name: str) -> pd.DataFrame:
    return pd.read_sql_query((SQL_DIR / name).read_text(), conn)


def chart_duck_curve(wide: pd.DataFrame) -> None:
    """Average demand, solar and battery by local hour of day, one panel per region."""
    regions = [r for r in config.REGIONS if r in set(wide["region_code"])]
    fig, axes = plt.subplots(1, len(regions), figsize=(6 * len(regions), 4.2), squeeze=False)
    for ax, region in zip(axes[0], regions):
        r = wide[wide["region_code"] == region].copy()
        r["local_hour"] = (pd.to_datetime(r["period_utc"], utc=True)
                           .dt.tz_convert(config.REGION_TZ[region]).dt.hour)
        prof = r.groupby("local_hour")[["demand_mwh", "solar_mwh", "battery_mwh"]].mean() / 1000
        ax.plot(prof.index, prof["demand_mwh"], label="Demand", color="#1f2937", lw=2)
        ax.plot(prof.index, prof["solar_mwh"], label="Solar", color="#f59e0b", lw=2)
        ax.bar(prof.index, prof["battery_mwh"], label="Battery (+ discharge / − charge)",
               color="#10b981", alpha=0.7)
        ax.axhline(0, color="#9ca3af", lw=0.8)
        ax.set_title(f"{region}: average day, last 90 days")
        ax.set_xlabel("Local hour")
        ax.set_ylabel("GW (avg MWh per hour / 1000)")
        ax.set_xticks(range(0, 24, 3))
        ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(IMG_DIR / "duck_curve.png", dpi=150)
    plt.close(fig)


def chart_battery_growth(monthly: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(9, 4))
    for region, r in monthly.groupby("region_code"):
        ax.plot(r["month"], r["discharge_mwh"] / 1e6, marker="o", label=region)
    ax.set_title("Monthly battery discharge")
    ax.set_ylabel("TWh")
    ax.tick_params(axis="x", rotation=45)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(IMG_DIR / "battery_growth.png", dpi=150)
    plt.close(fig)


def chart_forecast_error(err: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(9, 4))
    for region, r in err.groupby("region_code"):
        ax.plot(pd.to_datetime(r["day"]), r["mape_7d_avg_pct"], label=region)
    ax.set_title("Day-ahead demand forecast error (7-day rolling MAPE)")
    ax.set_ylabel("% error")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(IMG_DIR / "forecast_error.png", dpi=150)
    plt.close(fig)


def print_headlines(monthly: pd.DataFrame, peaks: pd.DataFrame, err: pd.DataFrame) -> None:
    """Numbers to turn into one-sentence findings in the README. Check them before using them."""
    print("\n=== Headline numbers (verify before putting in your README) ===")
    for region, r in monthly.groupby("region_code"):
        r = r.dropna(subset=["discharge_mwh"])
        full = r[r["hours"] >= 24 * 28]  # skip partial months at the edges
        if len(full) >= 2:
            first, last = full.iloc[0], full.iloc[-1]
            growth = 100 * (last.discharge_mwh / first.discharge_mwh - 1) if first.discharge_mwh else float("nan")
            print(f"{region}: battery discharge {first.month} -> {last.month}: "
                  f"{first.discharge_mwh:,.0f} -> {last.discharge_mwh:,.0f} MWh ({growth:+.0f}%)")
    for region, r in peaks.groupby("region_code"):
        print(f"{region}: on its top-10 demand days, batteries covered on average "
              f"{r['battery_share_pct'].mean():.1f}% of the peak hour (max {r['battery_share_pct'].max():.1f}%)")
    for region, r in err.groupby("region_code"):
        worst = r.loc[r["mape_pct"].idxmax()]
        print(f"{region}: average forecast MAPE {r['mape_pct'].mean():.2f}%, "
              f"worst day {worst.day} at {worst.mape_pct:.1f}%")


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    args = p.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    IMG_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    try:
        monthly = query(conn, "01_battery_monthly_growth.sql")
        wide = query(conn, "02_hourly_wide.sql")
        peaks = query(conn, "03_peak_day_battery_share.sql")
        err = query(conn, "04_forecast_error.sql")
    finally:
        conn.close()

    for name, df in [("battery_monthly", monthly), ("peak_days", peaks), ("forecast_error", err)]:
        df.to_csv(OUT_DIR / f"{name}.csv", index=False)
    chart_duck_curve(wide)
    chart_battery_growth(monthly)
    chart_forecast_error(err)
    print_headlines(monthly, peaks, err)
    conn = sqlite3.connect(args.db)
    try:
        shift = carbon.battery_shift(carbon.hourly_intensity(conn))
    finally:
        conn.close()
    shift.to_csv(OUT_DIR / "battery_co2_shift.csv", index=False)
    for _, r in shift.iterrows():
        if r["charge_mwh"] > 0:
            print(f"{r.region_code}: batteries charged at an average {r.charge_intensity:,.0f} kg CO2/MWh and "
                  f"discharged at {r.discharge_intensity:,.0f}, a net {r.avoided_tonnes:,.0f} t CO2 shifted "
                  f"(average-intensity method)")
    print(f"\nCharts saved to {IMG_DIR}, tables to {OUT_DIR}")


if __name__ == "__main__":
    main()
