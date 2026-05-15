"""Materialize ``fe__*`` window features from cleaned bet (DuckDB, read-only ingest).

Windows mirror :mod:`trainer_hightier.utils.trial_bet_behavior_1h`:
``RANGE ... PRECEDING`` excludes the current row. Only ``player_id`` timelines
included in ``training_parquet`` are scanned to cap cost.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_dataset_has_any_parquet,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def materialize_fe_derived_parquet(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Compute ``fe__*`` columns keyed by ``bet_id``; write standalone Parquet.

    Args:
        cleaned_bet_parquet: Hive-partition cleaned bet root (trainer default layout).
        training_parquet_for_bet_ids: Step-3-style training parquet; used to derive
            the ``player_id`` universe to scan (reduces DuckDB workload).
        out_parquet: Output path (written with Snappy compression).
        duckdb_runtime: DuckDB PRAGMA preset.

    Returns:
        Resolved ``out_parquet`` path.

    Raises:
        FileNotFoundError: If cleaned bet glob yields no readable Parquet or training
            parquet is missing.
    """

    src_root = Path(cleaned_bet_parquet).resolve()
    tp = Path(training_parquet_for_bet_ids).resolve()
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not tp.is_file():
        raise FileNotFoundError(training_parquet_for_bet_ids)
    if not cleaned_bet_dataset_has_any_parquet(src_root):
        raise FileNotFoundError(f"No cleaned bet parquet under {src_root}")
    bet_from = resolved_cleaned_bet_read_parquet_sql(src_root)
    tp_esc = _path_esc(tp).replace("'", "''")
    dst_esc = _path_esc(dst).replace("'", "''")

    sql = f"""
WITH tid AS (
  SELECT DISTINCT TRY_CAST(bet_id AS DOUBLE) AS bet_id
  FROM read_parquet('{tp_esc}')
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
),
pid AS (
  SELECT DISTINCT TRY_CAST(_b.player_id AS BIGINT) AS player_id
  FROM {bet_from} AS _b
  INNER JOIN tid ON TRY_CAST(_b.bet_id AS DOUBLE) = tid.bet_id
  WHERE TRY_CAST(_b.player_id AS BIGINT) IS NOT NULL
),
src AS (
  SELECT
    TRY_CAST(b."bet_id" AS DOUBLE) AS bet_id,
    TRY_CAST(b."player_id" AS BIGINT) AS player_id,
    CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) AS pcd,
    TRY_CAST(b."wager" AS DOUBLE) AS wager,
    TRY_CAST(b."payout_odds" AS DOUBLE) AS payout_odds
  FROM {bet_from} AS b
  INNER JOIN pid ON TRY_CAST(b."player_id" AS BIGINT) = pid.player_id
  WHERE TRY_CAST(b."bet_id" AS DOUBLE) IS NOT NULL
    AND TRY_CAST(b."player_id" AS BIGINT) IS NOT NULL
    AND b."payout_complete_dtm" IS NOT NULL
),
ordered AS (
  SELECT
    s.*,
    LAG(pcd) OVER (
      PARTITION BY player_id ORDER BY pcd
    ) AS lag_pcd,
    COUNT(*) OVER w15 AS fe__bets_cnt__w15m_raw,
    COALESCE(SUM(wager) OVER w15, 0.0) AS fe__wager_sum__w15m_raw,
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
    COALESCE(AVG(ABS(wager)) OVER w7d, CAST(NULL AS DOUBLE)) AS avg_abs_wager_w7d
  FROM src AS s
  WINDOW
    w15 AS (
      PARTITION BY player_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '15 MINUTE' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w1d AS (
      PARTITION BY player_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '1 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w7d AS (
      PARTITION BY player_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '7 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w30d AS (
      PARTITION BY player_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '30 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    ),
    w30_prior AS (
      PARTITION BY player_id ORDER BY pcd
      RANGE BETWEEN INTERVAL '30 DAY' PRECEDING AND INTERVAL '1 MICROSECOND' PRECEDING
    )
)
SELECT
  bet_id,
  CAST(EXTRACT(epoch FROM (pcd - lag_pcd)) AS DOUBLE) AS fe__time_since_last_bet_sec,
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
  END AS fe__payout_odds_z_prior_w30d
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
