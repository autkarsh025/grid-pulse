# grid-pulse ⚡ U.S. grid & battery-storage data pipeline

An automated ETL pipeline that pulls hourly U.S. electricity data (demand, day-ahead forecast and generation by fuel, including battery storage) from the **EIA Open Data API**, validates it with data-quality gates, loads it into SQL with idempotent upserts, flags anomalies, estimates grid carbon intensity, and publishes a self-updating interactive dashboard on how batteries cover solar's evening drop-off in **California (CAISO)** and **Texas (ERCOT)**.

**[▶ Live dashboard](https://autkarsh025.github.io/grid-pulse/)**: auto-written takeaways, an **EV charging carbon calculator**, records, a year-long battery dispatch heatmap, round-trip efficiency, carbon intensity, anomaly explorer and run-by-run data-quality history, with light/dark mode and CSV downloads. · ![tests](https://github.com/autkarsh025/grid-pulse/actions/workflows/tests.yml/badge.svg)

![Average day in CAISO and ERCOT: demand, solar, and batteries charging midday and discharging in the evening](docs/img/duck_curve.png)

## In plain English

California and Texas get a huge share of their electricity from solar. Solar floods the grid at midday and disappears at sunset, right when demand peaks, so other power plants have to ramp up fast every evening. Giant batteries (like Tesla Megapacks) are becoming the fix: they **charge on midday solar and discharge after sunset**.

**This project measures how well that's working, automatically.** Every morning, with no human involved, it:
1. **Downloads** yesterday's hourly grid data (demand, and generation by source including batteries) from the U.S. Energy Information Administration.
2. **Checks** it: no missing hours, no duplicates, no impossible values. Bad data is rejected, not stored.
3. **Stores** it in a SQL database, safely, so re-running a day never creates duplicates.
4. **Analyzes** it: how much of the evening crunch batteries cover, how efficient they are, how clean the grid is hour by hour, when an EV is cleanest to charge, and which readings look abnormal.
5. **Publishes** the results to the [live dashboard](https://autkarsh025.github.io/grid-pulse/), and opens a GitHub issue if anything fails.

**Why it matters:** it's the same kind of system an energy company's data team runs on its own fleet data: automated ingestion, validation, storage, monitoring and analysis.

---

## Architecture

```
 EIA API v2 ──► extract.py ──► data/raw/*.json        (as received; replayable)
                                   │
                              transform.py            (pandas: UTC timestamps, types, dedupe)
                                   │
                              quality.py  ── FAIL ──► run logged as failed, nothing loaded
                                   │ pass
                              load.py ──► SQLite      (composite keys + upserts = idempotent)
                                   │
                              anomalies.py            (robust z-score spikes/drops, flatlines)
                              carbon.py               (hourly CO2 intensity, battery CO2 shift)
                                   │
              sql/analysis/*.sql ──► charts + docs/index.html dashboard

 Scheduling: Airflow DAG (dags/) or GitHub Actions daily cron (.github/workflows/daily.yml)
 Alerting:   a failed scheduled run automatically opens a GitHub issue
```

## Tech stack

Python · pandas · SQL (SQLite) · Airflow · GitHub Actions · pytest · Plotly.js · matplotlib

## Quick start

```bash
git clone https://github.com/autkarsh025/grid-pulse.git && cd grid-pulse
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then paste your free EIA key into .env

pytest -q                                              # 48 tests, no API key needed
python -m src.pipeline --start 2025-11-01 --end 2026-11-20   # backfill ~12 months
python -m analysis.run_analysis                        # SQL analyses -> charts in docs/img/
python -m analysis.build_dashboard                     # interactive dashboard -> docs/index.html
```

A GitHub Actions job runs the pipeline every morning and commits the refreshed dashboard, so the live page updates itself. Daily runs locally: `python -m src.pipeline` (defaults to yesterday, UTC). Re-run transform/load from saved raw files without calling the API: add `--from-raw`.

## Design decisions

**Idempotent loads.** Every table has a composite primary key (`region_code, period_utc[, fuel_type]`), and loads use `INSERT … ON CONFLICT DO UPDATE`. Re-running any day, or overlapping a backfill, never creates duplicates, and EIA's revisions overwrite older values. Each batch loads in one transaction, so it lands completely or not at all.

**Everything in UTC.** In local time, daylight-saving days have 23 or 25 hours, which creates gaps and duplicate timestamps. In UTC every day has exactly 24 hours, so completeness checks are simple. Local time is used only for analysis.

**Quality gate before load.** `quality.py` checks each batch before it touches the database: non-empty, unique keys, null share ≤ 2%, ≥ 95% of expected hours present, demand within sane ranges, and solar near zero at night (warning). Freshness is checked after scheduled runs. Failures stop the run and are recorded in `pipeline_runs`. Bad values are flagged, never silently filled.

**Anomaly detection.** Each hourly value is compared with the same local hour over the previous 28 days using a robust z-score (median and MAD, so one bad day can't distort the baseline). A non-zero reading stuck for 8+ hours is flagged as a flatline, which often means a frozen sensor or a stale feed.

**Every grid reports differently.** ERCOT publishes batteries as `BAT` (plus, briefly in late 2025, charging under `UES`); CAISO has no `BAT` code at all. A data-profiling step (`python -m analysis.run_analysis`) showed CAISO's `OTH` ("Other") swinging from about −7 GW at noon to +5 GW at 7pm, which is the signature of its battery fleet charging on solar and discharging at night. So the transform maps CAISO `OTH` → `BAT` (`config.FUEL_CODE_OVERRIDES`), with the caveat that it includes a small amount of other generation.

**Net peak, not gross peak.** Batteries don't target the hour of highest demand. They target the highest *net* load (demand minus solar and wind), the hard hour after sunset. `sql/analysis/06_net_peak_battery_share.sql` measures battery share at each day's net peak.

**Carbon intensity.** Each hour's grid intensity is generation-weighted CO₂ per MWh, using EIA's average emission rates for coal and gas (`src/config.py`). Batteries are excluded from the mix, since they re-deliver energy rather than make it. The battery "CO₂ shift" compares the intensity of the hours when batteries charge with the hours when they discharge. This uses average, not marginal, intensity: a deliberate, documented simplification.

**Battery analytics.** Round-trip efficiency = energy discharged ÷ energy charged, by month (`sql/analysis/07_battery_round_trip.sql`). "Full-power hours" = daily discharge ÷ the fleet's peak hourly output over the trailing 30 days, a utilization measure that works without installed-capacity data. A week × hour heatmap shows how dispatch shifts across the year.

**Observability.** Every data-quality check result for every run is stored in `quality_results` (even for failed runs) and shown as a run-by-run grid on the dashboard.

**EV charging carbon.** Grid intensity in kg CO₂/MWh equals g CO₂/kWh, so an EV's g CO₂/mile = hourly grid intensity × the car's kWh/mile (`src/ev.py`). Compared with EPA's gas-car baseline (8,887 g CO₂/gallon ÷ 22.2 mpg ≈ 400 g/mile), it shows how much cleaner an EV is depending on *when* it charges, plus a calculator and the cleanest charging hours of the past week.

**Alerting.** If the daily run fails, the workflow opens a GitHub issue with a link to the logs, or comments on the existing one, so a broken pipeline never fails silently.

**Raw data kept.** API responses are saved untouched before transforming, so a transform bug can be fixed and replayed without re-downloading.

## Findings

From one year of hourly EIA data (Nov 23, 2025 – Nov 22, 2026; ~166K records). The dashboard updates these daily.

1. **Batteries now cover a quarter of California's hardest hour.** CAISO batteries supplied **24.9%** of the daily net-peak load over the last 30 days, up from **20.3%** a year earlier. In ERCOT the share nearly doubled, from **7.1% to 12.6%**.
2. **They target the net peak, not the demand peak.** At the gross demand peak, batteries covered only **4.2%** in CAISO and **~0%** in ERCOT (top-10 demand days), because that peak happens while solar is still producing. They discharge after sunset, when demand minus solar and wind is highest.
3. **Fleet output is growing fast.** Monthly battery discharge rose **+38% in CAISO** and **+90% in ERCOT** from Dec 2025 to Oct 2026.
4. **Batteries move energy from clean hours to dirty ones.** CAISO batteries charge when the grid averages **76 kg CO₂/MWh** and discharge when it averages **176**. ERCOT: **253 → 373**. (Average-intensity estimate; see design notes.)
5. **Efficiency is right where lithium-ion should be.** Fleet round-trip efficiency averaged **87.4% in CAISO** and **81.3% in ERCOT** over the last three months.
6. **Two upstream data issues found and fixed:** CAISO reports no battery category to EIA (its batteries are inside "Other"), and ERCOT briefly reported battery charging as "unknown energy storage," which made one month's efficiency read an impossible 164%. Both are remapped in the transform, with tests.

## Repo layout

```
src/        extract, transform, quality, load, anomalies, carbon, battery, ev, pipeline (CLI), config
sql/        schema.sql + analysis queries (CTEs, window functions)
dags/       Airflow DAG: extract >> transform >> quality >> load >> detect_anomalies
analysis/   charts and dashboard builder
tests/      pytest: transform, quality, load idempotency, anomalies, carbon, end-to-end with mocked API
docs/       dashboard (served by GitHub Pages) and chart images
```

## What I'd add next

- Postgres in Docker, with partitioning by month
- Weather data (temperature) to explain demand spikes
- Incremental anomaly scoring instead of a full rebuild
- At larger scale: object storage for raw data (S3/Parquet), Spark or a warehouse for transforms, streaming ingestion

---
Data: U.S. Energy Information Administration, Hourly Electric Grid Monitor (EIA-930), via the EIA Open Data API v2.
