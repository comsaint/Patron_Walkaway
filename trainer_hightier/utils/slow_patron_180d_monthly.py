"""Materialize slow-varying patron metrics (180d lookback, monthly snapshot) for Feast.

Computes, per ``canonical_id``, monthly snapshot rows keyed by that patron's
**first ``gaming_day`` in each calendar month** (HK local date from cleaned
session). Each snapshot aggregates cleaned **sessions** in
``[anchor - (lookback_days-1), anchor]`` (inclusive on both ends by calendar
``gaming_day``): ``SUM(theo_win)``, ``COUNT(DISTINCT gaming_day)``, and ADT =
``total_theo / distinct days`` (NULL if no distinct days).

Each **cleaned bet** receives the snapshot with the **latest**
``anchor_gaming_day`` such that ``anchor_gaming_day <= bet_gaming_day``, where
``bet_gaming_day = COALESCE(bet.gaming_day, DATE(payout_complete_dtm))``.

All heavy work stays in DuckDB (no pandas full-frame load).
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def default_cleaned_session_parquet_path(*, repo_root: Path | None = None) -> Path:
    """Default cleaned session Parquet."""
    base = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
    return (base / "trainer_hightier" / "artifacts" / "cleaned" / "cleaned__gmwds_t_session.parquet").resolve()


def default_cleaned_bet_parquet_path(*, repo_root: Path | None = None) -> Path:
    """Default cleaned bet Parquet."""
    base = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
    return (base / "trainer_hightier" / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet.parquet").resolve()


def default_slow_patron_180d_monthly_parquet_path(*, repo_root: Path | None = None) -> Path:
    """Default Feast materialization path."""
    base = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
    return (base / "trainer_hightier" / "artifacts" / "feast" / "slow_patron_180d_monthly.parquet").resolve()


def _materialize_sql(
    *,
    sess_esc: str,
    map_esc: str,
    bet_esc: str,
    lookback_days: int,
) -> str:
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1, got {lookback_days!r}")
    span = int(lookback_days) - 1
    return f"""
WITH sessions_mapped AS (
  SELECT
    TRIM(CAST(m.canonical_id AS VARCHAR)) AS canonical_id,
    CAST(s.gaming_day AS DATE) AS gaming_day_d,
    COALESCE(TRY_CAST(s.theo_win AS DOUBLE), 0.0) AS theo_win
  FROM read_parquet('{sess_esc}') s
  INNER JOIN (
    SELECT
      TRY_CAST(player_id AS BIGINT) AS player_id,
      ANY_VALUE(TRIM(CAST(canonical_id AS VARCHAR))) AS canonical_id
    FROM read_parquet('{map_esc}')
    WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
    GROUP BY player_id
  ) m ON TRY_CAST(s.player_id AS BIGINT) = m.player_id
  WHERE s.gaming_day IS NOT NULL
    AND CAST(s.gaming_day AS DATE) IS NOT NULL
    AND TRIM(CAST(m.canonical_id AS VARCHAR)) <> ''
),
monthly_anchors AS (
  SELECT
    canonical_id,
    DATE_TRUNC('month', gaming_day_d)::DATE AS cal_month,
    MIN(gaming_day_d) AS anchor_gaming_day
  FROM sessions_mapped
  GROUP BY canonical_id, DATE_TRUNC('month', gaming_day_d)::DATE
),
snapshots AS (
  SELECT
    ma.canonical_id,
    ma.anchor_gaming_day,
    COALESCE(SUM(s.theo_win), 0.0) AS patron__theo_win_sum__w180d_m1snap,
    CAST(COUNT(DISTINCT s.gaming_day_d) AS BIGINT) AS patron__gaming_days_cnt__w180d_m1snap,
    CASE
      WHEN COUNT(DISTINCT s.gaming_day_d) > 0 THEN
        CAST(COALESCE(SUM(s.theo_win), 0.0) AS DOUBLE)
          / CAST(COUNT(DISTINCT s.gaming_day_d) AS DOUBLE)
      ELSE CAST(NULL AS DOUBLE)
    END AS patron__adt__w180d_m1snap
  FROM monthly_anchors ma
  INNER JOIN sessions_mapped s
    ON s.canonical_id = ma.canonical_id
   AND s.gaming_day_d <= ma.anchor_gaming_day
   AND s.gaming_day_d >= ma.anchor_gaming_day - INTERVAL '{span}' DAY
  GROUP BY ma.canonical_id, ma.anchor_gaming_day
),
map_dedup AS (
  SELECT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    ANY_VALUE(TRIM(CAST(canonical_id AS VARCHAR))) AS canonical_id
  FROM read_parquet('{map_esc}')
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
  GROUP BY player_id
),
bet_base AS (
  SELECT
    TRY_CAST(b.bet_id AS DOUBLE) AS bet_id,
    CAST(b.prediction_visible_ts_cf AS TIMESTAMPTZ) AS prediction_visible_ts_cf,
    CAST(b.__etl_insert_Dtm_synthetic AS TIMESTAMPTZ) AS __etl_insert_Dtm_synthetic,
    TRIM(CAST(m.canonical_id AS VARCHAR)) AS canonical_id,
    COALESCE(
      CAST(b.gaming_day AS DATE),
      CAST(CAST(b.payout_complete_dtm AS TIMESTAMPTZ) AS DATE)
    ) AS bet_gaming_day
  FROM read_parquet('{bet_esc}') b
  INNER JOIN map_dedup m ON TRY_CAST(b.player_id AS BIGINT) = m.player_id
  WHERE TRY_CAST(b.bet_id AS DOUBLE) IS NOT NULL
    AND b.prediction_visible_ts_cf IS NOT NULL
    AND b.__etl_insert_Dtm_synthetic IS NOT NULL
    AND TRIM(CAST(m.canonical_id AS VARCHAR)) <> ''
    AND COALESCE(
      CAST(b.gaming_day AS DATE),
      CAST(CAST(b.payout_complete_dtm AS TIMESTAMPTZ) AS DATE)
    ) IS NOT NULL
)
SELECT
  bb.bet_id,
  bb.prediction_visible_ts_cf,
  bb.__etl_insert_Dtm_synthetic,
  lst.patron__theo_win_sum__w180d_m1snap,
  lst.patron__gaming_days_cnt__w180d_m1snap,
  lst.patron__adt__w180d_m1snap
FROM bet_base bb
LEFT JOIN LATERAL (
  SELECT
    sn.patron__theo_win_sum__w180d_m1snap,
    sn.patron__gaming_days_cnt__w180d_m1snap,
    sn.patron__adt__w180d_m1snap
  FROM snapshots sn
  WHERE sn.canonical_id = bb.canonical_id
    AND sn.anchor_gaming_day <= bb.bet_gaming_day
  ORDER BY sn.anchor_gaming_day DESC
  LIMIT 1
) lst ON TRUE
""".strip()


def materialize_slow_patron_180d_monthly(
    cleaned_session_parquet: Path | None = None,
    canonical_mapping_parquet: Path | None = None,
    cleaned_bet_parquet: Path | None = None,
    out_parquet: Path | None = None,
    *,
    lookback_days: int = 180,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> Path:
    """Write bet-grain Parquet with three slow patron features (see module docstring).

    Args:
        cleaned_session_parquet: Cleaned ``t_session`` with ``player_id``, ``gaming_day``, ``theo_win``.
        canonical_mapping_parquet: ``player_id`` / ``canonical_id``.
        cleaned_bet_parquet: Cleaned bet with Feast timestamps + ``gaming_day`` or ``payout_complete_dtm``.
        out_parquet: Output path; default ``artifacts/feast/slow_patron_180d_monthly.parquet``.
        lookback_days: Inclusive span of calendar days ending on ``anchor_gaming_day`` (default 180).
        duckdb_runtime: Optional DuckDB PRAGMAs.

    Returns:
        Resolved output path.

    Raises:
        FileNotFoundError: Missing inputs.
        ValueError: Schema mismatch or invalid ``lookback_days``.
    """
    src_sess = Path(cleaned_session_parquet or default_cleaned_session_parquet_path()).resolve()
    src_map = Path(canonical_mapping_parquet or default_canonical_mapping_parquet_path()).resolve()
    src_bet = Path(cleaned_bet_parquet or default_cleaned_bet_parquet_path()).resolve()
    dst = Path(out_parquet or default_slow_patron_180d_monthly_parquet_path()).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    for p, name in (
        (src_sess, "cleaned session"),
        (src_map, "canonical mapping"),
        (src_bet, "cleaned bet"),
    ):
        if not p.is_file():
            raise FileNotFoundError(f"{name} parquet not found: {p}")

    sess_cols = set(pq.read_schema(src_sess).names)
    need_sess = frozenset({"player_id", "gaming_day", "theo_win"})
    miss_s = sorted(need_sess - sess_cols)
    if miss_s:
        raise ValueError(f"cleaned session missing columns {miss_s}; got {sorted(sess_cols)}")

    bet_cols = set(pq.read_schema(src_bet).names)
    need_bet = frozenset(
        {"bet_id", "player_id", "payout_complete_dtm", "prediction_visible_ts_cf", "__etl_insert_Dtm_synthetic"}
    )
    miss_b = sorted(need_bet - bet_cols)
    if miss_b:
        raise ValueError(f"cleaned bet missing columns {miss_b}; got {sorted(bet_cols)}")
    if "gaming_day" not in bet_cols:
        logger.warning(
            "slow_patron_180d_monthly: cleaned bet has no gaming_day; bet_gaming_day falls back to "
            "DATE(payout_complete_dtm) only."
        )

    map_cols = set(pq.read_schema(src_map).names)
    miss_m = sorted({"player_id", "canonical_id"} - map_cols)
    if miss_m:
        raise ValueError(f"canonical mapping missing columns {miss_m}; got {sorted(map_cols)}")

    sess_esc = _path_posix(src_sess).replace("'", "''")
    map_esc = _path_posix(src_map).replace("'", "''")
    bet_esc = _path_posix(src_bet).replace("'", "''")
    inner = _materialize_sql(sess_esc=sess_esc, map_esc=map_esc, bet_esc=bet_esc, lookback_days=lookback_days)
    dst_esc = _path_posix(dst).replace("'", "''")

    con = duckdb.connect(database=":memory:")
    try:
        if duckdb_runtime is not None:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({inner}) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()

    n_out = 0
    try:
        pf = pq.ParquetFile(dst)
        n_out = int(pf.metadata.num_rows) if pf.metadata else 0
    except Exception:
        pass
    logger.info(
        "slow_patron_180d_monthly: rows=%d lookback_days=%d written %s",
        n_out,
        lookback_days,
        dst,
    )
    return dst
