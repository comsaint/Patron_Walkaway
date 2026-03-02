import duckdb
import pandas as pd

con = duckdb.connect()

query = """
WITH base AS (
    SELECT 
        session_id,
        CASE 
            WHEN casino_player_id IS NOT NULL AND trim(casino_player_id) != '' AND lower(trim(casino_player_id)) != 'null' 
            THEN trim(casino_player_id)
            ELSE CAST(player_id AS VARCHAR)
        END AS canonical_id,
        CASE 
            WHEN casino_player_id IS NOT NULL AND trim(casino_player_id) != '' AND lower(trim(casino_player_id)) != 'null' 
            THEN 1 ELSE 0 
        END AS is_rated,
        COALESCE(session_end_dtm, lud_dtm, session_start_dtm) AS sess_time
    FROM read_parquet('data/gmwds_t_session.parquet')
    WHERE is_manual = 0
      AND is_deleted = 0
      AND is_canceled = 0
),
patron_agg AS (
    SELECT 
        canonical_id,
        MAX(is_rated) AS is_rated_ever,
        COUNT(session_id) AS total_sessions,
        EXTRACT(EPOCH FROM (MAX(sess_time) - MIN(sess_time))) / 86400.0 AS history_span_days
    FROM base
    GROUP BY canonical_id
)
SELECT 
    COUNT(*) AS total_unrated,
    SUM(CASE WHEN total_sessions = 1 THEN 1 ELSE 0 END) AS cnt_1_session,
    SUM(CASE WHEN history_span_days = 0 THEN 1 ELSE 0 END) AS cnt_span_0,
    SUM(CASE WHEN history_span_days > 0 AND history_span_days < 1 THEN 1 ELSE 0 END) AS cnt_span_0_to_1,
    SUM(CASE WHEN history_span_days >= 1 THEN 1 ELSE 0 END) AS cnt_span_ge_1,
    SUM(CASE WHEN history_span_days >= 7 THEN 1 ELSE 0 END) AS cnt_span_ge_7,
    SUM(CASE WHEN history_span_days >= 30 THEN 1 ELSE 0 END) AS cnt_span_ge_30
FROM patron_agg
WHERE is_rated_ever = 0;
"""

df = con.execute(query).df()
for col in df.columns:
    print(f"{col}: {df[col][0]}")
