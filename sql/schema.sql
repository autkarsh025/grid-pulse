-- grid-pulse schema (SQLite). Every timestamp is stored in UTC as 'YYYY-MM-DD HH:MM:SS'.
-- Composite primary keys + upserts make every load idempotent: rerunning a day never duplicates rows.

CREATE TABLE IF NOT EXISTS regions (
    region_code  TEXT PRIMARY KEY,          -- e.g. 'CISO', 'ERCO'
    region_name  TEXT NOT NULL,
    timezone     TEXT NOT NULL              -- IANA zone, for local-time analysis
);

CREATE TABLE IF NOT EXISTS hourly_demand (
    region_code   TEXT NOT NULL REFERENCES regions(region_code),
    period_utc    TEXT NOT NULL,            -- start of the hour, UTC
    demand_mwh    REAL,                     -- actual demand
    forecast_mwh  REAL,                     -- EIA day-ahead demand forecast
    updated_at    TEXT NOT NULL,            -- when this row was last written
    PRIMARY KEY (region_code, period_utc)
);

CREATE TABLE IF NOT EXISTS hourly_generation (
    region_code     TEXT NOT NULL REFERENCES regions(region_code),
    period_utc      TEXT NOT NULL,
    fuel_type       TEXT NOT NULL,          -- 'SUN', 'WND', 'NG', 'BAT', ...
    generation_mwh  REAL,                   -- battery can be negative (charging)
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (region_code, period_utc, fuel_type)
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    window_start   TEXT NOT NULL,           -- first UTC date processed
    window_end     TEXT NOT NULL,           -- last UTC date processed
    rows_loaded    INTEGER DEFAULT 0,
    status         TEXT NOT NULL,           -- 'running' / 'success' / 'failed'
    error_message  TEXT
);

-- Derived table, rebuilt by src/anomalies.py after each load.
CREATE TABLE IF NOT EXISTS anomalies (
    region_code   TEXT NOT NULL REFERENCES regions(region_code),
    period_utc    TEXT NOT NULL,
    metric        TEXT NOT NULL,            -- 'demand' / 'battery' / 'solar'
    kind          TEXT NOT NULL,            -- 'spike' / 'drop' / 'flatline'
    value         REAL,
    expected      REAL,                     -- baseline median (NULL for flatlines)
    score         REAL,                     -- robust z-score, or run length in hours
    detected_at   TEXT NOT NULL,
    PRIMARY KEY (region_code, period_utc, metric, kind)
);

-- Speeds up the analysis queries that filter by fuel type over time.
CREATE INDEX IF NOT EXISTS idx_generation_fuel_period
    ON hourly_generation (fuel_type, period_utc);
