-- Q2 (duck curve): one row per region-hour with demand, solar and battery side by side.
-- Conditional aggregation pivots fuel types into columns. The chart script converts
-- period_utc to local time in pandas (SQLite has no time-zone support), then averages
-- by local hour of day to show when batteries charge (midday solar) and discharge (evening peak).
WITH gen AS (
    SELECT
        region_code,
        period_utc,
        SUM(CASE WHEN fuel_type = 'SUN' THEN generation_mwh END) AS solar_mwh,
        SUM(CASE WHEN fuel_type = 'BAT' THEN generation_mwh END) AS battery_mwh,
        SUM(generation_mwh)                                      AS total_gen_mwh
    FROM hourly_generation
    GROUP BY region_code, period_utc
)
SELECT
    d.region_code,
    d.period_utc,
    d.demand_mwh,
    g.solar_mwh,
    g.battery_mwh,
    g.total_gen_mwh
FROM hourly_demand AS d
LEFT JOIN gen AS g
       ON g.region_code = d.region_code AND g.period_utc = d.period_utc
WHERE d.period_utc >= datetime((SELECT MAX(period_utc) FROM hourly_demand), '-90 days')
ORDER BY d.region_code, d.period_utc;
