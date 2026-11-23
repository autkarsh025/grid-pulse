-- Q6: Batteries chase the NET peak, not the gross peak.
-- Net load = demand - solar - wind: what the rest of the fleet has to cover after renewables.
-- On each day, find the hour with the highest net load (ROW_NUMBER), then measure how much of
-- that hour's net load batteries supplied. Compare with 03 (gross peak) to see the difference.
-- Edge case that matters: EIA publishes generation a few hours AFTER demand. An hour with demand
-- but no generation yet would look like a huge net load with zero batteries, so it would win the
-- "peak" and drag the share toward 0. Only complete hours (INNER JOIN) on complete days count.
WITH gen AS (
    SELECT region_code, period_utc,
           SUM(CASE WHEN fuel_type = 'SUN' THEN generation_mwh ELSE 0 END) AS solar_mwh,
           SUM(CASE WHEN fuel_type = 'WND' THEN generation_mwh ELSE 0 END) AS wind_mwh,
           SUM(CASE WHEN fuel_type = 'BAT' THEN generation_mwh END)        AS battery_mwh
    FROM hourly_generation
    GROUP BY region_code, period_utc
),
hourly AS (
    SELECT d.region_code, d.period_utc, date(d.period_utc) AS day, d.demand_mwh,
           d.demand_mwh - COALESCE(g.solar_mwh, 0) - COALESCE(g.wind_mwh, 0) AS net_load_mwh,
           COALESCE(g.battery_mwh, 0) AS battery_mwh
    FROM hourly_demand d
    JOIN gen g USING (region_code, period_utc)          -- complete hours only
    WHERE d.demand_mwh IS NOT NULL
),
daily_net_peak AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY region_code, day ORDER BY net_load_mwh DESC) AS rn,
           COUNT(*)     OVER (PARTITION BY region_code, day)                            AS hours
    FROM hourly
)
SELECT region_code,
       day,
       period_utc                                       AS net_peak_hour_utc,
       ROUND(demand_mwh)                                AS demand_mwh,
       ROUND(net_load_mwh)                              AS net_load_mwh,
       ROUND(battery_mwh)                               AS battery_mwh,
       ROUND(100.0 * battery_mwh / NULLIF(net_load_mwh, 0), 1) AS battery_share_of_net_peak_pct
FROM daily_net_peak
WHERE rn = 1 AND hours >= 22                             -- complete days only
ORDER BY region_code, day;
