"""Materialize ``txn__*`` player cashflow features from L0 cleaned ``t_casino_txn``.

Scope v0: BUYIN + CASHOUT only (no CHANGE). L1 filters follow ``doc/FINDINGS.md`` FND-19.
Input: ``cleaned__gmwds_t_casino_txn/`` (not raw). Partial partitions are skipped via sidecar.
PIT: both ``txn_event_ts`` and ``txn_available_ts`` must be before training
``payout_complete_dtm``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Final

import duckdb
import pandas as pd

from trainer_hightier.config import (
    DEFAULT_T_CASINO_TXN_CLEANED_ROOT,
    TXN_L0_CLEANING_POLICY_ID,
    TXN_LITE_CLEANING_POLICY_ID,
    TXN_LITE_INCLUDED_TYPES,
    TXN_LITE_MATERIALIZER_VERSION,
    TXN_LITE_SOURCE_CONTRACT_REF,
    DuckDbRuntimeConfig,
    txn_lite_feature_columns,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

_TXN_VALID_BODY: Final[str] = """
  SELECT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    CAST(txn_event_ts AS TIMESTAMPTZ) AS event_ts,
    CAST(txn_available_ts AS TIMESTAMPTZ) AS available_ts,
    UPPER(TRIM(CAST(type AS VARCHAR))) AS type,
    UPPER(TRIM(CAST(sub_type AS VARCHAR))) AS sub_type,
    CAST(txn_value AS DOUBLE) AS txn_value
  FROM {cleaned} AS t
  WHERE txn_event_ts IS NOT NULL
    AND txn_available_ts IS NOT NULL
    AND txn_value IS NOT NULL
    AND CAST(txn_value AS DOUBLE) > 0
    AND action = 'SUBMIT'
    AND status <> 'CANCELED'
    AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND UPPER(TRIM(CAST(type AS VARCHAR))) IN ('BUYIN', 'CASHOUT')
    AND (
      (UPPER(TRIM(CAST(type AS VARCHAR))) = 'CASHOUT' AND status = 'COMPLETED')
      OR (
        UPPER(TRIM(CAST(type AS VARCHAR))) = 'BUYIN'
        AND (
          status = 'COMPLETED'
          OR (
            status = 'SUBMITTED'
            AND buyin_status IN ('SUCCESS', 'PROVISIONAL_SUCCESS')
          )
        )
      )
    )
"""


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/").replace("'", "''")


def discover_cleaned_txn_partitions(
    cleaned_root: Path,
    *,
    exclude_partial: bool = True,
) -> tuple[list[Path], list[str], list[str]]:
    """Return eligible ``cleaned.parquet`` paths and included/excluded partition names."""

    root = Path(cleaned_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"cleaned casino_txn root not found: {root}")
    included_paths: list[Path] = []
    included_names: list[str] = []
    excluded_names: list[str] = []
    for part_dir in sorted(root.glob("partition_*")):
        if not part_dir.is_dir():
            continue
        cleaned_path = part_dir / "cleaned.parquet"
        if not cleaned_path.is_file():
            continue
        if exclude_partial:
            meta_path = part_dir / "source_metadata.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    meta = {}
                if bool(meta.get("is_partial_partition")):
                    excluded_names.append(part_dir.name)
                    continue
        included_paths.append(cleaned_path)
        included_names.append(part_dir.name)
    return included_paths, included_names, excluded_names


def resolve_cleaned_casino_txn_read_sql(
    cleaned_root: Path,
    *,
    exclude_partial: bool = True,
) -> tuple[str, list[str], list[str]]:
    """Build DuckDB ``read_parquet`` source over non-partial L0 cleaned partitions."""

    paths, included_names, excluded_names = discover_cleaned_txn_partitions(
        cleaned_root,
        exclude_partial=exclude_partial,
    )
    if not paths:
        raise FileNotFoundError(
            f"No eligible cleaned partitions under {cleaned_root} "
            f"(excluded_partial={excluded_names})",
        )
    if len(paths) == 1:
        return f"read_parquet('{_path_esc(paths[0])}')", included_names, excluded_names
    entries = ", ".join(f"'{_path_esc(p)}'" for p in paths)
    return f"read_parquet([{entries}], union_by_name=true)", included_names, excluded_names


def _txn_valid_cte(cleaned_read: str) -> str:
    return f"txn_valid AS ({_TXN_VALID_BODY.format(cleaned=cleaned_read)})"


def _join_lookback_hours(extra_window_hours: tuple[int, ...]) -> int:
    """Max PIT lookback for txn join (1h default; 24h when ablation windows include 24)."""

    if not extra_window_hours:
        return 1
    return max((1, *extra_window_hours))


def _cash_out_sum_sql(hours: int, *, cutoff: str = "pcd") -> str:
    """Aggregate CASHOUT sum for one lookback window before ``cutoff``."""

    suffix = f"w{hours}h"
    return (
        f"CAST(SUM(CASE WHEN type = 'CASHOUT'"
        f" AND event_ts >= {cutoff} - INTERVAL {hours} HOUR AND event_ts < {cutoff}"
        f" THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__cash_out_sum__{suffix}"
    )


def _buyin_cash_sum_sql(hours: int, *, cutoff: str = "pcd") -> str:
    """Aggregate BUYIN/CASH sum for one lookback window before ``cutoff``."""

    suffix = f"w{hours}h"
    return (
        f"CAST(SUM(CASE WHEN type = 'BUYIN' AND sub_type = 'CASH'"
        f" AND event_ts >= {cutoff} - INTERVAL {hours} HOUR AND event_ts < {cutoff}"
        f" THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__buyin_cash_sum__{suffix}"
    )


def _build_materialize_copy_sql(
    *,
    train_source: str,
    cleaned_read: str,
    extra_window_hours: tuple[int, ...],
    availability_cutoff_expr: str = "tr.pcd",
    train_rows_extra_select: str = "",
) -> str:
    """Build DuckDB SQL for bet-grain txn_lite features.

    Args:
        train_source: DuckDB table expression for training/scoring bets
            (e.g. ``read_parquet('path')`` or registered ``scoring_bets``).
        availability_cutoff_expr: SQL expression for PIT availability cutoff
            (default ``tr.pcd`` for cleaned offline replay).
        train_rows_extra_select: Optional leading-comma extra SELECT columns
            for ``train_rows`` (e.g. ``avail_cutoff`` for production scoring).
    """

    lookback_h = _join_lookback_hours(extra_window_hours)
    extra_hours = tuple(h for h in extra_window_hours if h > 1)
    extra_agg = []
    for hours in extra_hours:
        extra_agg.append(_cash_out_sum_sql(hours))
        extra_agg.append(_buyin_cash_sum_sql(hours))
    extra_agg_sql = ""
    if extra_agg:
        extra_agg_sql = ",\n  " + ",\n  ".join(extra_agg)
    extra_select = train_rows_extra_select or ""

    inner = f"""
WITH {_txn_valid_cte(cleaned_read)},
train_rows AS (
  SELECT
    TRY_CAST(bet_id AS DOUBLE) AS bet_id,
    TRY_CAST(player_id AS BIGINT) AS player_id,
    CAST(payout_complete_dtm AS TIMESTAMPTZ) AS pcd{extra_select}
  FROM {train_source} AS b
  WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
    AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND b.payout_complete_dtm IS NOT NULL
),
joined AS (
  SELECT
    tr.bet_id,
    tr.pcd,
    txn.type,
    txn.sub_type,
    txn.txn_value,
    txn.event_ts
  FROM train_rows AS tr
  LEFT JOIN txn_valid AS txn
    ON tr.player_id = txn.player_id
   AND txn.event_ts < tr.pcd
   AND txn.available_ts <= {availability_cutoff_expr}
   AND txn.event_ts >= tr.pcd - INTERVAL {lookback_h} HOUR
)
SELECT
  bet_id,
  MAX(CASE
    WHEN type = 'CASHOUT'
     AND event_ts >= pcd - INTERVAL 15 MINUTE
     AND event_ts < pcd
    THEN 1 ELSE 0 END) AS txn__has_cash_out__w15m,
  CAST(SUM(CASE
    WHEN type = 'CASHOUT'
     AND event_ts >= pcd - INTERVAL 1 HOUR AND event_ts < pcd
    THEN 1 ELSE 0 END) AS DOUBLE) AS txn__cash_out_cnt__w1h,
  CAST(SUM(CASE
    WHEN type = 'CASHOUT'
     AND event_ts >= pcd - INTERVAL 1 HOUR AND event_ts < pcd
    THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__cash_out_sum__w1h,
  CAST(SUM(CASE
    WHEN type = 'BUYIN' AND sub_type = 'CASH'
     AND event_ts >= pcd - INTERVAL 1 HOUR AND event_ts < pcd
    THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__buyin_cash_sum__w1h,
  MAX(CASE
    WHEN type = 'BUYIN' AND sub_type = 'PRIZE REDEMPTION'
     AND event_ts >= pcd - INTERVAL 1 HOUR AND event_ts < pcd
    THEN 1 ELSE 0 END) AS txn__buyin_prize_redemption_flag__w1h{extra_agg_sql}
FROM joined
GROUP BY bet_id, pcd
""".strip()

    outer_extra = []
    for hours in extra_hours:
        suffix = f"w{hours}h"
        outer_extra.append(f"txn__cash_out_sum__{suffix}")
        outer_extra.append(f"txn__buyin_cash_sum__{suffix}")
        outer_extra.append(
            f"txn__cash_out_sum__{suffix} - txn__buyin_cash_sum__{suffix}"
            f" AS txn__net_cash_flow__{suffix}",
        )
    outer_extra_sql = ""
    if outer_extra:
        outer_extra_sql = ",\n  " + ",\n  ".join(outer_extra)

    return f"""
SELECT
  bet_id,
  txn__has_cash_out__w15m,
  txn__cash_out_cnt__w1h,
  txn__cash_out_sum__w1h,
  CASE WHEN txn__cash_out_sum__w1h > txn__buyin_cash_sum__w1h THEN 1 ELSE 0 END
    AS txn__net_cash_out_flag__w1h,
  txn__cash_out_sum__w1h - txn__buyin_cash_sum__w1h AS txn__net_cash_flow__w1h,
  txn__buyin_cash_sum__w1h,
  txn__buyin_prize_redemption_flag__w1h{outer_extra_sql}
FROM ({inner}) AS agg
""".strip()


def _build_player_game_txn_copy_sql(
    *,
    train_source: str,
    cleaned_read: str,
    extra_window_hours: tuple[int, ...],
) -> str:
    """Build DuckDB SQL for player-game txn_lite at ``player_game_ready_ts`` PIT."""

    lookback_h = _join_lookback_hours(extra_window_hours)
    extra_hours = tuple(h for h in extra_window_hours if h > 1)
    extra_agg = []
    for hours in extra_hours:
        extra_agg.append(_cash_out_sum_sql(hours, cutoff="pit_ts"))
        extra_agg.append(_buyin_cash_sum_sql(hours, cutoff="pit_ts"))
    extra_agg_sql = ""
    if extra_agg:
        extra_agg_sql = ",\n  " + ",\n  ".join(extra_agg)

    inner = f"""
WITH {_txn_valid_cte(cleaned_read)},
train_rows AS (
  SELECT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    TRY_CAST(game_id AS BIGINT) AS game_id,
    CAST(player_game_ready_ts AS TIMESTAMPTZ) AS pit_ts
  FROM {train_source} AS b
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND TRY_CAST(game_id AS BIGINT) IS NOT NULL
    AND b.player_game_ready_ts IS NOT NULL
),
joined AS (
  SELECT
    tr.player_id,
    tr.game_id,
    tr.pit_ts,
    txn.type,
    txn.sub_type,
    txn.txn_value,
    txn.event_ts
  FROM train_rows AS tr
  LEFT JOIN txn_valid AS txn
    ON tr.player_id = txn.player_id
   AND txn.event_ts < tr.pit_ts
   AND txn.available_ts <= tr.pit_ts
   AND txn.event_ts >= tr.pit_ts - INTERVAL {lookback_h} HOUR
)
SELECT
  player_id,
  game_id,
  MAX(CASE
    WHEN type = 'CASHOUT'
     AND event_ts >= pit_ts - INTERVAL 15 MINUTE
     AND event_ts < pit_ts
    THEN 1 ELSE 0 END) AS txn__has_cash_out__w15m,
  CAST(SUM(CASE
    WHEN type = 'CASHOUT'
     AND event_ts >= pit_ts - INTERVAL 1 HOUR AND event_ts < pit_ts
    THEN 1 ELSE 0 END) AS DOUBLE) AS txn__cash_out_cnt__w1h,
  CAST(SUM(CASE
    WHEN type = 'CASHOUT'
     AND event_ts >= pit_ts - INTERVAL 1 HOUR AND event_ts < pit_ts
    THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__cash_out_sum__w1h,
  CAST(SUM(CASE
    WHEN type = 'BUYIN' AND sub_type = 'CASH'
     AND event_ts >= pit_ts - INTERVAL 1 HOUR AND event_ts < pit_ts
    THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__buyin_cash_sum__w1h,
  MAX(CASE
    WHEN type = 'BUYIN' AND sub_type = 'PRIZE REDEMPTION'
     AND event_ts >= pit_ts - INTERVAL 1 HOUR AND event_ts < pit_ts
    THEN 1 ELSE 0 END) AS txn__buyin_prize_redemption_flag__w1h{extra_agg_sql}
FROM joined
GROUP BY player_id, game_id
""".strip()

    outer_extra = []
    for hours in extra_hours:
        suffix = f"w{hours}h"
        outer_extra.append(f"txn__cash_out_sum__{suffix}")
        outer_extra.append(f"txn__buyin_cash_sum__{suffix}")
        outer_extra.append(
            f"txn__cash_out_sum__{suffix} - txn__buyin_cash_sum__{suffix}"
            f" AS txn__net_cash_flow__{suffix}",
        )
    outer_extra_sql = ""
    if outer_extra:
        outer_extra_sql = ",\n  " + ",\n  ".join(outer_extra)

    return f"""
SELECT
  player_id,
  game_id,
  txn__has_cash_out__w15m,
  txn__cash_out_cnt__w1h,
  txn__cash_out_sum__w1h,
  CASE WHEN txn__cash_out_sum__w1h > txn__buyin_cash_sum__w1h THEN 1 ELSE 0 END
    AS txn__net_cash_out_flag__w1h,
  txn__cash_out_sum__w1h - txn__buyin_cash_sum__w1h AS txn__net_cash_flow__w1h,
  txn__buyin_cash_sum__w1h,
  txn__buyin_prize_redemption_flag__w1h{outer_extra_sql}
FROM ({inner}) AS agg
""".strip()


def enrich_player_game_splits_with_txn_pg(
    pg_splits_dir: Path,
    out_dir: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    cleaned_casino_txn_root: Path | None = None,
    extra_window_hours: tuple[int, ...] = (),
    exclude_partial_partitions: bool = True,
) -> dict[str, Any]:
    """Join player-game txn_lite features at ``player_game_ready_ts`` onto PG splits."""

    sd = Path(pg_splits_dir).resolve()
    od = Path(out_dir).resolve()
    od.mkdir(parents=True, exist_ok=True)
    cleaned_root = Path(cleaned_casino_txn_root or default_cleaned_casino_txn_root()).resolve()
    cleaned_read, included_partitions, excluded_partitions = resolve_cleaned_casino_txn_read_sql(
        cleaned_root,
        exclude_partial=exclude_partial_partitions,
    )
    txn_cols = txn_lite_feature_columns(extra_window_hours=extra_window_hours)
    coalesce_txn = ",\n    ".join(
        f"coalesce(txn.{col}, 0) AS {col}" for col in txn_cols
    )
    split_stats: dict[str, dict[str, int]] = {}
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        for split in ("train", "val", "test"):
            src = sd / f"{split}.parquet"
            dst = od / f"{split}.parquet"
            if not src.is_file():
                raise FileNotFoundError(f"player_game txn_pg enrich requires {src}")
            src_sql = _path_esc(src)
            dst_sql = _path_esc(dst)
            txn_sql = _build_player_game_txn_copy_sql(
                train_source=f"read_parquet('{src_sql}')",
                cleaned_read=cleaned_read,
                extra_window_hours=extra_window_hours,
            )
            con.execute(
                f"""
                COPY (
                  SELECT
                    pg.*,
                    {coalesce_txn}
                  FROM read_parquet('{src_sql}') AS pg
                  LEFT JOIN ({txn_sql}) AS txn
                    USING (player_id, game_id)
                ) TO '{dst_sql}' (FORMAT PARQUET, COMPRESSION SNAPPY)
                """,
            )
            split_stats[split] = {
                "input_player_games": int(
                    con.execute(f"SELECT count(*) FROM read_parquet('{src_sql}')").fetchone()[0],
                ),
                "output_player_games": int(
                    con.execute(f"SELECT count(*) FROM read_parquet('{dst_sql}')").fetchone()[0],
                ),
            }
    finally:
        con.close()

    meta: dict[str, Any] = {
        "join_grain": "player_id x game_id x player_game_ready_ts",
        "pit_cutoff_column": "player_game_ready_ts",
        "pit_event_time": "txn_event_ts",
        "pit_available_time": "txn_available_ts",
        "cleaned_input_root": str(cleaned_root),
        "included_partitions": included_partitions,
        "excluded_partial_partitions": excluded_partitions,
        "feature_columns": list(txn_cols),
        "split_stats": split_stats,
    }
    logger.info(
        "[txn_pg] enriched player-game splits %s → %s stats=%s",
        sd,
        od,
        split_stats,
    )
    return meta


def parquet_fingerprint(path: Path) -> str:
    """Return a short SHA-256 hex digest of one parquet file."""

    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Parquet source not found for fingerprint: {p}")
    digest = hashlib.sha256()
    digest.update(p.name.encode("utf-8"))
    with p.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def cleaned_partitions_fingerprint(paths: list[Path]) -> str:
    """Return a short digest over eligible cleaned partition parquet paths."""

    digest = hashlib.sha256()
    for fp in sorted(Path(p).resolve() for p in paths):
        digest.update(str(fp).encode("utf-8"))
        digest.update(parquet_fingerprint(fp).encode("utf-8"))
    return digest.hexdigest()[:16]


def materialize_txn_lite_parquet(
    *,
    cleaned_casino_txn_root: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    extra_window_hours: tuple[int, ...] = (),
    exclude_partial_partitions: bool = True,
) -> dict[str, Any]:
    """Build bet-grain ``txn__*`` columns for training rows (player_id × PIT).

    Args:
        cleaned_casino_txn_root: L0 ``cleaned__gmwds_t_casino_txn/`` root directory.
        training_parquet_for_bet_ids: Step-3 training parquet (needs ``bet_id``,
            ``player_id``, ``payout_complete_dtm``).
        out_parquet: Output Parquet path (``bet_id`` + ``txn__*`` columns).
        duckdb_runtime: DuckDB PRAGMA settings.
        extra_window_hours: Optional longer lookbacks (e.g. ``(4, 24)``) for window
            ablation; adds sum/net columns only (not in registry until promoted).
        exclude_partial_partitions: Skip partitions with ``is_partial_partition`` sidecar.

    Returns:
        Materialization audit dict (row counts, fingerprints, policy id).
    """

    cleaned_root = Path(cleaned_casino_txn_root).resolve()
    train = Path(training_parquet_for_bet_ids).resolve()
    out = Path(out_parquet).resolve()
    cleaned_read, included_partitions, excluded_partitions = resolve_cleaned_casino_txn_read_sql(
        cleaned_root,
        exclude_partial=exclude_partial_partitions,
    )
    included_paths, _, _ = discover_cleaned_txn_partitions(
        cleaned_root,
        exclude_partial=exclude_partial_partitions,
    )
    if not train.is_file():
        raise FileNotFoundError(f"training_parquet_for_bet_ids missing: {train}")
    out.parent.mkdir(parents=True, exist_ok=True)

    tq, oq = _path_esc(train), _path_esc(out)
    train_source = f"read_parquet('{tq}')"
    copy_sql = _build_materialize_copy_sql(
        train_source=train_source,
        cleaned_read=cleaned_read,
        extra_window_hours=extra_window_hours,
    )
    out_feature_cols = txn_lite_feature_columns(extra_window_hours=extra_window_hours)
    valid_cte = _txn_valid_cte(cleaned_read)

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({copy_sql}) TO '{oq}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
        cleaned_n = int(con.execute(f"SELECT COUNT(*) FROM {cleaned_read}").fetchone()[0])
        valid_n = int(con.execute(
            f"WITH {valid_cte} SELECT COUNT(*) FROM txn_valid",
        ).fetchone()[0])
        train_n = int(con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{tq}')",
        ).fetchone()[0])
        out_n = int(con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{oq}')",
        ).fetchone()[0])
    finally:
        con.close()

    meta: dict[str, Any] = {
        "source_name": "t_casino_txn",
        "source_contract_ref": TXN_LITE_SOURCE_CONTRACT_REF,
        "cleaning_policy_id": TXN_LITE_CLEANING_POLICY_ID,
        "l0_cleaning_policy_id": TXN_L0_CLEANING_POLICY_ID,
        "materializer_code_version": TXN_LITE_MATERIALIZER_VERSION,
        "input_layer": "l0_cleaned",
        "not_model_eligible": True,
        "types_included": list(TXN_LITE_INCLUDED_TYPES),
        "types_excluded_note": "CHANGE, TD_FILL, TD_CREDIT, TRANSFER, UPDATE_OWNER excluded v0",
        "pit_event_time": "txn_event_ts",
        "pit_available_time": "txn_available_ts",
        "join_grain": "player_id x payout_complete_dtm (event before, available by cutoff)",
        "cleaned_input_root": str(cleaned_root),
        "cleaned_read_sql": cleaned_read,
        "included_partitions": included_partitions,
        "excluded_partial_partitions": excluded_partitions,
        "cleaned_input_fingerprint": cleaned_partitions_fingerprint(included_paths),
        "materialized_artifact_fingerprint": parquet_fingerprint(out),
        "cleaned_row_count": cleaned_n,
        "valid_txn_row_count": valid_n,
        "training_row_count": train_n,
        "materialized_bet_row_count": out_n,
        "extra_window_hours": list(extra_window_hours),
        "feature_columns": list(out_feature_cols),
    }
    logger.info(
        "[txn_lite] materialized %d bet rows (valid_txn=%d cleaned=%d partitions=%d) → %s",
        out_n,
        valid_n,
        cleaned_n,
        len(included_partitions),
        out,
    )
    return meta


def compute_txn_lite_features_for_bets(
    bets: pd.DataFrame,
    *,
    cleaned_casino_txn_root: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    extra_window_hours: tuple[int, ...] = (),
) -> pd.DataFrame:
    """Compute bet-grain ``txn__*`` columns from L0 cleaned parquet (offline / training)."""

    out_feature_cols = txn_lite_feature_columns(extra_window_hours=extra_window_hours)
    if bets.empty:
        return pd.DataFrame(columns=["bet_id", *out_feature_cols])
    required = frozenset({"bet_id", "player_id", "payout_complete_dtm"})
    missing = required - frozenset(bets.columns)
    if missing:
        raise ValueError(
            f"compute_txn_lite_features_for_bets missing columns {sorted(missing)}; "
            f"got {list(bets.columns)!r}",
        )
    cleaned_root = Path(cleaned_casino_txn_root).resolve()
    cleaned_read, _, _ = resolve_cleaned_casino_txn_read_sql(
        cleaned_root,
        exclude_partial=True,
    )
    return compute_txn_lite_features_from_txn_source(
        bets,
        txn_source_read=cleaned_read,
        duckdb_runtime=duckdb_runtime,
        extra_window_hours=extra_window_hours,
        availability_cutoff_expr="tr.pcd",
    )


def compute_txn_lite_features_from_txn_source(
    bets: pd.DataFrame,
    *,
    txn_source_read: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    extra_window_hours: tuple[int, ...] = (),
    availability_cutoff_expr: str = "tr.pcd",
    train_rows_extra_select: str = "",
    scoring_bets_frame: pd.DataFrame | None = None,
    txn_frame: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compute bet-grain ``txn__*`` from a DuckDB txn source expression or in-memory frame."""

    out_feature_cols = txn_lite_feature_columns(extra_window_hours=extra_window_hours)
    if bets.empty:
        return pd.DataFrame(columns=["bet_id", *out_feature_cols])
    required = frozenset({"bet_id", "player_id", "payout_complete_dtm"})
    missing = required - frozenset(bets.columns)
    if missing:
        raise ValueError(
            f"compute_txn_lite_features_from_txn_source missing columns {sorted(missing)}; "
            f"got {list(bets.columns)!r}",
        )
    source_read = txn_source_read
    copy_sql = _build_materialize_copy_sql(
        train_source="scoring_bets",
        cleaned_read=source_read,
        extra_window_hours=extra_window_hours,
        availability_cutoff_expr=availability_cutoff_expr,
        train_rows_extra_select=train_rows_extra_select,
    )
    work = scoring_bets_frame if scoring_bets_frame is not None else bets[list(required)].copy()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.register("scoring_bets", work)
        if txn_frame is not None:
            con.register("fetched_txn", txn_frame)
            source_read = "fetched_txn"
            copy_sql = _build_materialize_copy_sql(
                train_source="scoring_bets",
                cleaned_read=source_read,
                extra_window_hours=extra_window_hours,
                availability_cutoff_expr=availability_cutoff_expr,
                train_rows_extra_select=train_rows_extra_select,
            )
        out = con.execute(copy_sql).df()
    finally:
        con.close()
    return out


def default_cleaned_casino_txn_root() -> Path:
    """Default L0 cleaned root for txn_lite v1."""

    return DEFAULT_T_CASINO_TXN_CLEANED_ROOT.resolve()


def resolved_cleaned_casino_txn_root(
    cfg: "HightierServingConfig | None" = None,
) -> Path:
    """Return deploy-configured or package-default L0 cleaned ``t_casino_txn`` root."""

    from trainer_hightier.config import HightierServingConfig, default_hightier_serving_config

    serving: HightierServingConfig = cfg or default_hightier_serving_config()
    if serving.cleaned_casino_txn_root is not None:
        return Path(serving.cleaned_casino_txn_root).resolve()
    return default_cleaned_casino_txn_root()


def write_txn_lite_sidecars(
    *,
    run_dir: Path,
    materialization_meta: dict[str, Any],
    out_parquet: Path,
) -> tuple[Path, Path]:
    """Write ``materialization_report.json`` and ``source_metadata.json`` under run_dir."""

    root = Path(run_dir).resolve() / "external_sources" / "t_casino_txn"
    root.mkdir(parents=True, exist_ok=True)
    mat_path = root / "materialization_report.json"
    src_path = root / "source_metadata.json"
    artifact_path = Path(out_parquet).resolve()
    payload = {**materialization_meta, "materialized_features_path": str(artifact_path)}
    mat_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    src_path.write_text(
        json.dumps(
            {
                "source_name": "t_casino_txn",
                "source_contract_ref": TXN_LITE_SOURCE_CONTRACT_REF,
                "cleaning_policy_id": TXN_LITE_CLEANING_POLICY_ID,
                "l0_cleaning_policy_id": TXN_L0_CLEANING_POLICY_ID,
                "materializer": "trainer_hightier/feature_experiment/materialize_txn_lite.py",
                "input_layer": "l0_cleaned",
                "not_model_eligible": True,
                "included_partitions": materialization_meta.get("included_partitions", []),
                "excluded_partial_partitions": materialization_meta.get(
                    "excluded_partial_partitions",
                    [],
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    dest = root / "materialized_features.parquet"
    if artifact_path != dest.resolve():
        shutil.copy2(artifact_path, dest)
    return mat_path, src_path
