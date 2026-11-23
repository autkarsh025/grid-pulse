-- Q5: How is each region's generation mix changing? Monthly share of generation by fuel.
-- A window SUM over the partition gives the month's total without a second query or self-join.
-- Batteries and negative values (charging, pumped storage) are excluded: they move energy, they don't make it.
WITH monthly AS (
    SELECT region_code,
           strftime('%Y-%m', period_utc) AS month,
           fuel_type,
           SUM(generation_mwh)           AS mwh
    FROM hourly_generation
    WHERE fuel_type <> 'BAT' AND generation_mwh > 0
    GROUP BY region_code, month, fuel_type
)
SELECT region_code,
       month,
       fuel_type,
       ROUND(mwh) AS mwh,
       ROUND(100.0 * mwh / SUM(mwh) OVER (PARTITION BY region_code, month), 2) AS share_pct
FROM monthly
ORDER BY region_code, month, fuel_type;
