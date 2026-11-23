# 🌅 Sundown: build guide (your playbook)

(Repo name: `grid-pulse`; the GitHub URLs below keep that name.)

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

**Checkpoint:** `60 passed`.

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

**Know it cold: the CAISO battery discovery (your best interview story).**
The first real run showed no battery data for California, even though CAISO has more than 11 GW of batteries. The fuel profile in `run_analysis` showed why: CAISO doesn't report a `BAT` code, and its `OTH` ("Other") category averaged about −7,100 MWh at noon and +5,000 MWh at 7pm. Only batteries behave like that. EIA folds small categories into "Other" for confidentiality, so the transform now maps CAISO `OTH` → `BAT`. Tell it as: *"The data looked fine and passed every check, but a number didn't make sense, so I profiled it, found the cause, and fixed it at the transform layer with a documented mapping and a test."* That's the exact skill a data engineer is hired for.

**Bonus story (ERCOT):** after round-trip efficiency was added, ERCOT showed **164%** for Dec 2025, which is physically impossible. The fuel profile had already shown a short-lived `UES` ("unknown energy storage") code, all negative values, so it was pure charging. ERCOT had reported part of its charging there, so charging was undercounted. Mapping `UES` → `BAT` fixed it, but there's a subtle trap: when both codes report in the same hour, the transform has to **sum** them, not drop one as a "duplicate." There's a test for exactly that. A new metric exposed an upstream data problem. That's a great answer to "tell me about a bug you found."

**Know it cold (net peak):** batteries discharge at the *net* peak (demand minus solar and wind), not the gross demand peak. In ERCOT the gross peak is late afternoon, when solar is still producing, while batteries discharge after sunset. `06_net_peak_battery_share.sql` measures the share at each day's net peak.

**Know it cold (`battery.py`, `07_battery_round_trip.sql`):**
- **Round-trip efficiency (RTE)** = MWh discharged ÷ MWh charged. Real lithium-ion fleets return roughly 80–90%; losses are heat, inverters and auxiliary load. Monthly totals smooth out energy carried overnight. If CAISO's RTE looks off, the reason is that its "BAT" comes from EIA's "Other" category, which also holds a little non-battery output. Say that before they ask.
- **Full-power hours** = daily discharge ÷ the fleet's peak hourly discharge (trailing 30 days). EIA's hourly feed has no installed capacity, so the observed peak stands in for it. A 4-hour fleet doing one full cycle a day scores about 4.
- **Dispatch heatmap:** week × local hour, on a diverging color scale (blue = charging, red = discharging, gray = idle). It shows the midday charging band and the evening discharge band growing over the year.

**Know it cold (observability):** `quality_results` stores every check for every run, *including failed runs* (results are saved before `enforce` raises). That's how you'd answer "how often does this source send bad data?" Monitoring data quality over time, not just pass/fail today, is what production data teams do.

**Know it cold (`ev.py`, the EV tab):**
- **Unit trick:** kg CO₂/MWh = g CO₂/kWh (both numerator and denominator scale by 1,000). So EV g/mile = grid g/kWh × kWh/mile. Example: 150 g/kWh × 0.28 kWh/mi = 42 g/mile.
- **Gas baseline:** EPA says 8,887 g CO₂ per gallon; at 22.2 mpg that's about 400 g/mile.
- **Charging losses:** EPA's kWh/100 mi sticker ratings are measured at the wall, so losses are already included. Don't add them twice.
- **"Smart" charging** = the 4 cleanest hours of the average day, which in solar-heavy grids lands at midday. This is the case for workplace charging and for software that times charging to the grid.
- **Caveats to say yourself:** tailpipe-only vs. power-plant-only (both skip upstream), and average rather than marginal intensity.
- **Why Tesla cares:** the same logic drives scheduled/off-peak charging, Powerwall charge-from-solar, and fleet charging that follows grid signals.

**Know it cold (scaling to 7 grids + weather):**
- **Config-driven:** grids, time zones, demand ceilings and weather cities all live in `config.py`. Going from 2 grids to 7 changed configuration, not pipeline code. That's the design goal.
- **Two sources, one join:** grid data (EIA) and weather (Open-Meteo) are joined on `(region_code, period_utc)`. Both are stored in UTC, which is what makes the join trivial.
- **Enrichment vs. core:** if weather fails, the run continues (logged warning). If grid data fails a quality check, the run stops. Be ready to say *why* they're treated differently.
- **Archive lag:** Open-Meteo's archive trails real time by several days, so recent days come from its forecast API and are overwritten by archive values on later runs, thanks to upserts.
- **Heat/cold sensitivity:** a least-squares slope of daily peak demand vs. daily high (hot days ≥ 75°F) or low (cold days ≤ 45°F). It's simple, explainable and comparable across grids. A better model would fit a piecewise ("hockey stick") curve and control for weekday/weekend.
- **Reconciliation check:** demand ≈ net generation − interchange. Three independently reported numbers must agree; a gap means a broken feed. It's a warning, not an error, because small reporting gaps are normal.
- **Quarantine (third data story):** the first 7-grid run refused a whole month of New York data because of 6 impossible hourly values. Rejecting 700 good hours for 6 bad ones is too blunt, so impossible values now go to a `quarantine` table (with the reason) and the rest loads. The batch still fails if more than 1% needs quarantining, because at that point the feed itself is broken. This is the "dead-letter queue" pattern from streaming systems.
- **None vs. zero:** PJM and NYISO report no battery category, and NYISO reports no utility solar. The first version showed those as **0%**, which is wrong: it's *unknown*, not zero. Unreported data is now `None` and shows as a dash. Interviewers love this distinction (NULL semantics).
- **Migrations:** `load.migrate()` adds new columns to an existing database. `CREATE TABLE IF NOT EXISTS` won't, which is a classic gotcha.

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
- **Full rebuild:** Run workflow → set `backfill_days` (e.g. `365`) to rebuild the database from scratch. Use it after changing transform logic, since old rows were loaded with the old rules.
- **Alerting:** if a daily run fails, the workflow opens an issue titled "Daily pipeline failed" with a link to the logs (or comments on the open one). Turn on GitHub notifications and you'll get an email. Close the issue once you've fixed it.

**Know it cold:** GitHub runners start empty every time, so the workflow carries the SQLite file between runs using the Actions cache. That's fine for a demo, but caches can be evicted. In production you'd use a real database (Postgres) or object storage. Saying this unprompted shows judgment.

### 6b. Airflow on your laptop (REQUIRED for the Tesla posting: "demonstrated experience building data pipelines using Airflow")
You should be able to say you've run, broken and fixed this DAG yourself. About an hour.

1. **Install Docker Desktop** (docker.com) and start it.
2. Make sure your `.env` (Phase 0) has your EIA key. The Airflow container reads the same file.
3. Start Airflow:
   ```bash
   cd docker/airflow
   docker compose up
   ```
   The first start downloads the image (a few minutes). Wait for a log line saying the webserver is listening on 8080.
4. Get the admin password (in a second terminal):
   ```bash
   docker compose exec airflow cat /opt/airflow/standalone_admin_password.txt
   ```
5. Open **http://localhost:8080**, log in as `admin`, find **grid_pulse**, flip its toggle **on**, then press ▶ **Trigger DAG**.
6. Click the run and open the **Graph** view. Watch `extract → transform → quality → load → enrich_weather / detect_anomalies` turn green. Click a task, then **Logs**, and find the quality-check lines (`[PASS] demand_completeness ...`).
7. **Screenshot the green graph** → save as `docs/img/airflow_dag.png` → add it to the README under Architecture.
8. **Break it on purpose** (this is the part interviewers love):
   - Stop the container (Ctrl+C), change `EIA_API_KEY` in `.env` to `wrong`, run `docker compose up` again, and trigger the DAG.
   - Watch `extract` fail, wait 10 minutes, **retry** (the DAG sets `retries=2`), and fail again. Downstream tasks show `upstream_failed`; nothing bad was loaded.
   - Put the real key back, restart, select the failed run, and **Clear** the `extract` task. Airflow re-runs it and everything downstream.
9. **Backfill a few days** through Airflow:
   ```bash
   docker compose exec airflow airflow dags backfill grid_pulse -s 2026-11-01 -e 2026-11-05
   ```
   Because loads are idempotent, re-running days you already have changes nothing.
10. `docker compose down` when you're done.

**Know it cold (`dags/grid_pulse_dag.py`):**
- **DAG** = directed acyclic graph of tasks: `extract >> transform >> quality >> load >> [enrich_weather, detect_anomalies]`.
- Tasks pass **file paths** via XCom, not DataFrames. XCom lives in Airflow's metadata DB and is meant for small values.
- Each run processes its **data interval** (one UTC day). Backfills are just more intervals, and because loads are idempotent, re-running any day is safe.
- `max_active_runs=1` because SQLite allows only one writer at a time.
- `retries=2` with a 10-minute delay handles transient API failures at the task level. Clearing a task re-runs it and everything downstream.
- `standalone` mode bundles the scheduler, webserver and a SQLite metadata DB, which is fine for local work. In production, Airflow runs separate scheduler/webserver/worker services with Postgres and a Celery or Kubernetes executor.
- `tests/test_dag.py` checks the DAG's imports and wiring in CI without installing Airflow (a stub `airflow` package records what the DAG file does).

---

## Phase 6c · Spark (the posting asks for "working knowledge of Spark")
`spark/net_peak_spark.py` runs in CI and every morning. Read it top to bottom, then run it yourself once:
```bash
brew install openjdk@17          # Spark needs Java (macOS)
pip install -r requirements-spark.txt
python -m spark.net_peak_spark
```
It prints the export size, the Spark result, the validation and the partition-pruning plan. The latest result is also on the dashboard (Data quality tab → Spark cross-check). On the real data: **2,545 region-days compared, 0 mismatches.**

**Know it cold:**
- **Parquet** is a columnar file format: queries read only the columns they need, compressed. **Partitioning** by `region_code` and `month` creates folders like `region_code=CISO/month=2026-08/`, so a filter on those columns skips the other folders entirely. That's **partition pruning**, and you can show it in the plan (`PartitionFilters: [(region_code = CISO), (month = 2026-08)]`).
- **Lazy evaluation:** Spark transformations (`filter`, `join`, `withColumn`) only build a plan; nothing runs until an action (`toPandas`, `write`, `count`).
- **Window functions** in Spark mirror SQL: `Window.partitionBy("region_code", "day").orderBy(desc("net_load_mwh"))` + `row_number()`.
- **Shuffles:** `groupBy` and window functions move data between workers by key. They're the expensive part. `spark.sql.shuffle.partitions = 8` instead of the default 200 avoids hundreds of tiny tasks on small data.
- **Migration pattern:** port one step, run both versions, and require identical output before trusting the new one. That's exactly what the validation does, and the run fails on any mismatch.
- **Honesty line:** "~600K rows isn't big data. It runs on a laptop, but the patterns (partitioned Parquet, pruning, window functions, validation) are the same ones used at fleet scale."

## Phase 6d · Query optimization (for the "Query Optimization" team)
```bash
python -m analysis.explain_queries
```
It prints each analysis query's runtime and SQLite execution plan. **Know it cold:** `SCAN` reads every row; `SEARCH … USING INDEX` jumps to matching rows. The `(fuel_type, period_utc)` index lets battery- and solar-only queries skip ~500K rows. Primary keys double as indexes for joins. In Spark, the equivalents are partition pruning, predicate pushdown into Parquet, and avoiding unnecessary shuffles.

## Phase 6e · Tableau Public (optional, 30 min: the posting mentions Tableau/Power BI)
1. Download **Tableau Public** (free) from public.tableau.com.
2. On the live dashboard, click **⤓ CSV** on "How much of the evening crunch do batteries cover?" and on the generation-mix chart.
3. Tableau Public → **Connect → Text file** → open a CSV → build one line chart (day on columns, share on rows, region as color).
4. **Save to Tableau Public** and link it from the README. Now "Tableau" on your résumé is true.

---

## Phase 7 · Résumé + interview prep

### Résumé bullets (fill in only with your real numbers)
Tailored to Tesla's Energy data-engineering posting. Its keywords (pipelines, Airflow, Spark, data-quality checks, automated reporting, SQL) appear naturally. **Only keep "Airflow" after you've done Phase 6b yourself, and "Tableau" only after 6e.**

**Sundown: U.S. Power Grid & Battery Storage Data Pipeline** | Python, SQL, Airflow, Spark, GitHub Actions | github.com/autkarsh025/grid-pulse
- Built and deployed an automated ELT pipeline ingesting 580K+ hourly records from all 7 U.S. grid operators (EIA API) plus hourly weather into SQL, orchestrated with Airflow and scheduled daily via GitHub Actions; idempotent upserts, backfills, schema migrations and per-run logging.
- Automated data-quality checks before every load (completeness, ranges, energy-balance reconciliation, freshness) with row-level quarantine and auto-filed failure alerts; found and fixed three upstream data issues, including CAISO batteries hidden in EIA's "Other" category.
- Ported the core analysis to PySpark over a Parquet data lake partitioned by grid and month, validated day for day against SQL (2,545 region-days, 0 mismatches); 60 pytest tests in CI.
- Published a daily auto-updating dashboard showing batteries now cover ~25% of California's evening net peak (up from 20% a year earlier) and when EV charging is cleanest (27 vs. 400 g CO₂/mile for a gas car).

If you need to cut to 3 bullets, merge the last two. Pick at most 2–3 numbers per bullet; recruiters skim.

### Questions you'll get, with the gist of a good answer
1. **"Walk me through it."** → Use the architecture diagram, left to right, in 90 seconds. End with one finding.
2. **"What happens if you run the same day twice?"** → Nothing changes: composite key + upsert. That's what makes retries and backfills safe.
3. **"Why UTC?"** → DST days have 23/25 local hours: gaps and duplicate keys. UTC is always 24.
4. **"What if EIA changes the API?"** → Transform raises on missing columns, and quality checks catch empty, incomplete or out-of-range batches. The run is logged as failed, nothing bad gets loaded, and raw JSON lets me replay once fixed.
5. **"How do you handle missing data?"** → Flag, don't fill. Small gaps pass (≤ 2% nulls, ≥ 95% hours); bigger ones fail the run. Filling would make analysis look cleaner than reality.
6. **"How would this scale at Tesla (thousands of Megapacks, second-level data)?"** → Raw data in object storage as partitioned Parquet; Spark or a warehouse (BigQuery/Snowflake) for transforms; incremental, partition-level loads; streaming ingestion (Kafka) for telemetry; anomaly scoring per device against its own history and its fleet peers.
7. **"Why median/MAD instead of mean/std?"** → Robust to the very outliers you're trying to find.
8. **"How did you calculate carbon?"** → Generation-weighted average intensity with EIA emission factors. Batteries excluded from the mix. Then say the average-vs-marginal caveat yourself.
9. **"Tell me about a data problem you found."** → The CAISO `OTH` = batteries story above.
10. **"How do you know when it breaks?"** → Quality gates fail the run, every run is logged in `pipeline_runs`, and a failed scheduled run opens a GitHub issue automatically.
11. **"Why Spark if the data fits in SQLite?"** → To learn the patterns the team uses at scale and prove the port is correct: partitioned Parquet, pruning, window functions, validated against SQL with 0 mismatches. Say plainly that 600K rows isn't big data.
12. **"Show me Airflow."** → The DAG's tasks, XCom passing file paths, retries, clearing a failed task, backfill. You ran and broke it yourself in Phase 6b.
13. **"How would you optimize a slow query?"** → Read the plan (`EXPLAIN`), look for full scans that should be index searches, filter early, avoid unnecessary sorts and shuffles, partition by the columns you filter on.
14. **"What would you do differently?"** → Postgres instead of SQLite, Spark for the full pipeline (not one step), incremental anomaly scoring, Slack alerts, a proper demand forecast.

---

## Final checklist before you send the link anywhere
- [ ] `pytest` green locally and the badge is green on GitHub
- [ ] Real data backfilled; `pipeline_runs` all success (or failures explained)
- [ ] Battery fuel code verified in the EIA browser
- [ ] Findings in the README use only real numbers
- [ ] Charts in `docs/img/` and dashboard live on Pages
- [ ] Airflow run, broken and fixed by you (Phase 6b); screenshot in the README
- [ ] Spark job run once on your own machine (Phase 6c)
- [ ] Tableau Public chart linked (optional, Phase 6e)
- [x] GitHub username filled in everywhere
- [ ] `.env` and `data/` NOT in the repo
- [ ] You can explain every file out loud
- [ ] Résumé bullet + GitHub link added
