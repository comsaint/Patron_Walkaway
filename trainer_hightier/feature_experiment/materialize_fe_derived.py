"""Materialize ``fe__*`` window features from cleaned bet (DuckDB, read-only ingest).

Windows mirror :mod:`trainer_hightier.utils.trial_bet_behavior_1h` for most
features: ``RANGE ... PRECEDING`` excludes the current row. Outcome momentum
columns ``fe__outcome__casino_win_sum__*`` and ``fe__outcome__casino_win_to_theo_ratio__w1h``
use a peer-inclusive window (``CURRENT ROW``) minus the scored bet so same-``pcd``
siblings contribute (PIT-safe when ETL is visible at score time). Patron-level
windows partition by ``canonical_id`` (aligned with walkaway labels). The scan
universe is all ``player_id`` rows that map to any ``canonical_id`` seen on
training ``bet_id`` rows (so multi-card patrons include history under every
linked ``player_id``).

Time-zone convention (verified against cleaned bet Parquet):
- ``payout_complete_dtm`` is stored as ``timestamp[us, tz=UTC]``; window arithmetic
  on TIMESTAMPTZ is tz-agnostic for differences.
- For wall-clock semantics (hour-of-day, day-of-week, "today" gaming_day), the
  business tz is ``Asia/Hong_Kong``; expressions explicitly use
  ``AT TIME ZONE 'Asia/Hong_Kong'`` to avoid relying on DuckDB session tz.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HightierServingConfig,
    SHORT_TERM_TRIAL_BET_COLUMNS,
    default_hightier_serving_config,
)
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_dataset_has_any_parquet,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

BOUNDED_SHORT_TERM_MATERIALIZER_VERSION: Final[str] = "bounded_hot_pool_v4_stake_escalation"
FE_DERIVED_FULL_MATERIALIZE_BATCH_SIZE: Final[int] = 10_000
FE_DERIVED_PATRON_POOL_PLAYER_CHUNK_SIZE: Final[int] = 500

_TRAINING_BET_STAGING_COLUMNS: Final[tuple[str, ...]] = (
    "bet_id",
    "player_id",
    "payout_complete_dtm",
    "wager",
    "is_back_bet",
    "payout_odds",
    "casino_win",
    "bet_type",
    "type_of_bet",
    "session_id",
    "table_id",
    "gaming_day_event",
)


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")



_FE_DERIVED_AFTER_TID_CTE: Final[str] = """
pid_from_train AS (
  SELECT DISTINCT TRY_CAST(_b.player_id AS BIGINT) AS player_id
  FROM {bet_from} AS _b
  INNER JOIN tid ON TRY_CAST(_b.bet_id AS DOUBLE) = tid.bet_id
  WHERE TRY_CAST(_b.player_id AS BIGINT) IS NOT NULL
),
cmap AS (
  SELECT DISTINCT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{cmap_esc}')
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
),
cid_from_train AS (
  SELECT DISTINCT c.canonical_id AS canonical_id
  FROM pid_from_train AS p
  INNER JOIN cmap AS c ON p.player_id = c.player_id
),
pid AS (
  SELECT player_id FROM pid_from_train
  UNION
  SELECT DISTINCT c.player_id
  FROM cmap AS c
  INNER JOIN cid_from_train AS t ON c.canonical_id = t.canonical_id
),
src AS (
  SELECT
    TRY_CAST(b."bet_id" AS DOUBLE) AS bet_id,
    TRY_CAST(b."player_id" AS BIGINT) AS player_id,
    COALESCE(c.canonical_id, CAST(b."player_id" AS VARCHAR)) AS canonical_id,
    TRY_CAST(b."session_id" AS BIGINT) AS session_id,
    TRY_CAST(b."table_id" AS BIGINT) AS table_id,
    TRY_CAST(b."game_id" AS BIGINT) AS game_id,
    TRY_CAST(b."gaming_day_event" AS DATE) AS gaming_day_event,
    CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) AS pcd,
    TRY_CAST(b."wager" AS DOUBLE) AS wager,
    TRY_CAST(b."payout_odds" AS DOUBLE) AS payout_odds,
    TRY_CAST(b."casino_win" AS DOUBLE) AS casino_win,
    TRY_CAST(b."theo_win" AS DOUBLE) AS theo_win,
    TRY_CAST(b."base_ha" AS DOUBLE) AS base_ha
  FROM {bet_from} AS b
  INNER JOIN pid ON TRY_CAST(b."player_id" AS BIGINT) = pid.player_id
  LEFT JOIN cmap AS c ON TRY_CAST(b."player_id" AS BIGINT) = c.player_id
  WHERE TRY_CAST(b."bet_id" AS DOUBLE) IS NOT NULL
    AND TRY_CAST(b."player_id" AS BIGINT) IS NOT NULL
    AND b."payout_complete_dtm" IS NOT NULL
),
src_lagged AS (
  SELECT s.*,
    LAG(pcd) OVER w_canonical AS lag1_pcd,
    LAG(pcd, 2) OVER w_canonical AS lag2_pcd,
    LAG(table_id) OVER w_canonical AS lag1_table_id,
    LAG(table_id, 2) OVER w_canonical AS lag2_table_id,
    LAG(wager) OVER w_canonical AS lag1_wager,
    LAG(payout_odds) OVER w_canonical AS lag1_payout_odds,
    LAG(casino_win) OVER w_canonical AS lag1_casino_win,
    LAG(casino_win, 2) OVER w_canonical AS lag2_casino_win,
    LAG(casino_win, 3) OVER w_canonical AS lag3_casino_win,
    ROW_NUMBER() OVER w_session AS bet_idx_in_session,
    MIN(pcd) OVER w_session AS first_pcd_in_session,
    SUM(wager) OVER w_session_prior AS wager_sum_in_session_prior,
    SUM(wager) OVER w_session AS wager_sum_in_session,
    SUM(theo_win) OVER w_session AS theo_win_sum_in_session,
    COUNT(*) OVER w_canon_day_prior AS bets_today_so_far,
    SUM(wager) OVER w_canon_day_prior AS wager_today_so_far,
    MIN(pcd) OVER w_canon_day_inclusive AS first_pcd_today
  FROM src AS s
  WINDOW
    w_canonical AS (PARTITION BY canonical_id ORDER BY pcd, bet_id),
    w_session AS (PARTITION BY session_id ORDER BY pcd, bet_id),
    w_session_prior AS (
      PARTITION BY session_id ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ),
    w_canon_day_prior AS (
      PARTITION BY canonical_id, gaming_day_event ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ),
    w_canon_day_inclusive AS (
      PARTITION BY canonical_id, gaming_day_event ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
),
src_with_iv AS (
  SELECT s.*,
    EXTRACT(epoch FROM (pcd - lag1_pcd)) AS interarrival_sec,
    CASE
      WHEN table_id IS NOT NULL AND lag1_table_id IS NOT NULL AND table_id <> lag1_table_id THEN 1
      WHEN table_id IS NOT NULL AND lag1_table_id IS NOT NULL THEN 0
      ELSE NULL
    END AS changed_table_vs_lag1,
    SUM(CASE WHEN COALESCE(casino_win, 0) <= 0 THEN 1 ELSE 0 END)
      OVER (
        PARTITION BY canonical_id ORDER BY pcd, bet_id
        ROWS UNBOUNDED PRECEDING
      ) AS _loss_streak_grp,
    CASE
      WHEN lag1_casino_win > 0 AND lag1_wager > 1e-9 AND wager >= 2.0 * lag1_wager THEN 1.0
      ELSE 0.0
    END AS loss_then_double_flag
  FROM src_lagged AS s
),
ordered AS (
  SELECT s.*,
    COUNT(*) OVER w5m AS cnt_w5m,
    COUNT(*) OVER w15 AS fe__bets_cnt__w15m_raw,
    COALESCE(SUM(wager) OVER w15, 0.0) AS fe__wager_sum__w15m_raw,
    COALESCE(SUM(casino_win) OVER w15, 0.0) AS cw_sum_w15m,
    COALESCE(SUM(casino_win) OVER w15_peer, 0.0) AS cw_sum_w15m_peer,
    COUNT(*) OVER w1h AS cnt_w1h,
    COALESCE(SUM(wager) OVER w1h, 0.0) AS wager_sum_w1h,
    COALESCE(SUM(casino_win) OVER w1h, 0.0) AS cw_sum_w1h,
    COALESCE(SUM(casino_win) OVER w1h_peer, 0.0) AS cw_sum_w1h_peer,
    COALESCE(SUM(theo_win) OVER w1h, 0.0) AS tw_sum_w1h,
    COALESCE(SUM(theo_win) OVER w1h_peer, 0.0) AS tw_sum_w1h_peer,
    COALESCE(SUM(CASE WHEN casino_win > 0 THEN 1 ELSE 0 END) OVER w1h, 0) AS loss_cnt_w1h,
    AVG(payout_odds) OVER w1h AS payout_odds_avg_w1h,
    STDDEV_POP(payout_odds) OVER w1h AS payout_odds_std_w1h,
    AVG(wager) OVER w1h AS avg_wager_w1h,
    STDDEV_POP(wager) OVER w1h AS std_wager_w1h,
    MAX(wager) OVER w1h AS max_wager_w1h,
    MAX(payout_odds) OVER w1h AS max_payout_odds_w1h,
    AVG(interarrival_sec) OVER w1h AS interarrival_avg_w1h,
    STDDEV_POP(interarrival_sec) OVER w1h AS interarrival_std_w1h,
    SUM(changed_table_vs_lag1) OVER w_5_prior_rows AS changes_in_last_5_bets,
    COUNT(*) OVER w1d AS fe__bets_cnt__w1d_raw,
    COALESCE(SUM(wager) OVER w1d, 0.0) AS fe__wager_sum__w1d_raw,
    COUNT(*) OVER w7d AS fe__bets_cnt__w7d_raw,
    COALESCE(SUM(wager) OVER w7d, 0.0) AS fe__wager_sum__w7d_raw,
    COUNT(*) OVER w30d AS fe__bets_cnt__w30d_raw,
    COALESCE(SUM(wager) OVER w30d, 0.0) AS fe__wager_sum__w30d_raw,
    COALESCE(AVG(wager) OVER w30_prior, CAST(NULL AS DOUBLE)) AS prior_wager_mean_w30d,
    COALESCE(STDDEV_POP(wager) OVER w30_prior, CAST(NULL AS DOUBLE)) AS prior_wager_std_w30d,
    COALESCE(AVG(payout_odds) OVER w30_prior, CAST(NULL AS DOUBLE)) AS prior_odds_mean_w30d,
    COALESCE(STDDEV_POP(payout_odds) OVER w30_prior, CAST(NULL AS DOUBLE)) AS prior_odds_std_w30d,
    MAX(pcd) OVER w7d AS max_pcd_w7d,
    MIN(pcd) OVER w7d AS min_pcd_w7d,
    COALESCE(STDDEV_POP(wager) OVER w7d, CAST(NULL AS DOUBLE)) AS std_wager_w7d,
    COALESCE(AVG(ABS(wager)) OVER w7d, CAST(NULL AS DOUBLE)) AS avg_abs_wager_w7d,
    AVG(interarrival_sec) OVER w7d AS interarrival_avg_w7d,
    STDDEV_POP(interarrival_sec) OVER w7d AS interarrival_std_w7d,
    AVG(payout_odds) OVER w7d AS payout_odds_avg_w7d,
    STDDEV_POP(payout_odds) OVER w7d AS payout_odds_std_w7d,
    CAST(
      CASE
        WHEN COALESCE(casino_win, 0) > 0 THEN ROW_NUMBER() OVER (
          PARTITION BY canonical_id, _loss_streak_grp ORDER BY pcd, bet_id
        )
        ELSE 0
      END AS DOUBLE
    ) AS fe__outcome__consecutive_loss_streak,
    CASE
      WHEN COALESCE(SUM(CASE WHEN casino_win > 0 THEN 1 ELSE 0 END) OVER w1h, 0) > 0
      THEN CAST(
        SUM(loss_then_double_flag) OVER w1h
        / COALESCE(SUM(CASE WHEN casino_win > 0 THEN 1 ELSE 0 END) OVER w1h, 0)
        AS DOUBLE)
      ELSE CAST(NULL AS DOUBLE)
    END AS fe__outcome__loss_then_double_ratio__w1h,
    CAST(
      AVG(
        CASE
          WHEN lag1_casino_win > 0 AND lag1_wager > 1e-9 THEN wager / lag1_wager
          ELSE NULL
        END
      ) OVER w1h AS DOUBLE
    ) AS fe__outcome__wager_after_loss_step_ratio__w1h,
    REGR_SLOPE(wager, EXTRACT(epoch FROM pcd)) OVER w1h_peer AS wager_regr_slope_w1h_peer,
    AVG(wager) OVER w_last3 AS wager_avg_last3,
    AVG(wager) OVER w_prior3 AS wager_avg_prior3
  FROM src_with_iv AS s
  WINDOW
    w5m AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '5 MINUTE' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w15 AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '15 MINUTE' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w15_peer AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '15 MINUTE' PRECEDING AND CURRENT ROW
    ),
    w1h AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 HOUR' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w1h_peer AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 HOUR' PRECEDING AND CURRENT ROW
    ),
    w1d AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w7d AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '7 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w30d AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '30 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w30_prior AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '30 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w_5_prior_rows AS (
      PARTITION BY canonical_id ORDER BY pcd, bet_id
      ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING
    ),
    w_last3 AS (
      PARTITION BY canonical_id ORDER BY pcd, bet_id
      ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
    ),
    w_prior3 AS (
      PARTITION BY canonical_id ORDER BY pcd, bet_id
      ROWS BETWEEN 5 PRECEDING AND 3 PRECEDING
    )
)
SELECT
  bet_id,
  CAST(EXTRACT(epoch FROM (pcd - lag1_pcd)) AS DOUBLE) AS fe__time_since_last_bet_sec,
  CAST(fe__bets_cnt__w15m_raw AS DOUBLE) AS fe__bets_cnt__w15m,
  CAST(fe__wager_sum__w15m_raw AS DOUBLE) AS fe__wager_sum__w15m,
  CAST(fe__bets_cnt__w1d_raw AS DOUBLE) AS fe__bets_cnt__w1d,
  CAST(fe__wager_sum__w1d_raw AS DOUBLE) AS fe__wager_sum__w1d,
  CASE
    WHEN fe__wager_sum__w1d_raw > 1e-9 THEN CAST(fe__wager_sum__w15m_raw / fe__wager_sum__w1d_raw AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__wager_sum__w15m_over_w1d,
  CASE
    WHEN fe__bets_cnt__w1d_raw > 1e-9 THEN CAST(fe__bets_cnt__w15m_raw / fe__bets_cnt__w1d_raw AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__bets_cnt__w15m_over_w1d,
  CAST(fe__bets_cnt__w7d_raw AS DOUBLE) AS fe__bets_cnt__w7d,
  CAST(fe__bets_cnt__w30d_raw AS DOUBLE) AS fe__bets_cnt__w30d,
  CAST(fe__wager_sum__w7d_raw AS DOUBLE) AS fe__wager_sum__w7d,
  CAST(fe__wager_sum__w30d_raw AS DOUBLE) AS fe__wager_sum__w30d,
  CASE
    WHEN fe__wager_sum__w30d_raw > 1e-9 THEN CAST(fe__wager_sum__w7d_raw / fe__wager_sum__w30d_raw AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__wager_sum__w7d_over_w30d,
  CAST(fe__bets_cnt__w30d_raw / 30.0 AS DOUBLE) AS fe__bets_density_proxy_w30d,
  CASE
    WHEN fe__bets_cnt__w7d_raw > 1.5 AND max_pcd_w7d IS NOT NULL AND min_pcd_w7d IS NOT NULL
    THEN CAST(
      EXTRACT(epoch FROM (max_pcd_w7d - min_pcd_w7d)) / (fe__bets_cnt__w7d_raw - 1.0)
      AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival_mean_sec_w7d,
  CASE
    WHEN avg_abs_wager_w7d IS NOT NULL AND avg_abs_wager_w7d > 1e-12 AND std_wager_w7d IS NOT NULL
    THEN CAST(std_wager_w7d / avg_abs_wager_w7d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__wager_cv_w7d,
  CASE
    WHEN prior_wager_std_w30d IS NOT NULL AND ABS(prior_wager_std_w30d) > 1e-12
    THEN CAST((wager - prior_wager_mean_w30d) / prior_wager_std_w30d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__wager_z_prior_w30d,
  CASE
    WHEN prior_odds_std_w30d IS NOT NULL AND ABS(prior_odds_std_w30d) > 1e-12
       AND payout_odds IS NOT NULL
    THEN CAST((payout_odds - prior_odds_mean_w30d) / prior_odds_std_w30d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__payout_odds_z_prior_w30d,

  -- Group L: clock_context (HK wall-clock; explicit AT TIME ZONE for portability)
  CAST(EXTRACT(hour FROM pcd AT TIME ZONE 'Asia/Hong_Kong') AS DOUBLE) AS fe__clock__hour_of_day,
  CAST(EXTRACT(isodow FROM pcd AT TIME ZONE 'Asia/Hong_Kong') AS DOUBLE) AS fe__clock__day_of_week,
  CASE
    WHEN EXTRACT(isodow FROM pcd AT TIME ZONE 'Asia/Hong_Kong') >= 6 THEN 1.0
    ELSE 0.0
  END AS fe__clock__is_weekend,
  CASE
    WHEN EXTRACT(hour FROM pcd AT TIME ZONE 'Asia/Hong_Kong') BETWEEN 0 AND 5 THEN 1.0
    ELSE 0.0
  END AS fe__clock__is_late_night,

  -- Group M: outcome_state (casino_win > 0 = player lost; bet-table only, PIT-safe)
  CAST(cw_sum_w15m_peer - COALESCE(casino_win, 0.0) AS DOUBLE) AS fe__outcome__casino_win_sum__w15m,
  CAST(cw_sum_w1h_peer - COALESCE(casino_win, 0.0) AS DOUBLE) AS fe__outcome__casino_win_sum__w1h,
  CASE
    WHEN cnt_w1h > 0 THEN CAST(loss_cnt_w1h * 1.0 / cnt_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__outcome__loss_rate__w1h,
  CASE
    WHEN wager_sum_w1h > 1e-9 THEN CAST(cw_sum_w1h / wager_sum_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__outcome__net_pnl_to_wager_ratio__w1h,
  CASE
    WHEN (tw_sum_w1h_peer - COALESCE(theo_win, 0.0)) > 1e-9
    THEN CAST(
      (cw_sum_w1h_peer - COALESCE(casino_win, 0.0))
      / (tw_sum_w1h_peer - COALESCE(theo_win, 0.0))
      AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__outcome__casino_win_to_theo_ratio__w1h,
  CAST(
    CASE WHEN lag1_casino_win IS NOT NULL AND lag1_casino_win > 0 THEN 1 ELSE 0 END
    + CASE WHEN lag2_casino_win IS NOT NULL AND lag2_casino_win > 0 THEN 1 ELSE 0 END
    + CASE WHEN lag3_casino_win IS NOT NULL AND lag3_casino_win > 0 THEN 1 ELSE 0 END
    AS DOUBLE
  ) AS fe__outcome__last_3_bets_loss_count,
  fe__outcome__consecutive_loss_streak,
  fe__outcome__loss_then_double_ratio__w1h,
  fe__outcome__wager_after_loss_step_ratio__w1h,

  -- Group N: stake_dynamics
  CASE
    WHEN std_wager_w1h IS NOT NULL AND ABS(std_wager_w1h) > 1e-12
       AND avg_wager_w1h IS NOT NULL AND wager IS NOT NULL
    THEN CAST((wager - avg_wager_w1h) / std_wager_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_z__w1h,
  CASE
    WHEN max_wager_w1h IS NOT NULL AND max_wager_w1h > 1e-9 AND wager IS NOT NULL
    THEN CAST(wager / max_wager_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_to_recent_max_ratio__w1h,
  CASE
    WHEN lag1_wager IS NOT NULL AND lag1_wager > 1e-9 AND wager IS NOT NULL
    THEN CAST(wager / lag1_wager AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_step_pct,
  CASE
    WHEN avg_wager_w1h IS NOT NULL AND avg_wager_w1h > 1e-12 AND std_wager_w1h IS NOT NULL
    THEN CAST(std_wager_w1h / avg_wager_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_cv__w1h,
  CASE
    WHEN wager_regr_slope_w1h_peer IS NOT NULL
       AND avg_wager_w1h IS NOT NULL AND avg_wager_w1h > 1e-9
    THEN CAST(wager_regr_slope_w1h_peer / avg_wager_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_trend_slope__w1h,
  CASE
    WHEN wager_avg_prior3 IS NOT NULL AND wager_avg_prior3 > 1e-9
       AND wager_avg_last3 IS NOT NULL
    THEN CAST(wager_avg_last3 / wager_avg_prior3 AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_last3_vs_prior3_ratio__w1h,

  -- Group K: canonical_today (partition by canonical_id × gaming_day; static cmap is PIT-safe)
  CAST(COALESCE(bets_today_so_far, 0) AS DOUBLE) AS fe__canonical__bets_cnt__today,
  CAST(COALESCE(wager_today_so_far, 0.0) AS DOUBLE) AS fe__canonical__wager_sum__today,
  CASE
    WHEN bets_today_so_far IS NOT NULL AND bets_today_so_far > 0 AND wager_today_so_far IS NOT NULL
    THEN CAST(wager_today_so_far / bets_today_so_far AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__canonical__avg_wager__today,
  CAST(EXTRACT(epoch FROM (pcd - first_pcd_today)) AS DOUBLE)
    AS fe__canonical__elapsed_sec_since_first_bet__today,

  -- Group O: table_switch (NULL when no lag exists; counts use 4-prior-rows window)
  CAST(changed_table_vs_lag1 AS DOUBLE) AS fe__tableswitch__changed_table_vs_lag1,
  CASE
    WHEN table_id IS NOT NULL AND lag2_table_id IS NOT NULL AND table_id <> lag2_table_id THEN 1.0
    WHEN table_id IS NOT NULL AND lag2_table_id IS NOT NULL THEN 0.0
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__tableswitch__changed_table_vs_lag2,
  CAST(COALESCE(changes_in_last_5_bets, 0) AS DOUBLE) AS fe__tableswitch__changes_in_last_5_bets,

  -- Group E: session_position (uses bet-table session_id; PIT-safe)
  CAST(bet_idx_in_session AS DOUBLE) AS fe__session__bet_idx_in_session,
  CAST(EXTRACT(epoch FROM (pcd - first_pcd_in_session)) AS DOUBLE)
    AS fe__session__elapsed_sec_since_first_bet_in_session,
  CAST(COALESCE(wager_sum_in_session_prior, 0.0) AS DOUBLE)
    AS fe__session__wager_sum_in_session_so_far,
    
  -- New derived PIT-safe session features matching sess__* semantics but running totals
  ln(1 + greatest(coalesce(CAST(bet_idx_in_session AS DOUBLE), 0.0), 0.0)) AS fe__session__num_games_with_wager_log1p,
  ln(1 + greatest(coalesce(CAST(bet_idx_in_session AS DOUBLE), 0.0), 0.0)) AS fe__session__num_bets_log1p,
  ln(1 + greatest(coalesce(CAST(wager_sum_in_session AS DOUBLE), 0.0), 0.0)) AS fe__session__turnover_log1p,
  sign(coalesce(CAST(theo_win_sum_in_session AS DOUBLE), 0.0)) * ln(1 + abs(coalesce(CAST(theo_win_sum_in_session AS DOUBLE), 0.0))) AS fe__session__theo_win_log1p_signed,
  CASE
    WHEN coalesce(CAST(wager_sum_in_session AS DOUBLE), 0.0) > 0 AND coalesce(CAST(bet_idx_in_session AS DOUBLE), 0.0) > 0
    THEN ln(1 + CAST(wager AS DOUBLE) / (CAST(wager_sum_in_session AS DOUBLE) / CAST(bet_idx_in_session AS DOUBLE)))
    ELSE 0.0
  END AS fe__session__bet_wager_over_sess_avg_log1p,

  -- Group F: rate_decay (short-window count + velocity ratios; tail of 1d window)
  CAST(cnt_w5m AS DOUBLE) AS fe__rate__bets_cnt__w5m,
  CASE
    WHEN fe__bets_cnt__w15m_raw > 0 THEN CAST(cnt_w5m * 3.0 / fe__bets_cnt__w15m_raw AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__rate__velocity__w5m_over_w15m,
  CASE
    WHEN cnt_w1h > 0 THEN CAST(fe__bets_cnt__w15m_raw * 4.0 / cnt_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__rate__velocity__w15m_over_w1h,

  -- Group G: interarrival_dynamics
  CAST(EXTRACT(epoch FROM (lag1_pcd - lag2_pcd)) AS DOUBLE) AS fe__interarrival__lag2_sec,
  CASE
    WHEN interarrival_std_w7d IS NOT NULL AND interarrival_std_w7d > 1e-9
       AND interarrival_avg_w7d IS NOT NULL AND interarrival_sec IS NOT NULL
    THEN CAST((interarrival_sec - interarrival_avg_w7d) / interarrival_std_w7d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival__last_gap_z__w7d,
  CASE
    WHEN interarrival_avg_w1h IS NOT NULL AND interarrival_avg_w1h > 1e-9
       AND interarrival_sec IS NOT NULL
    THEN CAST(interarrival_sec / interarrival_avg_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival__last_gap_to_recent_mean_ratio__w1h,
  CASE
    WHEN interarrival_avg_w1h IS NOT NULL AND interarrival_avg_w1h > 1e-9
       AND interarrival_std_w1h IS NOT NULL
    THEN CAST(interarrival_std_w1h / interarrival_avg_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival__cv__w1h,

  -- Group I: theo_exposure (theo_win = expected casino take per bet)
  CASE
    WHEN wager IS NOT NULL AND wager > 1e-9 AND theo_win IS NOT NULL
    THEN CAST(theo_win / wager AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__theo__theo_win_to_wager_ratio,
  CAST(base_ha AS DOUBLE) AS fe__theo__base_ha,
  CAST(tw_sum_w1h AS DOUBLE) AS fe__theo__theo_win_sum__w1h,
  CASE
    WHEN wager_sum_w1h > 1e-9 THEN CAST(tw_sum_w1h / wager_sum_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__theo__theo_win_to_wager_ratio__w1h,

  -- Group Q: payout_odds_dynamics
  CASE
    WHEN payout_odds_std_w1h IS NOT NULL AND payout_odds_std_w1h > 1e-12
       AND payout_odds_avg_w1h IS NOT NULL AND payout_odds IS NOT NULL
    THEN CAST((payout_odds - payout_odds_avg_w1h) / payout_odds_std_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_z__w1h,
  CASE
    WHEN payout_odds_std_w7d IS NOT NULL AND payout_odds_std_w7d > 1e-12
       AND payout_odds_avg_w7d IS NOT NULL AND payout_odds IS NOT NULL
    THEN CAST((payout_odds - payout_odds_avg_w7d) / payout_odds_std_w7d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_z__w7d,
  CASE
    WHEN max_payout_odds_w1h IS NOT NULL AND max_payout_odds_w1h > 1e-9 AND payout_odds IS NOT NULL
    THEN CAST(payout_odds / max_payout_odds_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_to_recent_max_ratio__w1h,
  CASE
    WHEN lag1_payout_odds IS NOT NULL AND lag1_payout_odds > 1e-9 AND payout_odds IS NOT NULL
    THEN CAST(payout_odds / lag1_payout_odds AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_step_ratio
""".strip()

_TID_SELECT_FROM_STAGED: Final[str] = """
  SELECT DISTINCT TRY_CAST(bet_id AS DOUBLE) AS bet_id
  FROM staged_tid
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
""".strip()


def _fe_derived_window_materialize_sql(
    *,
    bet_from: str,
    cmap_esc: str,
    tid_select_sql: str,
) -> str:
    """Build full-window ``fe__*`` materialize SQL for one ``tid`` source."""

    return f"""
WITH tid AS (
{tid_select_sql}
),
{_FE_DERIVED_AFTER_TID_CTE.format(bet_from=bet_from, cmap_esc=cmap_esc)}
FROM ordered
WHERE bet_id IN (SELECT bet_id FROM tid)
""".strip()


def _iter_training_bet_id_batches(
    training_parquet: Path,
    *,
    batch_size: int,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Iterator[pd.DataFrame]:
    """Yield ``bet_id`` slices from a training parquet without loading all ids."""

    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1; got {batch_size}")
    t_esc = _path_esc(training_parquet)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(
            f"""
            CREATE TEMP TABLE _training_bet_ids_ordered AS
            SELECT
              TRY_CAST(bet_id AS DOUBLE) AS bet_id,
              ROW_NUMBER() OVER (
                ORDER BY TRY_CAST(bet_id AS DOUBLE) ASC
              ) AS rn
            FROM read_parquet('{t_esc}')
            WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
            """,
        )
        total_row = con.execute("SELECT COALESCE(MAX(rn), 0)::BIGINT FROM _training_bet_ids_ordered").fetchone()
        total = int(total_row[0]) if total_row else 0
        for start in range(0, total, batch_size):
            end = min(start + batch_size, total)
            df = con.execute(
                f"""
                SELECT bet_id
                FROM _training_bet_ids_ordered
                WHERE rn > {start} AND rn <= {end}
                """,
            ).fetchdf()
            if not df.empty:
                yield df
    finally:
        con.close()


def _write_fe_derived_batch_parquet(
    *,
    bet_id_batch: pd.DataFrame,
    bet_from: str,
    cmap_esc: str,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Materialize one ``bet_id`` batch to a standalone parquet part."""

    dst_esc = _path_esc(out_parquet).replace("'", "''")
    sql = _fe_derived_window_materialize_sql(
        bet_from=bet_from,
        cmap_esc=cmap_esc,
        tid_select_sql=_TID_SELECT_FROM_STAGED,
    )
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.register("staged_tid", bet_id_batch)
        con.execute(f"COPY ({sql}) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()


def _collect_training_player_ids(
    training_parquet: Path,
    *,
    cleaned_bet_from: str,
    cmap_esc: str,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> list[int]:
    """Resolve all ``player_id`` aliases for training ``bet_id`` rows (via canonical map)."""

    tp_esc = _path_esc(training_parquet).replace("'", "''")
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        rows = con.execute(
            f"""
            WITH tid AS (
              SELECT DISTINCT TRY_CAST(bet_id AS DOUBLE) AS bet_id
              FROM read_parquet('{tp_esc}')
              WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
            ),
            train_pid AS (
              SELECT DISTINCT TRY_CAST(_b."player_id" AS BIGINT) AS player_id
              FROM {cleaned_bet_from} AS _b
              INNER JOIN tid ON TRY_CAST(_b."bet_id" AS DOUBLE) = tid.bet_id
              WHERE TRY_CAST(_b."player_id" AS BIGINT) IS NOT NULL
            ),
            cmap AS (
              SELECT DISTINCT
                TRY_CAST(player_id AS BIGINT) AS player_id,
                TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
              FROM read_parquet('{cmap_esc}')
              WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
                AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
            ),
            cid AS (
              SELECT DISTINCT c.canonical_id
              FROM train_pid AS p
              INNER JOIN cmap AS c ON p.player_id = c.player_id
            )
            SELECT DISTINCT c.player_id
            FROM cmap AS c
            INNER JOIN cid ON c.canonical_id = cid.canonical_id
            ORDER BY 1
            """,
        ).fetchall()
        return [int(row[0]) for row in rows]
    finally:
        con.close()


def _materialize_patron_bet_pool_parquet(
    *,
    cleaned_bet_from: str,
    cmap_esc: str,
    player_ids: list[int],
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Scan cleaned bets once (chunked by ``player_id``) into a local patron pool."""

    if not player_ids:
        raise ValueError("player_ids must be non-empty for patron bet pool materialization")
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    pool_dir = dst.parent / f"{dst.stem}__pool_parts"
    if pool_dir.is_dir():
        shutil.rmtree(pool_dir)
    pool_dir.mkdir(parents=True, exist_ok=True)

    pool_parts: list[Path] = []
    chunk_size = FE_DERIVED_PATRON_POOL_PLAYER_CHUNK_SIZE
    for chunk_idx, start in enumerate(range(0, len(player_ids), chunk_size)):
        chunk = player_ids[start : start + chunk_size]
        ids_sql = ",".join(str(int(pid)) for pid in chunk)
        part = pool_dir / f"pool_{chunk_idx:06d}.parquet"
        part_esc = _path_esc(part).replace("'", "''")
        con = duckdb.connect(database=":memory:")
        try:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
            con.execute(
                f"""
                COPY (
                  SELECT
                    TRY_CAST(b."bet_id" AS DOUBLE) AS bet_id,
                    TRY_CAST(b."player_id" AS BIGINT) AS player_id,
                    COALESCE(c.canonical_id, CAST(b."player_id" AS VARCHAR)) AS canonical_id,
                    TRY_CAST(b."session_id" AS BIGINT) AS session_id,
                    TRY_CAST(b."table_id" AS BIGINT) AS table_id,
                    TRY_CAST(b."game_id" AS BIGINT) AS game_id,
                    TRY_CAST(b."gaming_day_event" AS DATE) AS gaming_day_event,
                    CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) AS payout_complete_dtm,
                    TRY_CAST(b."wager" AS DOUBLE) AS wager,
                    TRY_CAST(b."payout_odds" AS DOUBLE) AS payout_odds,
                    TRY_CAST(b."casino_win" AS DOUBLE) AS casino_win,
                    TRY_CAST(b."theo_win" AS DOUBLE) AS theo_win,
                    TRY_CAST(b."base_ha" AS DOUBLE) AS base_ha
                  FROM {cleaned_bet_from} AS b
                  LEFT JOIN (
                    SELECT DISTINCT
                      TRY_CAST(player_id AS BIGINT) AS player_id,
                      TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
                    FROM read_parquet('{cmap_esc}')
                    WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
                      AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
                  ) AS c ON TRY_CAST(b."player_id" AS BIGINT) = c.player_id
                  WHERE TRY_CAST(b."player_id" AS BIGINT) IN ({ids_sql})
                    AND TRY_CAST(b."bet_id" AS DOUBLE) IS NOT NULL
                    AND b."payout_complete_dtm" IS NOT NULL
                ) TO '{part_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
                """,
            )
        finally:
            con.close()
        pool_parts.append(part)
        if (chunk_idx + 1) % 20 == 0:
            logger.info("[fe_derived] patron pool parts=%d (last_chunk_players=%d)", chunk_idx + 1, len(chunk))

    _merge_fe_derived_batch_parquets(pool_parts, out_parquet=dst, duckdb_runtime=duckdb_runtime)
    shutil.rmtree(pool_dir, ignore_errors=True)
    return dst


def _merge_fe_derived_batch_parquets(
    batch_paths: list[Path],
    *,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Concatenate batch parquet parts into one output file."""

    dst_esc = _path_esc(out_parquet)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        if len(batch_paths) == 1:
            single_esc = _path_esc(batch_paths[0])
            con.execute(
                f"COPY (SELECT * FROM read_parquet('{single_esc}')) "
                f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
            )
        else:
            paths_esc = "[" + ",".join(f"'{_path_esc(p)}'" for p in batch_paths) + "]"
            con.execute(
                f"COPY (SELECT * FROM read_parquet({paths_esc})) "
                f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
            )
    finally:
        con.close()


def materialize_fe_derived_parquet(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    batch_size: int | None = None,
) -> Path:
    """Compute ``fe__*`` columns keyed by ``bet_id``; write standalone Parquet.

    Args:
        cleaned_bet_parquet: Hive-partition cleaned bet root (trainer default layout).
        training_parquet_for_bet_ids: Step-3-style training parquet; used to derive
            which ``canonical_id`` (via mapping) timelines to scan (reduces DuckDB
            workload while including all ``player_id`` aliases per canonical).
        out_parquet: Output path (written with Snappy compression).
        duckdb_runtime: DuckDB PRAGMA preset.
        canonical_mapping_parquet: Optional ``player_id``→``canonical_id`` map; defaults
            to :func:`~trainer_hightier.utils.canonical_mapping.default_canonical_mapping_parquet_path`.
        batch_size: Training ``bet_id`` rows per DuckDB window batch; defaults to
            :data:`FE_DERIVED_FULL_MATERIALIZE_BATCH_SIZE`.

    Returns:
        Resolved ``out_parquet`` path.

    Raises:
        FileNotFoundError: If cleaned bet glob yields no readable Parquet, training
            parquet is missing, or canonical mapping parquet is missing.
    """

    src_root = Path(cleaned_bet_parquet).resolve()
    tp = Path(training_parquet_for_bet_ids).resolve()
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmap_path = (
        Path(canonical_mapping_parquet).resolve()
        if canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    if not tp.is_file():
        raise FileNotFoundError(training_parquet_for_bet_ids)
    if not cleaned_bet_dataset_has_any_parquet(src_root):
        raise FileNotFoundError(f"No cleaned bet parquet under {src_root}")
    if not cmap_path.is_file():
        raise FileNotFoundError(
            f"canonical_player_mapping parquet missing: {cmap_path}; "
            "run trainer_hightier.utils.canonical_mapping materialization first."
        )
    cleaned_bet_from = resolved_cleaned_bet_read_parquet_sql(src_root)
    cmap_esc = _path_esc(cmap_path).replace("'", "''")

    effective_batch_size = int(
        batch_size if batch_size is not None else FE_DERIVED_FULL_MATERIALIZE_BATCH_SIZE,
    )
    if effective_batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {effective_batch_size}")

    batch_dir = dst.parent / f"{dst.stem}__fe_derived_batches"
    if batch_dir.is_dir():
        shutil.rmtree(batch_dir)
    batch_dir.mkdir(parents=True, exist_ok=True)

    player_ids = _collect_training_player_ids(
        tp,
        cleaned_bet_from=cleaned_bet_from,
        cmap_esc=cmap_esc,
        duckdb_runtime=duckdb_runtime,
    )
    pool_path = batch_dir / "patron_bet_pool.parquet"
    logger.info("[fe_derived] building patron bet pool (%d player_ids)", len(player_ids))
    _materialize_patron_bet_pool_parquet(
        cleaned_bet_from=cleaned_bet_from,
        cmap_esc=cmap_esc,
        player_ids=player_ids,
        out_parquet=pool_path,
        duckdb_runtime=duckdb_runtime,
    )
    pool_esc = _path_esc(pool_path).replace("'", "''")
    bet_from = f"read_parquet('{pool_esc}')"

    batch_paths: list[Path] = []
    batch_idx = 0
    for bet_id_batch in _iter_training_bet_id_batches(
        tp,
        batch_size=effective_batch_size,
        duckdb_runtime=duckdb_runtime,
    ):
        part = batch_dir / f"part_{batch_idx:06d}.parquet"
        _write_fe_derived_batch_parquet(
            bet_id_batch=bet_id_batch,
            bet_from=bet_from,
            cmap_esc=cmap_esc,
            out_parquet=part,
            duckdb_runtime=duckdb_runtime,
        )
        batch_paths.append(part)
        batch_idx += 1
        if batch_idx % 10 == 0:
            logger.info(
                "[fe_derived] materialized %d batches (last_part=%s rows=%d)",
                batch_idx,
                part.name,
                len(bet_id_batch),
            )

    if not batch_paths:
        raise ValueError(
            f"fe_derived materialization produced no rows from {training_parquet_for_bet_ids}",
        )

    _merge_fe_derived_batch_parquets(batch_paths, out_parquet=dst, duckdb_runtime=duckdb_runtime)
    shutil.rmtree(batch_dir, ignore_errors=True)
    logger.info(
        "[fe_derived] wrote %s (%d batches, batch_size=%d)",
        dst.name,
        len(batch_paths),
        effective_batch_size,
    )
    return dst


# Mid-term columns emitted by legacy per-bet rolling windows (non-production semantics).
LEGACY_MID_TERM_FEATURE_COLUMNS: tuple[str, ...] = (
    "fe__bets_cnt__w1d",
    "fe__wager_sum__w1d",
    "fe__wager_sum__w15m_over_w1d",
    "fe__bets_cnt__w15m_over_w1d",
    "fe__bets_cnt__w7d",
    "fe__bets_cnt__w30d",
    "fe__wager_sum__w7d",
    "fe__wager_sum__w30d",
    "fe__wager_sum__w7d_over_w30d",
    "fe__bets_density_proxy_w30d",
    "fe__interarrival_mean_sec_w7d",
    "fe__wager_cv_w7d",
    "fe__wager_z_prior_w30d",
    "fe__payout_odds_z_prior_w30d",
    "fe__interarrival__last_gap_z__w7d",
)


def _iter_training_bet_batches(
    training_parquet: Path,
    *,
    batch_size: int,
    duckdb_runtime: DuckDbRuntimeConfig,
    payout_yyyymm: str | None = None,
    restrict_player_ids: tuple[int, ...] | None = None,
) -> Iterator[pd.DataFrame]:
    """Yield chronological training bet slices without loading the full table into memory."""

    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1; got {batch_size}")
    t_esc = _path_esc(training_parquet)
    month_filter = ""
    if payout_yyyymm is not None:
        ym = str(payout_yyyymm).strip()
        if len(ym) != 6 or not ym.isdigit():
            raise ValueError(f"payout_yyyymm must be six digits, got {payout_yyyymm!r}")
        month_filter = f" AND strftime(CAST(payout_complete_dtm AS TIMESTAMPTZ), '%Y%m') = '{ym}'"
    player_filter = ""
    if restrict_player_ids:
        ids_sql = ",".join(str(int(pid)) for pid in restrict_player_ids)
        player_filter = f" AND TRY_CAST(player_id AS BIGINT) IN ({ids_sql})"
    base_where = f"""
            WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
              AND payout_complete_dtm IS NOT NULL
              AND TRY_CAST(player_id AS BIGINT) IS NOT NULL{month_filter}{player_filter}
    """.strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        schema_cols = {c.lower() for c in pq.read_schema(Path(training_parquet).resolve()).names}
        need = {c.lower() for c in _TRAINING_BET_STAGING_COLUMNS}
        missing = sorted(need - schema_cols)
        if missing:
            raise ValueError(
                f"training parquet missing columns for bounded short-term staging: {missing}; "
                f"path={training_parquet.resolve()}",
            )
        con.execute(
            f"""
            CREATE TEMP TABLE _training_bets_ordered AS
            SELECT
              TRY_CAST(bet_id AS DOUBLE) AS bet_id,
              TRY_CAST(player_id AS BIGINT) AS player_id,
              CAST(payout_complete_dtm AS TIMESTAMPTZ) AS payout_complete_dtm,
              TRY_CAST(wager AS DOUBLE) AS wager,
              TRY_CAST(is_back_bet AS INTEGER) AS is_back_bet,
              TRY_CAST(payout_odds AS DOUBLE) AS payout_odds,
              TRY_CAST(casino_win AS DOUBLE) AS casino_win,
              CAST(bet_type AS VARCHAR) AS bet_type,
              CAST(type_of_bet AS VARCHAR) AS type_of_bet,
              TRY_CAST(session_id AS BIGINT) AS session_id,
              TRY_CAST(table_id AS BIGINT) AS table_id,
              CAST(gaming_day_event AS TIMESTAMP) AS gaming_day_event,
              ROW_NUMBER() OVER (
                ORDER BY CAST(payout_complete_dtm AS TIMESTAMPTZ) ASC,
                         TRY_CAST(bet_id AS DOUBLE) ASC
              ) AS rn
            FROM read_parquet('{t_esc}')
            {base_where}
            """,
        )
        total_row = con.execute("SELECT COALESCE(MAX(rn), 0)::BIGINT FROM _training_bets_ordered").fetchone()
        total = int(total_row[0]) if total_row else 0
        for start in range(0, total, batch_size):
            end = min(start + batch_size, total)
            df = con.execute(
                f"""
                SELECT * EXCLUDE (rn)
                FROM _training_bets_ordered
                WHERE rn > {start} AND rn <= {end}
                """,
            ).fetchdf()
            if not df.empty:
                df["payout_complete_dtm"] = pd.to_datetime(
                    df["payout_complete_dtm"],
                    errors="coerce",
                    utc=True,
                )
                yield df
    finally:
        con.close()


def _short_term_features_for_batch(
    bets_batch: pd.DataFrame,
    *,
    cleaned_bet_parquet: Path,
    mapping_parquet: Path,
    serving_cfg: HightierServingConfig,
    duckdb_runtime: DuckDbRuntimeConfig,
    fe_columns: tuple[str, ...],
    trial_columns: tuple[str, ...],
    payout_yyyymm: str | None = None,
    month_pool_conn: duckdb.DuckDBPyConnection | None = None,
    month_pool_table: str | None = None,
) -> pd.DataFrame:
    """Compute short-layer PIT ``bet__*`` and ``fe__*`` for one training/scoring batch."""
    from trainer_hightier.serving.short_term_scoring_context import (
        build_short_term_features_for_batch,
        default_short_term_scoring_context,
    )

    return build_short_term_features_for_batch(
        bets_batch,
        cleaned_bet_parquet=cleaned_bet_parquet,
        mapping_parquet=mapping_parquet,
        serving_cfg=serving_cfg,
        duckdb_runtime=duckdb_runtime,
        fe_columns=fe_columns,
        trial_columns=trial_columns,
        context=default_short_term_scoring_context(serving_cfg),
        payout_yyyymm=payout_yyyymm,
        month_pool_conn=month_pool_conn,
        month_pool_table=month_pool_table,
    )


def materialize_fe_derived_short_term_parquet(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    short_term_columns: tuple[str, ...] | None = None,
    trial_columns: tuple[str, ...] | None = None,
    batch_size: int | None = None,
    payout_yyyymm: str | None = None,
    restrict_player_ids: tuple[int, ...] | None = None,
) -> Path:
    """Write **offline short-term PIT cache** (``bet__*`` + short ``fe__*``) for training rows.

    Each output row is point-in-time for that ``bet_id`` (bounded hot pool, same batch size
    as scorer v2). This is an acceleration artifact for Step 4/5—not a mid-style daily
    snapshot and not used as production lookup for unseen bets. Mid-term columns are joined
    separately via ``dataset_enrich`` + daily snapshot ASOF.
    """

    fe_cols = tuple(short_term_columns or ())
    trial_cols = tuple(trial_columns if trial_columns is not None else SHORT_TERM_TRIAL_BET_COLUMNS)
    if not fe_cols and not trial_cols:
        raise ValueError("short_term materialization requires at least one fe__ or bet__ column")
    out_cols = tuple(dict.fromkeys(("bet_id", *trial_cols, *fe_cols)))

    serving_cfg = default_hightier_serving_config()
    effective_batch_size = int(
        batch_size if batch_size is not None else serving_cfg.hightier_scorer_max_bets_per_cycle,
    )
    if effective_batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {effective_batch_size}")
    cmap = (
        Path(canonical_mapping_parquet).resolve()
        if canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    if not cmap.is_file():
        raise FileNotFoundError(f"canonical_player_mapping parquet missing: {cmap}")

    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    batch_dir = dst.parent / f"{dst.stem}__{BOUNDED_SHORT_TERM_MATERIALIZER_VERSION}_batches"
    if batch_dir.is_dir():
        shutil.rmtree(batch_dir)
    batch_dir.mkdir(parents=True, exist_ok=True)

    batch_paths: list[Path] = []
    batch_idx = 0
    cleaned_root = Path(cleaned_bet_parquet).resolve()
    month_pool = None
    if payout_yyyymm is not None:
        from trainer_hightier.utils.cleaned_bet_pool_read import open_month_hot_pool_session

        month_pool = open_month_hot_pool_session(
            cleaned_root,
            payout_yyyymm=str(payout_yyyymm),
            duckdb_runtime=duckdb_runtime,
            hk_tz=serving_cfg.hk_tz,
            restrict_player_ids=restrict_player_ids,
        )
    try:
        for bets_batch in _iter_training_bet_batches(
            Path(training_parquet_for_bet_ids).resolve(),
            batch_size=effective_batch_size,
            duckdb_runtime=duckdb_runtime,
            payout_yyyymm=payout_yyyymm,
            restrict_player_ids=restrict_player_ids,
        ):
            features = _short_term_features_for_batch(
                bets_batch,
                cleaned_bet_parquet=cleaned_root,
                mapping_parquet=cmap,
                serving_cfg=serving_cfg,
                duckdb_runtime=duckdb_runtime,
                fe_columns=fe_cols,
                trial_columns=trial_cols,
                payout_yyyymm=payout_yyyymm,
                month_pool_conn=month_pool.conn if month_pool is not None else None,
                month_pool_table=month_pool.table_name if month_pool is not None else None,
            )
            part = batch_dir / f"part_{batch_idx:06d}.parquet"
            features[list(out_cols)].to_parquet(part, index=False)
            batch_paths.append(part)
            batch_idx += 1
            if batch_idx % 10 == 0:
                logger.info(
                    "[bounded_short_term] materialized %d batches (last_part=%s rows=%d)",
                    batch_idx,
                    part.name,
                    len(features),
                )
    finally:
        if month_pool is not None:
            month_pool.close()

    if not batch_paths:
        raise ValueError(
            f"bounded short-term materialization produced no rows from {training_parquet_for_bet_ids}",
        )

    col_sql = ", ".join(f'"{c}"' for c in out_cols)
    dst_esc = _path_esc(dst)
    if len(batch_paths) == 1:
        single_esc = _path_esc(batch_paths[0])
        con = duckdb.connect(database=":memory:")
        try:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
            con.execute(
                f"COPY (SELECT {col_sql} FROM read_parquet('{single_esc}')) "
                f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
            )
        finally:
            con.close()
    else:
        paths_esc = "[" + ",".join(f"'{_path_esc(p)}'" for p in batch_paths) + "]"
        con = duckdb.connect(database=":memory:")
        try:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
            con.execute(
                f"COPY (SELECT {col_sql} FROM read_parquet({paths_esc})) "
                f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
            )
        finally:
            con.close()
    shutil.rmtree(batch_dir, ignore_errors=True)
    logger.info(
        "[bounded_short_term] wrote %s (%d batches, lookback_h=%d, batch_size=%d)",
        dst.name,
        len(batch_paths),
        int(serving_cfg.hot_feature_pool_lookback_hours),
        effective_batch_size,
    )
    return dst


# Shared window pipeline (starts after ``src`` CTE is defined).
_FE_DERIVED_PIPELINE_AFTER_SRC: Final[str] = """
src_lagged AS (
  SELECT s.*,
    LAG(pcd) OVER w_canonical AS lag1_pcd,
    LAG(pcd, 2) OVER w_canonical AS lag2_pcd,
    LAG(table_id) OVER w_canonical AS lag1_table_id,
    LAG(payout_odds) OVER w_canonical AS lag1_payout_odds,
    LAG(wager) OVER w_canonical AS lag1_wager,
    COUNT(*) OVER w_canon_day_prior AS bets_today_so_far,
    SUM(wager) OVER w_canon_day_prior AS wager_today_so_far,
    MIN(pcd) OVER w_canon_day_inclusive AS first_pcd_today
  FROM src AS s
  WINDOW
    w_canonical AS (PARTITION BY canonical_id ORDER BY pcd, bet_id),
    w_canon_day_prior AS (
      PARTITION BY canonical_id, gaming_day_event ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ),
    w_canon_day_inclusive AS (
      PARTITION BY canonical_id, gaming_day_event ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
),
src_with_iv AS (
  SELECT s.*,
    EXTRACT(epoch FROM (pcd - lag1_pcd)) AS interarrival_sec
  FROM src_lagged AS s
),
ordered AS (
  SELECT s.*,
    COUNT(*) OVER w15 AS fe__bets_cnt__w15m_raw,
    COALESCE(SUM(wager) OVER w15, 0.0) AS fe__wager_sum__w15m_raw,
    AVG(payout_odds) OVER w1h AS payout_odds_avg_w1h,
    STDDEV_POP(payout_odds) OVER w1h AS payout_odds_std_w1h,
    AVG(interarrival_sec) OVER w1h AS interarrival_avg_w1h,
    STDDEV_POP(interarrival_sec) OVER w1h AS interarrival_std_w1h,
    AVG(payout_odds) OVER w7d AS payout_odds_avg_w7d,
    STDDEV_POP(payout_odds) OVER w7d AS payout_odds_std_w7d,
    MAX(payout_odds) OVER w1h AS max_payout_odds_w1h
  FROM src_with_iv AS s
  WINDOW
    w15 AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '15 MINUTE' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w1h AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 HOUR' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w7d AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '7 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    )
)
SELECT
  bet_id,
  CAST(interarrival_sec AS DOUBLE) AS fe__time_since_last_bet_sec,
  CAST(fe__bets_cnt__w15m_raw AS DOUBLE) AS fe__bets_cnt__w15m,
  CAST(fe__wager_sum__w15m_raw AS DOUBLE) AS fe__wager_sum__w15m,
  CAST(COALESCE(bets_today_so_far, 0) AS DOUBLE) AS fe__canonical__bets_cnt__today,
  CAST(COALESCE(wager_today_so_far, 0.0) AS DOUBLE) AS fe__canonical__wager_sum__today,
  CASE
    WHEN bets_today_so_far IS NOT NULL AND bets_today_so_far > 0 AND wager_today_so_far IS NOT NULL
    THEN CAST(wager_today_so_far / bets_today_so_far AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__canonical__avg_wager__today,
  CAST(EXTRACT(epoch FROM (pcd - first_pcd_today)) AS DOUBLE)
    AS fe__canonical__elapsed_sec_since_first_bet__today,
  CAST(EXTRACT(epoch FROM (lag1_pcd - lag2_pcd)) AS DOUBLE) AS fe__interarrival__lag2_sec,
  CASE
    WHEN interarrival_avg_w1h IS NOT NULL AND interarrival_avg_w1h > 1e-9
       AND interarrival_sec IS NOT NULL
    THEN CAST(interarrival_sec / interarrival_avg_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival__last_gap_to_recent_mean_ratio__w1h,
  CASE
    WHEN interarrival_avg_w1h IS NOT NULL AND interarrival_avg_w1h > 1e-9
       AND interarrival_std_w1h IS NOT NULL
    THEN CAST(interarrival_std_w1h / interarrival_avg_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival__cv__w1h,
  CASE
    WHEN payout_odds_std_w1h IS NOT NULL AND payout_odds_std_w1h > 1e-12
       AND payout_odds_avg_w1h IS NOT NULL AND payout_odds IS NOT NULL
    THEN CAST((payout_odds - payout_odds_avg_w1h) / payout_odds_std_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_z__w1h,
  CASE
    WHEN payout_odds_std_w7d IS NOT NULL AND payout_odds_std_w7d > 1e-12
       AND payout_odds_avg_w7d IS NOT NULL AND payout_odds IS NOT NULL
    THEN CAST((payout_odds - payout_odds_avg_w7d) / payout_odds_std_w7d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_z__w7d,
  CASE
    WHEN max_payout_odds_w1h IS NOT NULL AND max_payout_odds_w1h > 1e-9 AND payout_odds IS NOT NULL
    THEN CAST(payout_odds / max_payout_odds_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_to_recent_max_ratio__w1h,
  CASE
    WHEN lag1_payout_odds IS NOT NULL AND lag1_payout_odds > 1e-9 AND payout_odds IS NOT NULL
    THEN CAST(payout_odds / lag1_payout_odds AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_step_ratio
FROM ordered
WHERE bet_id IN (SELECT bet_id FROM tid)
""".strip()

# Per-target-bet pool slice: windows partition by ``target_bet_id`` so co-batch neighbors
# cannot widen another bet's PIT history.
_FE_DERIVED_BOUNDED_SRC: Final[str] = """
scoring_bounds AS (
  SELECT
    TRY_CAST(bet_id AS DOUBLE) AS target_bet_id,
    TRY_CAST(player_id AS BIGINT) AS player_id,
    TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id,
    CAST(pool_start AS TIMESTAMPTZ) AS pool_start,
    CAST(scoring_pcd AS TIMESTAMPTZ) AS scoring_pcd
  FROM staged_bounds
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
    AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
),
src AS (
  SELECT
    sb.target_bet_id,
    TRY_CAST(b."bet_id" AS DOUBLE) AS bet_id,
    TRY_CAST(b."player_id" AS BIGINT) AS player_id,
    TRIM(CAST(b."canonical_id" AS VARCHAR)) AS canonical_id,
    TRY_CAST(b."session_id" AS BIGINT) AS session_id,
    TRY_CAST(b."table_id" AS BIGINT) AS table_id,
    TRY_CAST(b."game_id" AS BIGINT) AS game_id,
    TRY_CAST(b."gaming_day_event" AS DATE) AS gaming_day_event,
    CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) AS pcd,
    TRY_CAST(b."wager" AS DOUBLE) AS wager,
    TRY_CAST(b."payout_odds" AS DOUBLE) AS payout_odds,
    TRY_CAST(b."casino_win" AS DOUBLE) AS casino_win,
    TRY_CAST(b."theo_win" AS DOUBLE) AS theo_win,
    TRY_CAST(b."base_ha" AS DOUBLE) AS base_ha
  FROM pool_src AS b
  INNER JOIN scoring_bounds AS sb
    ON TRIM(CAST(b."canonical_id" AS VARCHAR)) = sb.canonical_id
   AND TRY_CAST(b."player_id" AS BIGINT) = sb.player_id
  WHERE TRY_CAST(b."bet_id" AS DOUBLE) IS NOT NULL
    AND TRY_CAST(b."player_id" AS BIGINT) IS NOT NULL
    AND b."payout_complete_dtm" IS NOT NULL
    AND TRIM(CAST(b."canonical_id" AS VARCHAR)) <> ''
    AND CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) >= sb.pool_start
    AND CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) <= sb.scoring_pcd
),
""".strip()

_FE_DERIVED_PIPELINE_BOUNDED_AFTER_SRC: Final[str] = """
src_lagged AS (
  SELECT s.*,
    LAG(pcd) OVER w_target AS lag1_pcd,
    LAG(pcd, 2) OVER w_target AS lag2_pcd,
    LAG(table_id) OVER w_target AS lag1_table_id,
    LAG(payout_odds) OVER w_target AS lag1_payout_odds,
    LAG(wager) OVER w_target AS lag1_wager,
    LAG(casino_win) OVER w_target AS lag1_casino_win,
    COUNT(*) OVER w_target_day_prior AS bets_today_so_far,
    SUM(wager) OVER w_target_day_prior AS wager_today_so_far,
    MIN(pcd) OVER w_target_day_inclusive AS first_pcd_today
  FROM src AS s
  WINDOW
    w_target AS (PARTITION BY target_bet_id ORDER BY pcd, bet_id),
    w_target_day_prior AS (
      PARTITION BY target_bet_id, gaming_day_event ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ),
    w_target_day_inclusive AS (
      PARTITION BY target_bet_id, gaming_day_event ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
),
src_with_iv AS (
  SELECT s.*,
    EXTRACT(epoch FROM (pcd - lag1_pcd)) AS interarrival_sec,
    SUM(CASE WHEN COALESCE(casino_win, 0) <= 0 THEN 1 ELSE 0 END)
      OVER (
        PARTITION BY target_bet_id ORDER BY pcd, bet_id
        ROWS UNBOUNDED PRECEDING
      ) AS _loss_streak_grp,
    CASE
      WHEN lag1_casino_win > 0 AND lag1_wager > 1e-9 AND wager >= 2.0 * lag1_wager THEN 1.0
      ELSE 0.0
    END AS loss_then_double_flag
  FROM src_lagged AS s
),
ordered AS (
  SELECT s.*,
    COUNT(*) OVER w5m AS cnt_w5m,
    COUNT(*) OVER w15 AS fe__bets_cnt__w15m_raw,
    COALESCE(SUM(wager) OVER w15, 0.0) AS fe__wager_sum__w15m_raw,
    COALESCE(SUM(casino_win) OVER w15, 0.0) AS cw_sum_w15m,
    COALESCE(SUM(casino_win) OVER w15_peer, 0.0) AS cw_sum_w15m_peer,
    COALESCE(SUM(casino_win) OVER w1h, 0.0) AS cw_sum_w1h,
    COALESCE(SUM(casino_win) OVER w1h_peer, 0.0) AS cw_sum_w1h_peer,
    COALESCE(SUM(theo_win) OVER w1h, 0.0) AS tw_sum_w1h,
    COALESCE(SUM(theo_win) OVER w1h_peer, 0.0) AS tw_sum_w1h_peer,
    COALESCE(SUM(CASE WHEN casino_win > 0 THEN 1 ELSE 0 END) OVER w1h, 0) AS loss_cnt_w1h,
    COUNT(*) OVER w1h AS cnt_w1h,
    COUNT(*) OVER w1d AS fe__bets_cnt__w1d_raw,
    COALESCE(SUM(wager) OVER w1d, 0.0) AS fe__wager_sum__w1d_raw,
    COUNT(*) OVER w7d AS fe__bets_cnt__w7d_raw,
    COALESCE(SUM(wager) OVER w7d, 0.0) AS fe__wager_sum__w7d_raw,
    COUNT(*) OVER w30d AS fe__bets_cnt__w30d_raw,
    COALESCE(SUM(wager) OVER w30d, 0.0) AS fe__wager_sum__w30d_raw,
    AVG(wager) OVER w1h AS avg_wager_w1h,
    STDDEV_POP(wager) OVER w1h AS std_wager_w1h,
    COALESCE(STDDEV_POP(wager) OVER w7d, CAST(NULL AS DOUBLE)) AS std_wager_w7d,
    COALESCE(AVG(ABS(wager)) OVER w7d, CAST(NULL AS DOUBLE)) AS avg_abs_wager_w7d,
    COALESCE(AVG(wager) OVER w30d, CAST(NULL AS DOUBLE)) AS prior_wager_mean_w30d,
    COALESCE(STDDEV_POP(wager) OVER w30d, CAST(NULL AS DOUBLE)) AS prior_wager_std_w30d,
    COALESCE(AVG(payout_odds) OVER w30d, CAST(NULL AS DOUBLE)) AS prior_odds_mean_w30d,
    COALESCE(STDDEV_POP(payout_odds) OVER w30d, CAST(NULL AS DOUBLE)) AS prior_odds_std_w30d,
    AVG(payout_odds) OVER w1h AS payout_odds_avg_w1h,
    STDDEV_POP(payout_odds) OVER w1h AS payout_odds_std_w1h,
    AVG(interarrival_sec) OVER w1h AS interarrival_avg_w1h,
    STDDEV_POP(interarrival_sec) OVER w1h AS interarrival_std_w1h,
    AVG(interarrival_sec) OVER w7d AS interarrival_avg_w7d,
    STDDEV_POP(interarrival_sec) OVER w7d AS interarrival_std_w7d,
    AVG(payout_odds) OVER w7d AS payout_odds_avg_w7d,
    STDDEV_POP(payout_odds) OVER w7d AS payout_odds_std_w7d,
    MAX(wager) OVER w1h AS max_wager_w1h,
    MAX(payout_odds) OVER w1h AS max_payout_odds_w1h,
    CAST(
      CASE
        WHEN COALESCE(casino_win, 0) > 0 THEN ROW_NUMBER() OVER (
          PARTITION BY target_bet_id, _loss_streak_grp ORDER BY pcd, bet_id
        )
        ELSE 0
      END AS DOUBLE
    ) AS fe__outcome__consecutive_loss_streak,
    CASE
      WHEN COALESCE(SUM(CASE WHEN casino_win > 0 THEN 1 ELSE 0 END) OVER w1h, 0) > 0
      THEN CAST(
        SUM(loss_then_double_flag) OVER w1h
        / COALESCE(SUM(CASE WHEN casino_win > 0 THEN 1 ELSE 0 END) OVER w1h, 0)
        AS DOUBLE)
      ELSE CAST(NULL AS DOUBLE)
    END AS fe__outcome__loss_then_double_ratio__w1h,
    CAST(
      AVG(
        CASE
          WHEN lag1_casino_win > 0 AND lag1_wager > 1e-9 THEN wager / lag1_wager
          ELSE NULL
        END
      ) OVER w1h AS DOUBLE
    ) AS fe__outcome__wager_after_loss_step_ratio__w1h,
    REGR_SLOPE(wager, EXTRACT(epoch FROM pcd)) OVER w1h_peer AS wager_regr_slope_w1h_peer,
    AVG(wager) OVER w_last3 AS wager_avg_last3,
    AVG(wager) OVER w_prior3 AS wager_avg_prior3
  FROM src_with_iv AS s
  WINDOW
    w5m AS (
      PARTITION BY target_bet_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '5 MINUTE' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w15 AS (
      PARTITION BY target_bet_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '15 MINUTE' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w15_peer AS (
      PARTITION BY target_bet_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '15 MINUTE' PRECEDING AND CURRENT ROW
    ),
    w1h AS (
      PARTITION BY target_bet_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 HOUR' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w1h_peer AS (
      PARTITION BY target_bet_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 HOUR' PRECEDING AND CURRENT ROW
    ),
    w1d AS (
      PARTITION BY target_bet_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w7d AS (
      PARTITION BY target_bet_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '7 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w30d AS (
      PARTITION BY target_bet_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '30 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w_last3 AS (
      PARTITION BY target_bet_id ORDER BY pcd, bet_id
      ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
    ),
    w_prior3 AS (
      PARTITION BY target_bet_id ORDER BY pcd, bet_id
      ROWS BETWEEN 5 PRECEDING AND 3 PRECEDING
    )
)
SELECT
  target_bet_id AS bet_id,
  CAST(interarrival_sec AS DOUBLE) AS fe__time_since_last_bet_sec,
  CAST(fe__bets_cnt__w15m_raw AS DOUBLE) AS fe__bets_cnt__w15m,
  CAST(fe__wager_sum__w15m_raw AS DOUBLE) AS fe__wager_sum__w15m,
  CAST(cnt_w5m AS DOUBLE) AS fe__rate__bets_cnt__w5m,
  CAST(fe__bets_cnt__w1d_raw AS DOUBLE) AS fe__bets_cnt__w1d,
  CAST(fe__wager_sum__w1d_raw AS DOUBLE) AS fe__wager_sum__w1d,
  CAST(fe__bets_cnt__w7d_raw AS DOUBLE) AS fe__bets_cnt__w7d,
  CAST(fe__wager_sum__w7d_raw AS DOUBLE) AS fe__wager_sum__w7d,
  CAST(fe__bets_cnt__w30d_raw AS DOUBLE) AS fe__bets_cnt__w30d,
  CAST(fe__wager_sum__w30d_raw AS DOUBLE) AS fe__wager_sum__w30d,
  CAST(cw_sum_w15m_peer - COALESCE(casino_win, 0.0) AS DOUBLE) AS fe__outcome__casino_win_sum__w15m,
  CAST(cw_sum_w1h_peer - COALESCE(casino_win, 0.0) AS DOUBLE) AS fe__outcome__casino_win_sum__w1h,
  CASE
    WHEN (tw_sum_w1h_peer - COALESCE(theo_win, 0.0)) > 1e-9
    THEN CAST(
      (cw_sum_w1h_peer - COALESCE(casino_win, 0.0))
      / (tw_sum_w1h_peer - COALESCE(theo_win, 0.0))
      AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__outcome__casino_win_to_theo_ratio__w1h,
  fe__outcome__consecutive_loss_streak,
  fe__outcome__loss_then_double_ratio__w1h,
  fe__outcome__wager_after_loss_step_ratio__w1h,
  CASE
    WHEN fe__wager_sum__w1d_raw > 1e-9
    THEN CAST(fe__wager_sum__w15m_raw / fe__wager_sum__w1d_raw AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__wager_sum__w15m_over_w1d,
  CASE
    WHEN fe__bets_cnt__w1d_raw > 1e-9
    THEN CAST(fe__bets_cnt__w15m_raw / fe__bets_cnt__w1d_raw AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__bets_cnt__w15m_over_w1d,
  CASE
    WHEN fe__wager_sum__w30d_raw > 1e-9
    THEN CAST(fe__wager_sum__w7d_raw / fe__wager_sum__w30d_raw AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__wager_sum__w7d_over_w30d,
  CASE
    WHEN fe__bets_cnt__w15m_raw > 0
    THEN CAST(cnt_w5m * 3.0 / fe__bets_cnt__w15m_raw AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__rate__velocity__w5m_over_w15m,
  CASE
    WHEN cnt_w1h > 0
    THEN CAST(fe__bets_cnt__w15m_raw * 4.0 / cnt_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__rate__velocity__w15m_over_w1h,
  CASE
    WHEN std_wager_w1h IS NOT NULL AND ABS(std_wager_w1h) > 1e-12
       AND avg_wager_w1h IS NOT NULL AND wager IS NOT NULL
    THEN CAST((wager - avg_wager_w1h) / std_wager_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_z__w1h,
  CASE
    WHEN avg_wager_w1h IS NOT NULL AND avg_wager_w1h > 1e-12 AND std_wager_w1h IS NOT NULL
    THEN CAST(std_wager_w1h / avg_wager_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_cv__w1h,
  CASE
    WHEN wager_regr_slope_w1h_peer IS NOT NULL
       AND avg_wager_w1h IS NOT NULL AND avg_wager_w1h > 1e-9
    THEN CAST(wager_regr_slope_w1h_peer / avg_wager_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_trend_slope__w1h,
  CASE
    WHEN wager_avg_prior3 IS NOT NULL AND wager_avg_prior3 > 1e-9
       AND wager_avg_last3 IS NOT NULL
    THEN CAST(wager_avg_last3 / wager_avg_prior3 AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_last3_vs_prior3_ratio__w1h,
  CASE
    WHEN avg_abs_wager_w7d IS NOT NULL AND avg_abs_wager_w7d > 1e-12 AND std_wager_w7d IS NOT NULL
    THEN CAST(std_wager_w7d / avg_abs_wager_w7d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__wager_cv_w7d,
  CASE
    WHEN prior_wager_std_w30d IS NOT NULL AND ABS(prior_wager_std_w30d) > 1e-12
    THEN CAST((wager - prior_wager_mean_w30d) / prior_wager_std_w30d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__wager_z_prior_w30d,
  CASE
    WHEN prior_odds_std_w30d IS NOT NULL AND ABS(prior_odds_std_w30d) > 1e-12
       AND payout_odds IS NOT NULL
    THEN CAST((payout_odds - prior_odds_mean_w30d) / prior_odds_std_w30d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__payout_odds_z_prior_w30d,
  CASE
    WHEN max_wager_w1h IS NOT NULL AND max_wager_w1h > 1e-9 AND wager IS NOT NULL
    THEN CAST(wager / max_wager_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__stake__wager_to_recent_max_ratio__w1h,
  CAST(COALESCE(bets_today_so_far, 0) AS DOUBLE) AS fe__canonical__bets_cnt__today,
  CAST(COALESCE(wager_today_so_far, 0.0) AS DOUBLE) AS fe__canonical__wager_sum__today,
  CASE
    WHEN bets_today_so_far IS NOT NULL AND bets_today_so_far > 0 AND wager_today_so_far IS NOT NULL
    THEN CAST(wager_today_so_far / bets_today_so_far AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__canonical__avg_wager__today,
  CAST(EXTRACT(epoch FROM (pcd - first_pcd_today)) AS DOUBLE)
    AS fe__canonical__elapsed_sec_since_first_bet__today,
  CAST(EXTRACT(epoch FROM (lag1_pcd - lag2_pcd)) AS DOUBLE) AS fe__interarrival__lag2_sec,
  CASE
    WHEN interarrival_avg_w1h IS NOT NULL AND interarrival_avg_w1h > 1e-9
       AND interarrival_sec IS NOT NULL
    THEN CAST(interarrival_sec / interarrival_avg_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival__last_gap_to_recent_mean_ratio__w1h,
  CASE
    WHEN interarrival_avg_w1h IS NOT NULL AND interarrival_avg_w1h > 1e-9
       AND interarrival_std_w1h IS NOT NULL
    THEN CAST(interarrival_std_w1h / interarrival_avg_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival__cv__w1h,
  CASE
    WHEN interarrival_std_w7d IS NOT NULL AND interarrival_std_w7d > 1e-9
       AND interarrival_avg_w7d IS NOT NULL AND interarrival_sec IS NOT NULL
    THEN CAST((interarrival_sec - interarrival_avg_w7d) / interarrival_std_w7d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__interarrival__last_gap_z__w7d,
  CASE
    WHEN payout_odds_std_w1h IS NOT NULL AND payout_odds_std_w1h > 1e-12
       AND payout_odds_avg_w1h IS NOT NULL AND payout_odds IS NOT NULL
    THEN CAST((payout_odds - payout_odds_avg_w1h) / payout_odds_std_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_z__w1h,
  CASE
    WHEN payout_odds_std_w7d IS NOT NULL AND payout_odds_std_w7d > 1e-12
       AND payout_odds_avg_w7d IS NOT NULL AND payout_odds IS NOT NULL
    THEN CAST((payout_odds - payout_odds_avg_w7d) / payout_odds_std_w7d AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_z__w7d,
  CASE
    WHEN max_payout_odds_w1h IS NOT NULL AND max_payout_odds_w1h > 1e-9 AND payout_odds IS NOT NULL
    THEN CAST(payout_odds / max_payout_odds_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_to_recent_max_ratio__w1h,
  CASE
    WHEN lag1_payout_odds IS NOT NULL AND lag1_payout_odds > 1e-9 AND payout_odds IS NOT NULL
    THEN CAST(payout_odds / lag1_payout_odds AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__odds__payout_odds_step_ratio,
  CAST(EXTRACT(hour FROM pcd AT TIME ZONE 'Asia/Hong_Kong') AS DOUBLE) AS fe__clock__hour_of_day,
  CAST(EXTRACT(isodow FROM pcd AT TIME ZONE 'Asia/Hong_Kong') AS DOUBLE) AS fe__clock__day_of_week,
  CASE
    WHEN EXTRACT(isodow FROM pcd AT TIME ZONE 'Asia/Hong_Kong') >= 6 THEN 1.0
    ELSE 0.0
  END AS fe__clock__is_weekend,
  CASE
    WHEN EXTRACT(hour FROM pcd AT TIME ZONE 'Asia/Hong_Kong') BETWEEN 0 AND 5 THEN 1.0
    ELSE 0.0
  END AS fe__clock__is_late_night
FROM ordered
WHERE target_bet_id IN (SELECT bet_id FROM tid)
  AND bet_id = target_bet_id
""".strip()


def _infer_scoring_bounds_from_pool(
    pool: pd.DataFrame,
    target_bet_ids: pd.Series,
    *,
    cfg: HightierServingConfig,
) -> pd.DataFrame:
    """Build per-bet bounds from pool rows matching target ``bet_id`` values."""
    from trainer_hightier.serving.scorer import compute_scoring_bounds_for_bets

    targets = pd.to_numeric(target_bet_ids, errors="coerce").dropna().unique()
    if len(targets) == 0:
        return pd.DataFrame(columns=["bet_id", "canonical_id", "pool_start", "scoring_pcd"])
    work = pool.copy()
    work["bet_id"] = pd.to_numeric(work["bet_id"], errors="coerce")
    staged = work.loc[work["bet_id"].isin(targets)]
    if staged.empty:
        raise ValueError(
            "scoring_bounds inference found no pool rows for target bet_id(s); "
            f"targets={targets[:8].tolist()}",
        )
    cols = ["bet_id", "player_id", "payout_complete_dtm"]
    if "canonical_id" in staged.columns:
        cols.append("canonical_id")
    if "gaming_day_event" in staged.columns:
        cols.append("gaming_day_event")
    return compute_scoring_bounds_for_bets(staged.loc[:, cols], cfg=cfg)


def _prepare_scoring_bounds(bounds: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalize scoring-bound rows for DuckDB registration."""
    need = ("bet_id", "player_id", "canonical_id", "pool_start", "scoring_pcd")
    missing = [c for c in need if c not in bounds.columns]
    if missing:
        raise ValueError(
            f"scoring_bounds missing columns {missing}; got {list(bounds.columns)}",
        )
    out = bounds.loc[:, list(need)].copy()
    out["bet_id"] = pd.to_numeric(out["bet_id"], errors="coerce")
    out["player_id"] = pd.to_numeric(out["player_id"], errors="coerce")
    out["canonical_id"] = out["canonical_id"].astype(str).str.strip()
    out["pool_start"] = pd.to_datetime(out["pool_start"], errors="coerce")
    out["scoring_pcd"] = pd.to_datetime(out["scoring_pcd"], errors="coerce")
    out = out.dropna(subset=["bet_id", "player_id", "canonical_id", "pool_start", "scoring_pcd"])
    out = out.loc[out["canonical_id"] != ""]
    if out.empty:
        raise ValueError("scoring_bounds has no valid rows after normalization")
    return out.reset_index(drop=True)


def _prepare_pool_for_fe_derived(pool: pd.DataFrame) -> pd.DataFrame:
    """Normalize bounded bet pool columns for in-memory fe derived PIT.

    ``t_bet`` money fields are ``Decimal(19,4)`` in ClickHouse (see
    ``schema/GDP_GMWDS_Raw_Schema_Dictionary.md`` §4: ``payout_odds`` up to
    ``100.0000``). Coerce to float64 before ``con.register`` so DuckDB does not
    infer an undersized DECIMAL from the pool sample.
    """

    need = (
        "bet_id",
        "player_id",
        "canonical_id",
        "session_id",
        "table_id",
        "game_id",
        "gaming_day_event",
        "payout_complete_dtm",
        "wager",
        "payout_odds",
        "casino_win",
    )
    missing = [c for c in need if c not in pool.columns]
    if missing:
        raise ValueError(
            f"pool missing columns required for short-term PIT: {missing}; "
            f"have={list(pool.columns)}"
        )
    optional = tuple(c for c in ("theo_win", "base_ha") if c in pool.columns)
    work = pool.loc[:, [*need, *optional]].copy()
    for opt in ("theo_win", "base_ha"):
        if opt not in work.columns:
            work[opt] = np.nan
    numeric_cols = (
        "bet_id",
        "player_id",
        "session_id",
        "table_id",
        "wager",
        "payout_odds",
        "casino_win",
        "theo_win",
        "base_ha",
    )
    for col in numeric_cols:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    return work


def compute_fe_derived_features_from_pool(
    pool: pd.DataFrame,
    target_bet_ids: pd.Series,
    *,
    scoring_bounds: pd.DataFrame | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> pd.DataFrame:
    """Compute bet-grain short-term ``fe__*`` for ``target_bet_ids`` from a bounded pool."""

    if pool.empty or target_bet_ids.empty:
        return pd.DataFrame(columns=["bet_id"])
    work_pool = _prepare_pool_for_fe_derived(pool)
    tid = pd.DataFrame({"bet_id": pd.to_numeric(target_bet_ids, errors="coerce")}).dropna()
    if tid.empty:
        return pd.DataFrame(columns=["bet_id"])
    runtime = duckdb_runtime or DuckDbRuntimeConfig()
    from trainer_hightier.config import default_hightier_serving_config

    bounds = scoring_bounds
    if bounds is None:
        bounds = _infer_scoring_bounds_from_pool(
            work_pool,
            tid["bet_id"],
            cfg=default_hightier_serving_config(),
        )
    bounds = _prepare_scoring_bounds(bounds)
    sql = f"""
WITH tid AS (
  SELECT DISTINCT TRY_CAST(bet_id AS DOUBLE) AS bet_id
  FROM staged_tid
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
),
{_FE_DERIVED_BOUNDED_SRC}
{_FE_DERIVED_PIPELINE_BOUNDED_AFTER_SRC}
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, runtime)
        con.register("pool_src", work_pool)
        con.register("staged_tid", tid)
        con.register("staged_bounds", bounds)
        return con.execute(sql).df()
    finally:
        con.close()
