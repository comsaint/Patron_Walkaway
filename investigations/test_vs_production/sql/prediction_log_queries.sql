-- Prediction log health checks (skeleton)

-- Latest write timestamp and total rows
SELECT MAX(scored_at) AS max_scored_at, COUNT(*) AS row_count
FROM prediction_log;

-- Recent throughput (example: last 30 minutes)
-- Adjust datetime function per runtime environment if needed.
SELECT COUNT(*) AS rows_last_30m
FROM prediction_log
WHERE scored_at >= datetime('now', '-30 minutes');
