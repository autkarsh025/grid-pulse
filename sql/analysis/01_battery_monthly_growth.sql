-- Q1: How fast is battery storage discharge growing in each region?
-- Monthly battery discharge (positive values only = energy sent to the grid),
-- with month-over-month change via LAG and a 3-month rolling average.
WITH monthly AS (
    SELECT
        region_code,
        strftime('%Y-%m', period_utc)                          AS month,
        SUM(CASE WHEN generation_mwh > 0 THEN generation_mwh END) AS discharge_mwh,
        -SUM(CASE WHEN generation_mwh < 0 THEN generation_mwh END) AS charge_mwh,
        COUNT(*)                                               AS hours
    FROM hourly_generation
    WHERE fuel_type = 'BAT'
    GROUP BY region_code, month
)
SELECT
    region_code,
    month,
    ROUND(discharge_mwh) AS discharge_mwh,
    ROUND(charge_mwh)    AS charge_mwh,
    hours,
    ROUND(100.0 * (discharge_mwh - LAG(discharge_mwh) OVER w)
          / NULLIF(LAG(discharge_mwh) OVER w, 0), 1)         AS mom_change_pct,
    ROUND(AVG(discharge_mwh) OVER (
        PARTITION BY region_code ORDER BY month
        ROWS BETWEEN 2 PRECEDING AND CURRENT ROW))            AS rolling_3mo_discharge_mwh
FROM monthly
WINDOW w AS (PARTITION BY region_code ORDER BY month)
ORDER BY region_code, month;
