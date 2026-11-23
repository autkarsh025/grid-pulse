-- Q4: How accurate is EIA's day-ahead demand forecast, and when is it worst?
-- Daily MAPE (mean absolute percentage error) plus a 7-day rolling average via a window frame.
WITH daily AS (
    SELECT
        region_code,
        date(period_utc) AS day,
        AVG(ABS(forecast_mwh - demand_mwh) / demand_mwh) * 100 AS mape_pct,
        MAX(demand_mwh) AS peak_demand_mwh
    FROM hourly_demand
    WHERE demand_mwh > 0 AND forecast_mwh IS NOT NULL
    GROUP BY region_code, day
    HAVING COUNT(*) >= 22                       -- skip partial days at the edges
)
SELECT
    region_code,
    day,
    ROUND(mape_pct, 2) AS mape_pct,
    ROUND(AVG(mape_pct) OVER (
        PARTITION BY region_code ORDER BY day
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 2) AS mape_7d_avg_pct,
    ROUND(peak_demand_mwh) AS peak_demand_mwh
FROM daily
ORDER BY region_code, day;
