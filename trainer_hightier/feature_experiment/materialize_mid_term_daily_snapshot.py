"""Materialize mid-term ``fe__*`` as canonical daily ``gaming_day`` snapshots."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig, MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_dataset_has_any_parquet,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

MID_TERM_SNAPSHOT_OUTPUT_COLUMNS: tuple[str, ...] = (
    "canonical_id",
    "anchor_gaming_day",
    "fe__bets_cnt__w1d",
    "fe__wager_sum__w1d",
    "fe__bets_cnt__w7d",
    "fe__wager_sum__w7d",
    "fe__bets_cnt__w30d",
    "fe__wager_sum__w30d",
    "fe__prior_wager_mean_w30d",
    "fe__prior_wager_std_w30d",
    "fe__prior_odds_mean_w30d",
    "fe__prior_odds_std_w30d",
    "fe__std_wager_w7d",
    "fe__avg_abs_wager_w7d",
    "fe__interarrival_avg_w7d",
    "fe__interarrival_std_w7d",
    "fe__max_pcd_w7d",
    "fe__min_pcd_w7d",
)


def _path_esc(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "''")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _daily_snapshot_sql(
    *,
    bet_from: str,
    cmap_esc: str,
    lookback_days: int,
    anchor_start: date | None,
    anchor_end: date | None,
) -> str:
    """Build SQL for canonical daily mid-term snapshots."""

    lb = int(max(1, lookback_days))
    span6 = 6
    span29 = 29
    anchor_filter = ""
    if anchor_start is not None:
        anchor_filter += f" AND anchor_gaming_day >= DATE '{anchor_start.isoformat()}'"
    if anchor_end is not None:
        anchor_filter += f" AND anchor_gaming_day <= DATE '{anchor_end.isoformat()}'"

    return f"""
WITH cmap AS (
  SELECT DISTINCT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{cmap_esc}')
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
),
bets AS (
  SELECT
    COALESCE(c.canonical_id, CAST(b."player_id" AS VARCHAR)) AS canonical_id,
    CAST(b."gaming_day" AS DATE) AS gday,
    CAST(b."payout_complete_dtm" AS TIMESTAMPTZ) AS pcd,
    TRY_CAST(b."wager" AS DOUBLE) AS wager,
    TRY_CAST(b."payout_odds" AS DOUBLE) AS payout_odds
  FROM {bet_from} AS b
  LEFT JOIN cmap AS c ON TRY_CAST(b."player_id" AS BIGINT) = c.player_id
  WHERE TRY_CAST(b."player_id" AS BIGINT) IS NOT NULL
    AND b."gaming_day" IS NOT NULL
    AND b."payout_complete_dtm" IS NOT NULL
),
ordered AS (
  SELECT
    *,
    LAG(pcd) OVER (PARTITION BY canonical_id ORDER BY pcd) AS lag_pcd
  FROM bets
),
with_iv AS (
  SELECT
    *,
    EXTRACT(epoch FROM (pcd - lag_pcd)) AS interarrival_sec
  FROM ordered
),
anchor_days AS (
  SELECT DISTINCT canonical_id, gday AS anchor_gaming_day
  FROM bets
),
snap_base AS (
  SELECT
    a.canonical_id,
    a.anchor_gaming_day,
    COUNT(*) FILTER (WHERE b.gday = a.anchor_gaming_day) AS fe__bets_cnt__w1d,
    COALESCE(SUM(b.wager) FILTER (WHERE b.gday = a.anchor_gaming_day), 0.0) AS fe__wager_sum__w1d,
    COUNT(*) FILTER (
      WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
    ) AS fe__bets_cnt__w7d,
    COALESCE(
      SUM(b.wager) FILTER (
        WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
      ),
      0.0
    ) AS fe__wager_sum__w7d,
    COUNT(*) FILTER (
      WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span29}' DAY AND a.anchor_gaming_day
    ) AS fe__bets_cnt__w30d,
    COALESCE(
      SUM(b.wager) FILTER (
        WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span29}' DAY AND a.anchor_gaming_day
      ),
      0.0
    ) AS fe__wager_sum__w30d,
    AVG(b.wager) FILTER (
      WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span29}' DAY AND a.anchor_gaming_day - INTERVAL '1' DAY
    ) AS fe__prior_wager_mean_w30d,
    STDDEV_POP(b.wager) FILTER (
      WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span29}' DAY AND a.anchor_gaming_day - INTERVAL '1' DAY
    ) AS fe__prior_wager_std_w30d,
    AVG(b.payout_odds) FILTER (
      WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span29}' DAY AND a.anchor_gaming_day - INTERVAL '1' DAY
    ) AS fe__prior_odds_mean_w30d,
    STDDEV_POP(b.payout_odds) FILTER (
      WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span29}' DAY AND a.anchor_gaming_day - INTERVAL '1' DAY
    ) AS fe__prior_odds_std_w30d,
    STDDEV_POP(b.wager) FILTER (
      WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
    ) AS fe__std_wager_w7d,
    AVG(ABS(b.wager)) FILTER (
      WHERE b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
    ) AS fe__avg_abs_wager_w7d,
    AVG(wi.interarrival_sec) FILTER (
      WHERE wi.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
        AND wi.interarrival_sec IS NOT NULL
    ) AS fe__interarrival_avg_w7d,
    STDDEV_POP(wi.interarrival_sec) FILTER (
      WHERE wi.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
        AND wi.interarrival_sec IS NOT NULL
    ) AS fe__interarrival_std_w7d,
    MAX(wi.pcd) FILTER (
      WHERE wi.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
    ) AS fe__max_pcd_w7d,
    MIN(wi.pcd) FILTER (
      WHERE wi.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
    ) AS fe__min_pcd_w7d
  FROM anchor_days AS a
  LEFT JOIN bets AS b
    ON a.canonical_id = b.canonical_id
   AND b.gday BETWEEN a.anchor_gaming_day - INTERVAL '{lb - 1}' DAY AND a.anchor_gaming_day
  LEFT JOIN with_iv AS wi
    ON a.canonical_id = wi.canonical_id
   AND wi.gday BETWEEN a.anchor_gaming_day - INTERVAL '{span6}' DAY AND a.anchor_gaming_day
  GROUP BY a.canonical_id, a.anchor_gaming_day
)
SELECT *
FROM snap_base
WHERE 1=1{anchor_filter}
""".strip()


def materialize_mid_term_daily_snapshot(
    *,
    cleaned_bet_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    lookback_days: int | None = None,
    anchor_gaming_day_start: date | None = None,
    anchor_gaming_day_end: date | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Write canonical mid-term daily snapshots keyed by ``anchor_gaming_day``."""

    src_root = Path(cleaned_bet_parquet).resolve()
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmap_path = (
        Path(canonical_mapping_parquet).resolve()
        if canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    if not cleaned_bet_dataset_has_any_parquet(src_root):
        raise FileNotFoundError(f"No cleaned bet parquet under {src_root}")
    if not cmap_path.is_file():
        raise FileNotFoundError(f"canonical mapping parquet missing: {cmap_path}")

    lb = int(lookback_days if lookback_days is not None else MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS)
    bet_from = resolved_cleaned_bet_read_parquet_sql(src_root)
    sql = _daily_snapshot_sql(
        bet_from=bet_from,
        cmap_esc=_path_esc(cmap_path),
        lookback_days=lb,
        anchor_start=anchor_gaming_day_start,
        anchor_end=anchor_gaming_day_end,
    )
    dst_esc = _path_esc(dst)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({sql}) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()

    pf = pq.ParquetFile(dst)
    nrows = int(pf.metadata.num_rows) if pf.metadata is not None else 0
    schema_names = set(pf.schema_arrow.names)
    miss = [c for c in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS if c not in schema_names]
    if miss:
        raise ValueError(f"mid-term snapshot missing columns {miss}; got {sorted(schema_names)}")

    meta: dict[str, Any] = {
        "artifact_kind": "mid_term_daily_gaming_day_snapshot",
        "row_count": nrows,
        "lookback_days": lb,
        "sha256": _sha256_file(dst),
        "path": str(dst),
    }
    sidecar = dst.parent / f"{dst.stem}.meta.json"
    sidecar.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("[mid_term_daily_snapshot] wrote %s rows=%d", dst, nrows)
    return dst, meta
