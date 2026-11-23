# grid-pulse build guide (your playbook)

This is your path from "zip on my laptop" to "live project on my résumé." Work through it in order. Each phase ends with a **checkpoint** (proof it works) and a **know it cold** list: the things you should be able to explain without looking. Interviewers will open your repo and ask about any line of it, so read every file as you go.

Expected time: about one focused weekend.

> You can delete this file before you push to GitHub, or keep it. It's your notes, not part of the product.

---

## Phase 0 · Setup (30 min)

1. **Get an EIA API key:** register at https://www.eia.gov/opendata/ (free, emailed instantly).
2. **Create a GitHub repo** named `grid-pulse` (public, no README, since you already have one).
3. Unzip the project, then in a terminal:
   ```bash
   cd grid-pulse
   python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   cp .env.example .env        # open .env, paste your key
   pytest -q
   ```
4. (Already done: your username `autkarsh025` is filled in throughout.)

**Checkpoint:** `36 passed`.

**Know it cold:** why the key lives in `.env` and `.env` is in `.gitignore` (secrets never go in code or git history; a leaked key on GitHub gets scraped within minutes).

---

## Phase 1 · Explore the API before trusting the code (45 min)

APIs change. Confirm the code's assumptions match today's API.

1. Open the API browser: https://www.eia.gov/opendata/browser/electricity/rto
2. Open **region-data**. Check that the facets are `respondent` and `type`, that `CISO`/`ERCO` exist, and that type codes `D` (demand) and `DF` (day-ahead forecast) exist.
3. Open **fuel-type-data**. Check the `fueltype` facet's codes. **Confirm the battery code.** The code assumes `BAT` (`src/config.py → BATTERY_CODE`). If EIA uses something else, change it there and in the two `'BAT'` strings in `sql/analysis/`.
4. Run a one-day test:
   ```bash
   python -m src.pipeline --start 2026-11-15
   ```
5. Open `data/raw/demand/2026-11-15_2026-11-15.json` and look at a few rows. Fields should include `period` (like `2026-11-15T08`), `respondent`, `type`, `value`, `value-units`.

**Checkpoint:** log ends with `done: N rows loaded`, and all quality checks show `[PASS]`.

**If something breaks:**
- `missing columns` → EIA renamed a field. Compare the raw JSON with `transform.py → _base_clean`.
- `demand_completeness` fails → EIA hasn't published the full day yet. Try a date a few days earlier.
- HTTP 403 → wrong or missing key.

**Know it cold (`extract.py`):**
- **Pagination:** EIA returns at most 5,000 rows per request. The loop advances `offset` until it has `total` rows.
- **Retries with exponential backoff:** 429 (rate limit) and 5xx errors are retried after 2s, 4s, 8s… Transient failures shouldn't kill a nightly job.
- **Why save raw JSON:** replayability. If you find a transform bug, fix it and run `--from-raw` without hitting the API again. This is the "bronze layer" idea from data lakes.

---

## Phase 2 · Backfill a year of history (30 min, mostly waiting)

```bash
python -m src.pipeline --start 2025-11-01 --end 2026-11-20
```
The range is split into 30-day chunks, and each chunk is one row in `pipeline_runs`. Run it a second time: the row counts **don't change**. That's idempotency working.

Look inside the database (VS Code "SQLite Viewer" extension, or):
```bash
sqlite3 data/grid_pulse.db
sqlite> SELECT * FROM pipeline_runs;
sqlite> SELECT fuel_type, COUNT(*) FROM hourly_generation GROUP BY 1;
sqlite> SELECT * FROM anomalies ORDER BY ABS(score) DESC LIMIT 10;
```

**Checkpoint:** roughly 17,500 demand rows (2 regions × ~8,760 hours) and all runs `success`. If a chunk failed, read its `error_message`. The pipeline refuses bad data on purpose. Decide whether it's a real data problem (fine, note it in the README) or a threshold that needs tuning in `config.py`.

**Know it cold (`transform.py`, `load.py`, `schema.sql`):**
- **Transform vs. quality split:** transform fixes *shape and types* (parse timestamps, cast strings to numbers, drop exact duplicates). It never "fixes" suspicious values; it turns them into NaN and lets quality decide. Silent fixing hides problems.
- **UTC:** on daylight-saving days, local time has 23 or 25 hours, which creates gaps and duplicate timestamps. UTC always has 24. The test `test_dst_day_still_has_24_utc_hours` proves it.
- **Long → wide pivot** for demand: EIA sends one row per (region, hour, type); the table wants one row per (region, hour) with `demand_mwh` and `forecast_mwh` columns.
- **Idempotent upsert:** the composite primary key defines "same row," and `ON CONFLICT DO UPDATE` replaces it instead of duplicating it. `COALESCE` keeps an old value if a re-pull returns NULL, but a real new value (an EIA revision) wins.
- **One transaction per batch:** demand and generation load together or not at all (`test_failed_batch_rolls_back`).
- **Negative battery values** = charging. Kept on purpose (`test_battery_negatives_are_preserved`).

---

## Phase 3 · Quality gates, anomalies and tests (1 hr: read, break things, re-run)

The best way to understand the tests is to break the code and watch them fail:
- In `load.py`, change `ON CONFLICT ... DO UPDATE` to a plain `INSERT` → run `pytest` → the idempotency test fails. Undo.
- In `quality.py`, set `MIN_COMPLETENESS` to `0.0` → `test_missing_hours_fail_completeness` fails. Undo.

**Know it cold (`quality.py`):**
| Check | Why | Severity |
|---|---|---|
| not empty | API returned nothing → something's wrong upstream | error |
| unique keys | duplicates would double-count energy | error |
| null share ≤ 2% | a few gaps are normal; many means a broken feed | error |
| completeness ≥ 95% | missing hours skew every average | error |
| demand range | negative or impossible demand = bad data | error |
| solar at night | sensor/unit errors show up as night solar | warn |
| freshness | catches a pipeline that silently stopped | error (scheduled runs) |

Quality runs **before** the load ("validate, then publish"), so bad data never reaches the tables people query. Every run, pass or fail, is logged in `pipeline_runs`.

**Know it cold (`anomalies.py`):** this is your "Tesla root-cause analysis" talking point.
- **Robust z-score:** compare each value with the *same local hour* over the past 28 days, because a 7pm reading should be judged against other 7pm readings, not against 3am. Median and MAD instead of mean and standard deviation: one huge outlier drags a mean but barely moves a median. `0.6745 × (x − median) / MAD` puts the score on the same scale as a normal z-score. Flag at |z| ≥ 4.
- **No look-ahead:** the baseline uses `shift(1)`, so a spike can't inflate its own baseline and hide.
- **Flatline:** a non-zero value identical for 8+ hours usually means a frozen sensor or a stale feed. Zeros are excluded because idle batteries at night are normal.
- **Full refresh:** `anomalies` is a derived table, rebuilt every run. The fact tables load incrementally. Know the tradeoff: a full refresh is simple and always consistent, but at Tesla scale you'd score incrementally.

**Know it cold (`carbon.py`):**
- **Intensity** = Σ(generation × emission factor) / Σ generation for each hour. Factors come from EIA: coal ≈ 2,257 lb/MWh, gas ≈ 976 lb/MWh, renewables and nuclear 0.
- **Why batteries are excluded:** a battery has no fuel. Its emissions belong to whatever charged it. Counting its discharge as zero-carbon generation would double-count the solar that charged it.
- **Battery CO₂ shift** = (discharge MWh × intensity when discharging) − (charge MWh × intensity when charging). Round-trip losses are included, because you charge more MWh than you get back.
- **The caveat to raise yourself:** this uses *average* intensity. The rigorous measure is *marginal* intensity (which plant actually ramps when the battery acts), which needs dispatch data. Naming a limitation before the interviewer does is a strong signal.

---

## Phase 4 · Analysis and findings (1.5 hrs)

```bash
python -m analysis.run_analysis
```
This runs the four SQL files, saves CSVs to `analysis/output/`, charts to `docs/img/`, and prints **headline numbers**. Open the charts. Then write 2–3 findings in the README's Findings section, each **one sentence with a real number**, for example (made-up shape, use YOUR numbers):
> "CAISO batteries discharged X% more energy in Oct 2026 than in Nov 2025, and on the 10 highest-demand days they supplied an average of Y% of the peak hour."

Look for one **surprising** thing (a weird anomaly, a forecast-error spike during a heat wave) and dig into it. Interviewers love "here's something I didn't expect, and here's what I found."

**Know it cold (the SQL):**
- `01`: CTE + `CASE` inside `SUM` (conditional aggregation) + `LAG` for month-over-month change + rolling average with `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW`, using a named `WINDOW`.
- `02`: pivoting fuel types into columns with conditional aggregation, then a `LEFT JOIN` so demand hours without generation still appear. Time-zone conversion happens in pandas because SQLite has no time-zone support.
- `03`: two-stage ranking. `ROW_NUMBER() OVER (PARTITION BY region, day ...)` finds each day's peak hour, then a second `ROW_NUMBER` ranks those days. Know `ROW_NUMBER` vs. `RANK` vs. `DENSE_RANK` (ties!).
- `05`: monthly fuel shares using `SUM(...) OVER (PARTITION BY region, month)`, a window total with no self-join.
- `04`: MAPE (mean absolute percentage error) per day with a 7-day rolling average. `NULLIF`/filters avoid division by zero.

---

## Phase 5 · Dashboard and GitHub Pages (45 min)

```bash
python -m analysis.build_dashboard
open docs/index.html          # Windows: start docs\index.html
```
Click through the region and metric toggles and hover the charts. Then push and publish:

```bash
git init && git add . && git commit -m "grid-pulse: EIA grid + battery storage pipeline"
git branch -M main
git remote add origin https://github.com/autkarsh025/grid-pulse.git
git push -u origin main
```
- GitHub → repo **Settings → Pages** → Source: *Deploy from a branch* → `main` / `/docs` → Save. In a minute or two, your dashboard is live at `https://autkarsh025.github.io/grid-pulse/`.
- **Before pushing, run `git status` and make sure `.env` and `data/` are NOT listed.**

`docs/index.html` and `docs/img/*.png` are committed on purpose (they're the published output). The database isn't (it's rebuilt by the pipeline).

**Know it cold:** the dashboard is a static file. Python queries the DB and embeds the results as JSON, and Plotly.js draws them in the browser. No server, so free hosting and nothing to keep running.

---

## Phase 6 · Scheduling (1–3 hrs)

### 6a. GitHub Actions (required, 20 min)
- Repo **Settings → Secrets and variables → Actions → New repository secret**: name `EIA_API_KEY`, value = your key.
- **Actions** tab → `daily-pipeline` → **Run workflow** to test it now. The first run backfills a year (the cache starts empty), and after that it runs daily at 14:00 UTC, loads yesterday, rebuilds the charts and dashboard, and commits them. **Your live dashboard updates itself every day.**
- `tests.yml` runs pytest on every push. That's the green badge in your README.
- **Alerting:** if a daily run fails, the workflow opens an issue titled "Daily pipeline failed" with a link to the logs (or comments on the open one). Turn on GitHub notifications and you'll get an email. Close the issue once you've fixed it.

**Know it cold:** GitHub runners start empty every time, so the workflow carries the SQLite file between runs using the Actions cache. That's fine for a demo, but caches can be evicted. In production you'd use a real database (Postgres) or object storage. Saying this unprompted shows judgment.

### 6b. Airflow (strongly recommended: Tesla's posting names it)
1. Install Docker Desktop.
2. Follow Airflow's official "Running Airflow in Docker" quick start (download its `docker-compose.yaml`).
3. Mount this repo into the containers. In the compose file's `volumes:`, add `- /path/to/grid-pulse:/opt/airflow/grid-pulse` and change the dags volume to `- /path/to/grid-pulse/dags:/opt/airflow/dags`. Set `GRID_PULSE_DATA_DIR=/opt/airflow/grid-pulse/data` and `EIA_API_KEY` in the compose `environment:`.
4. `docker compose up`, open http://localhost:8080, turn on `grid_pulse`, and trigger a run.
5. **Screenshot the graph view with all 5 tasks green**, save it as `docs/img/airflow_dag.png`, and add it to the README.

**Know it cold (`dags/grid_pulse_dag.py`):**
- **DAG** = directed acyclic graph of tasks: `extract >> transform >> quality >> load >> detect_anomalies`.
- Tasks pass **file paths** via XCom, not DataFrames. XCom lives in Airflow's metadata DB and is meant for small values.
- Each run processes its **data interval** (one UTC day), so backfills are `airflow dags backfill grid_pulse -s 2026-01-01 -e 2026-11-20`, and because loads are idempotent, re-running any day is safe.
- `max_active_runs=1` because SQLite allows only one writer at a time.
- `retries=2` with a 10-minute delay handles transient API failures at the task level.

---

## Phase 7 · Résumé + interview prep

### Résumé bullets (fill in only with your real numbers)
**Grid-Pulse: U.S. Grid & Battery Storage Data Pipeline** | Python, SQL, Airflow, pandas | [github link]
- Built an automated ETL pipeline ingesting [N]K+ hourly records from the EIA API across 2 grid regions into SQL, with idempotent upserts, backfills and daily scheduled runs via Airflow and GitHub Actions.
- Implemented pre-load data-quality gates (completeness, duplicates, nulls, ranges, freshness) and robust z-score anomaly detection and automatic failure alerts, with 36 pytest tests and per-run logging.
- Analyzed battery dispatch against solar output and demand peaks, finding [real finding with a number]; published an interactive dashboard on GitHub Pages.

[N] = `SELECT (SELECT COUNT(*) FROM hourly_demand) + (SELECT COUNT(*) FROM hourly_generation);`

### Questions you'll get, with the gist of a good answer
1. **"Walk me through it."** → Use the architecture diagram, left to right, in 90 seconds. End with one finding.
2. **"What happens if you run the same day twice?"** → Nothing changes: composite key + upsert. That's what makes retries and backfills safe.
3. **"Why UTC?"** → DST days have 23/25 local hours: gaps and duplicate keys. UTC is always 24.
4. **"What if EIA changes the API?"** → Transform raises on missing columns, and quality checks catch empty, incomplete or out-of-range batches. The run is logged as failed, nothing bad gets loaded, and raw JSON lets me replay once fixed.
5. **"How do you handle missing data?"** → Flag, don't fill. Small gaps pass (≤ 2% nulls, ≥ 95% hours); bigger ones fail the run. Filling would make analysis look cleaner than reality.
6. **"How would this scale at Tesla (thousands of Megapacks, second-level data)?"** → Raw data in object storage as partitioned Parquet; Spark or a warehouse (BigQuery/Snowflake) for transforms; incremental, partition-level loads; streaming ingestion (Kafka) for telemetry; anomaly scoring per device against its own history and its fleet peers.
7. **"Why median/MAD instead of mean/std?"** → Robust to the very outliers you're trying to find.
8. **"How did you calculate carbon?"** → Generation-weighted average intensity with EIA emission factors. Batteries excluded from the mix. Then say the average-vs-marginal caveat yourself.
9. **"How do you know when it breaks?"** → Quality gates fail the run, every run is logged in `pipeline_runs`, and a failed scheduled run opens a GitHub issue automatically.
10. **"What would you do differently?"** → Postgres instead of SQLite, incremental anomaly scoring, weather features, alerting (Slack/email) on failed runs.

---

## Final checklist before you send the link anywhere
- [ ] `pytest` green locally and the badge is green on GitHub
- [ ] Real data backfilled; `pipeline_runs` all success (or failures explained)
- [ ] Battery fuel code verified in the EIA browser
- [ ] Findings in the README use only real numbers
- [ ] Charts in `docs/img/` and dashboard live on Pages
- [ ] Airflow screenshot in the README (if done)
- [x] GitHub username filled in everywhere
- [ ] `.env` and `data/` NOT in the repo
- [ ] You can explain every file out loud
- [ ] Résumé bullet + GitHub link added
