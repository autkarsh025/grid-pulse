-- Q7: Battery round-trip efficiency (RTE) by month: energy delivered / energy stored.
-- A lithium-ion fleet typically returns ~80-90% of what it absorbs; the rest is lost as heat
-- and auxiliary load. Monthly totals smooth over energy carried from one day into the next.
-- Caveat: CAISO "BAT" comes from EIA's "Other" category, so it includes a little non-battery output.
WITH monthly AS (
    SELECT region_code,
           strftime('%Y-%m', period_utc)                               AS month,
           SUM(CASE WHEN generation_mwh > 0 THEN generation_mwh END)   AS discharged_mwh,
           -SUM(CASE WHEN generation_mwh < 0 THEN generation_mwh END)  AS charged_mwh,
           COUNT(*)                                                    AS hours
    FROM hourly_generation
    WHERE fuel_type = 'BAT'
    GROUP BY region_code, month
)
SELECT region_code,
       month,
       ROUND(discharged_mwh) AS discharged_mwh,
       ROUND(charged_mwh)    AS charged_mwh,
       ROUND(100.0 * discharged_mwh / NULLIF(charged_mwh, 0), 1) AS round_trip_pct,
       hours
FROM monthly
ORDER BY region_code, month;
