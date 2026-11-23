-- Q3: On the 10 highest-demand days per region, what share of the peak hour did batteries cover?
-- Step 1: find each day's peak hour with ROW_NUMBER. Step 2: rank days by that peak.
-- Note: "day" here is the UTC date. For a strict local-day version, convert in pandas.
WITH bat AS (
    SELECT region_code, period_utc, generation_mwh AS battery_mwh
    FROM hourly_generation
    WHERE fuel_type = 'BAT'
),
hourly AS (
    SELECT d.region_code, d.period_utc, date(d.period_utc) AS day,
           d.demand_mwh, COALESCE(b.battery_mwh, 0) AS battery_mwh
    FROM hourly_demand d
    LEFT JOIN bat b USING (region_code, period_utc)
    WHERE d.demand_mwh IS NOT NULL
),
daily_peak AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY region_code, day ORDER BY demand_mwh DESC) AS rn
    FROM hourly
),
ranked AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY region_code ORDER BY demand_mwh DESC, day) AS day_rank
    FROM daily_peak
    WHERE rn = 1
)
SELECT
    region_code,
    day_rank,
    day,
    period_utc                                        AS peak_hour_utc,
    ROUND(demand_mwh)                                 AS peak_demand_mwh,
    ROUND(battery_mwh)                                AS battery_at_peak_mwh,
    ROUND(100.0 * battery_mwh / demand_mwh, 1)        AS battery_share_pct
FROM ranked
WHERE day_rank <= 10
ORDER BY region_code, day_rank;
