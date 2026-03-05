-- Scorer poll example queries (one cycle, default --lookback-hours 8)
-- Use on production ClickHouse to test data size and latency per poll.
--
-- Parameters (HK time):
--   end       = now (e.g. 2026-03-05 10:00:00)
--   start     = end - 8h
--   bet_avail = end - 1 min  (BET_AVAIL_DELAY_MIN)
--   sess_avail = end - 7 min (SESSION_AVAIL_DELAY_MIN)
--
-- Replace datetime literals to match your DB timezone (e.g. UTC if columns are UTC).

-- =============================================================================
-- 1. BETS (8h window, 12 columns)
-- =============================================================================

SELECT
    bet_id,
    is_back_bet,
    base_ha,
    bet_type,
    payout_complete_dtm,
    session_id,
    player_id,
    table_id,
    position_idx,
    wager,
    payout_odds,
    status
FROM GDP_GMWDS_Raw.t_bet FINAL
WHERE payout_complete_dtm >= toDateTime('2026-03-05 02:00:00', 'Asia/Hong_Kong')   -- start (end - 8h)
  AND payout_complete_dtm <= toDateTime('2026-03-05 09:59:00', 'Asia/Hong_Kong')   -- bet_avail (end - 1 min)
  AND payout_complete_dtm IS NOT NULL
  AND wager > 0
  AND player_id != -1;


-- =============================================================================
-- 2. SESSIONS (~3d window for rolling context, 8 columns)
-- =============================================================================

WITH deduped AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY session_id
               ORDER BY lud_dtm DESC, __etl_insert_Dtm DESC
           ) AS rn
    FROM GDP_GMWDS_Raw.t_session
    WHERE session_start_dtm >= toDateTime('2026-03-05 02:00:00', 'Asia/Hong_Kong') - INTERVAL 2 DAY   -- start - 2d
      AND session_start_dtm <= toDateTime('2026-03-05 10:00:00', 'Asia/Hong_Kong') + INTERVAL 1 DAY   -- end + 1d
      AND is_deleted = 0
      AND is_canceled = 0
      AND is_manual = 0
)
SELECT
    session_id,
    table_id,
    player_id,
    CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END AS casino_player_id,
    session_start_dtm,
    session_end_dtm,
    lud_dtm,
    COALESCE(session_end_dtm, lud_dtm) AS session_avail_dtm
FROM deduped
WHERE rn = 1
  AND COALESCE(session_end_dtm, lud_dtm) <= toDateTime('2026-03-05 09:53:00', 'Asia/Hong_Kong');   -- sess_avail (end - 7 min)
