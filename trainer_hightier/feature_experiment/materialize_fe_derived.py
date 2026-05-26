"""Materialize ``fe__*`` window features from cleaned bet (DuckDB, read-only ingest).

Windows mirror :mod:`trainer_hightier.utils.trial_bet_behavior_1h`:
``RANGE ... PRECEDING`` excludes the current row. Patron-level windows partition
by ``canonical_id`` (aligned with walkaway labels). The scan universe is all
``player_id`` rows that map to any ``canonical_id`` seen on training ``bet_id``
rows (so multi-card patrons include history under every linked ``player_id``).

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

BOUNDED_SHORT_TERM_MATERIALIZER_VERSION: Final[str] = "bounded_hot_pool_v1"

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
    "gaming_day",
)


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def materialize_fe_derived_parquet(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
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
    bet_from = resolved_cleaned_bet_read_parquet_sql(src_root)
    tp_esc = _path_esc(tp).replace("'", "''")
    cmap_esc = _path_esc(cmap_path).replace("'", "''")
    dst_esc = _path_esc(dst).replace("'", "''")

    sql = f"""
WITH tid AS (
  SELECT DISTINCT TRY_CAST(bet_id AS DOUBLE) AS bet_id
  FROM read_parquet('{tp_esc}')
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
),
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
    TRY_CAST(b."gaming_day" AS DATE) AS gaming_day,
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
      PARTITION BY canonical_id, gaming_day ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ),
    w_canon_day_inclusive AS (
      PARTITION BY canonical_id, gaming_day ORDER BY pcd, bet_id
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
    END AS changed_table_vs_lag1
  FROM src_lagged AS s
),
ordered AS (
  SELECT s.*,
    COUNT(*) OVER w5m AS cnt_w5m,
    COUNT(*) OVER w15 AS fe__bets_cnt__w15m_raw,
    COALESCE(SUM(wager) OVER w15, 0.0) AS fe__wager_sum__w15m_raw,
    COALESCE(SUM(casino_win) OVER w15, 0.0) AS cw_sum_w15m,
    COUNT(*) OVER w1h AS cnt_w1h,
    COALESCE(SUM(wager) OVER w1h, 0.0) AS wager_sum_w1h,
    COALESCE(SUM(casino_win) OVER w1h, 0.0) AS cw_sum_w1h,
    COALESCE(SUM(theo_win) OVER w1h, 0.0) AS tw_sum_w1h,
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
    STDDEV_POP(payout_odds) OVER w7d AS payout_odds_std_w7d
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
    w1h AS (
      PARTITION BY canonical_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 HOUR' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
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
  CAST(cw_sum_w15m AS DOUBLE) AS fe__outcome__casino_win_sum__w15m,
  CAST(cw_sum_w1h AS DOUBLE) AS fe__outcome__casino_win_sum__w1h,
  CASE
    WHEN cnt_w1h > 0 THEN CAST(loss_cnt_w1h * 1.0 / cnt_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__outcome__loss_rate__w1h,
  CASE
    WHEN wager_sum_w1h > 1e-9 THEN CAST(cw_sum_w1h / wager_sum_w1h AS DOUBLE)
    ELSE CAST(NULL AS DOUBLE)
  END AS fe__outcome__net_pnl_to_wager_ratio__w1h,
  CAST(
    CASE WHEN lag1_casino_win IS NOT NULL AND lag1_casino_win > 0 THEN 1 ELSE 0 END
    + CASE WHEN lag2_casino_win IS NOT NULL AND lag2_casino_win > 0 THEN 1 ELSE 0 END
    + CASE WHEN lag3_casino_win IS NOT NULL AND lag3_casino_win > 0 THEN 1 ELSE 0 END
    AS DOUBLE
  ) AS fe__outcome__last_3_bets_loss_count,

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
FROM ordered
WHERE bet_id IN (SELECT bet_id FROM tid)
"""

    sql = sql.strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({sql}) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()
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
) -> Iterator[pd.DataFrame]:
    """Yield chronological training bet slices without loading the full table into memory."""

    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1; got {batch_size}")
    t_esc = _path_esc(training_parquet)
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
        total_row = con.execute(
            f"""
            SELECT COUNT(*)::BIGINT AS n
            FROM read_parquet('{t_esc}')
            WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
              AND payout_complete_dtm IS NOT NULL
              AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
            """,
        ).fetchone()
        total = int(total_row[0]) if total_row else 0
        for start in range(0, total, batch_size):
            end = min(start + batch_size, total)
            df = con.execute(
                f"""
                WITH ordered AS (
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
                    CAST(gaming_day AS TIMESTAMP) AS gaming_day,
                    ROW_NUMBER() OVER (
                      ORDER BY CAST(payout_complete_dtm AS TIMESTAMPTZ) ASC,
                               TRY_CAST(bet_id AS DOUBLE) ASC
                    ) AS rn
                  FROM read_parquet('{t_esc}')
                  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
                    AND payout_complete_dtm IS NOT NULL
                    AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
                )
                SELECT * EXCLUDE (rn)
                FROM ordered
                WHERE rn > {start} AND rn <= {end}
                """,
            ).fetchdf()
            if not df.empty:
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
) -> pd.DataFrame:
    """Compute production-aligned short-term ``bet__*`` and ``fe__*`` for one scoring batch."""

    from trainer_hightier.serving.feature_builder import (
        attach_canonical_id,
        attach_synthetic_etl_and_prediction_visible,
        attach_trial_bet_behavior_1h,
    )
    from trainer_hightier.serving.offline_serving_backtest import build_pool_from_cleaned_parquet

    if bets_batch.empty:
        return pd.DataFrame(columns=["bet_id", *trial_columns, *fe_columns])
    work = bets_batch.copy()
    work["__etl_insert_Dtm"] = pd.to_datetime(work["payout_complete_dtm"], errors="coerce")
    pool = build_pool_from_cleaned_parquet(
        work,
        cleaned_root=cleaned_bet_parquet,
        cfg=serving_cfg,
        mapping_parquet=mapping_parquet,
        expand_canonical_aliases=False,
    )
    pool = attach_canonical_id(pool, mapping_parquet=mapping_parquet)
    staged = attach_synthetic_etl_and_prediction_visible(work)
    staged = attach_canonical_id(staged, mapping_parquet=mapping_parquet)
    staged = attach_trial_bet_behavior_1h(staged, pool, duckdb_runtime=duckdb_runtime)
    fe_part = (
        compute_fe_derived_features_from_pool(pool, staged["bet_id"], duckdb_runtime=duckdb_runtime)
        if fe_columns
        else pd.DataFrame({"bet_id": staged["bet_id"]})
    )
    out = pd.DataFrame({"bet_id": pd.to_numeric(staged["bet_id"], errors="coerce")})
    for col in trial_columns:
        out[col] = staged[col].to_numpy()
    for col in fe_columns:
        if col not in fe_part.columns:
            raise ValueError(
                f"bounded fe__ materialization missing column {col!r}; got {list(fe_part.columns)}",
            )
        out[col] = fe_part[col].to_numpy()
    return out


def materialize_fe_derived_short_term_parquet(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    short_term_columns: tuple[str, ...] | None = None,
    trial_columns: tuple[str, ...] | None = None,
) -> Path:
    """Materialize bounded hot-pool short-term ``bet__*`` and ``fe__*`` (train–serve aligned).

    Uses the same pool window and batch size as scorer v2; does not scan full bet history.
    Mid-term model columns are supplied separately via daily snapshot ASOF enrich.
    """

    fe_cols = tuple(short_term_columns or ())
    trial_cols = tuple(trial_columns if trial_columns is not None else SHORT_TERM_TRIAL_BET_COLUMNS)
    if not fe_cols and not trial_cols:
        raise ValueError("short_term materialization requires at least one fe__ or bet__ column")
    out_cols = tuple(dict.fromkeys(("bet_id", *trial_cols, *fe_cols)))

    serving_cfg = default_hightier_serving_config()
    batch_size = int(serving_cfg.hightier_scorer_max_bets_per_cycle)
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
    for bets_batch in _iter_training_bet_batches(
        Path(training_parquet_for_bet_ids).resolve(),
        batch_size=batch_size,
        duckdb_runtime=duckdb_runtime,
    ):
        features = _short_term_features_for_batch(
            bets_batch,
            cleaned_bet_parquet=Path(cleaned_bet_parquet).resolve(),
            mapping_parquet=cmap,
            serving_cfg=serving_cfg,
            duckdb_runtime=duckdb_runtime,
            fe_columns=fe_cols,
            trial_columns=trial_cols,
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
        batch_size,
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
      PARTITION BY canonical_id, gaming_day ORDER BY pcd, bet_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ),
    w_canon_day_inclusive AS (
      PARTITION BY canonical_id, gaming_day ORDER BY pcd, bet_id
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
        "gaming_day",
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
    sql = f"""
WITH tid AS (
  SELECT DISTINCT TRY_CAST(bet_id AS DOUBLE) AS bet_id
  FROM staged_tid
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
),
src AS (
  SELECT
    TRY_CAST(b."bet_id" AS DOUBLE) AS bet_id,
    TRY_CAST(b."player_id" AS BIGINT) AS player_id,
    TRIM(CAST(b."canonical_id" AS VARCHAR)) AS canonical_id,
    TRY_CAST(b."session_id" AS BIGINT) AS session_id,
    TRY_CAST(b."table_id" AS BIGINT) AS table_id,
    TRY_CAST(b."gaming_day" AS DATE) AS gaming_day,
    CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) AS pcd,
    TRY_CAST(b."wager" AS DOUBLE) AS wager,
    TRY_CAST(b."payout_odds" AS DOUBLE) AS payout_odds,
    TRY_CAST(b."casino_win" AS DOUBLE) AS casino_win,
    TRY_CAST(b."theo_win" AS DOUBLE) AS theo_win,
    TRY_CAST(b."base_ha" AS DOUBLE) AS base_ha
  FROM pool_src AS b
  WHERE TRY_CAST(b."bet_id" AS DOUBLE) IS NOT NULL
    AND TRY_CAST(b."player_id" AS BIGINT) IS NOT NULL
    AND b."payout_complete_dtm" IS NOT NULL
    AND TRIM(CAST(b."canonical_id" AS VARCHAR)) <> ''
),
{_FE_DERIVED_PIPELINE_AFTER_SRC}
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, runtime)
        con.register("pool_src", work_pool)
        con.register("staged_tid", tid)
        return con.execute(sql).df()
    finally:
        con.close()
