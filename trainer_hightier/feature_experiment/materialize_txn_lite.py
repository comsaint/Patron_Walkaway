"""Materialize ``txn__*`` player cashflow features from ``t_casino_txn`` (experiment-only).

Scope v0: BUYIN + CASHOUT only (no CHANGE). Cleaning follows ``doc/FINDINGS.md`` FND-19.
PIT: txn ``start_dtm`` must be strictly before training ``payout_complete_dtm``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Final

import duckdb

from trainer_hightier.config import (
    DEFAULT_T_CASINO_TXN_RAW_PARQUET,
    TXN_LITE_CLEANING_POLICY_ID,
    TXN_LITE_FEATURE_COLUMNS,
    TXN_LITE_INCLUDED_TYPES,
    TXN_LITE_MATERIALIZER_VERSION,
    TXN_LITE_SOURCE_CONTRACT_REF,
    DuckDbRuntimeConfig,
    txn_lite_feature_columns,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

_CLEAN_BASE_CTE: Final[str] = """
ranked AS (
  SELECT
    t.*,
    MAX(CASE WHEN t.__op = 'd' OR t.__deleted = 'True' THEN 1 ELSE 0 END)
      OVER (PARTITION BY t.casino_txn_id) AS has_delete,
    ROW_NUMBER() OVER (
      PARTITION BY t.casino_txn_id
      ORDER BY t.__etl_insert_Dtm DESC NULLS LAST,
               t.updated_dtm DESC NULLS LAST
    ) AS rn
  FROM {raw} AS t
),
clean_base AS (
  SELECT *
  FROM ranked
  WHERE rn = 1 AND has_delete = 0
),
txn_valid AS (
  SELECT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    CAST(start_dtm AS TIMESTAMPTZ) AS start_dtm,
    UPPER(TRIM(CAST(type AS VARCHAR))) AS type,
    UPPER(TRIM(CAST(sub_type AS VARCHAR))) AS sub_type,
    CAST(txn_value AS DOUBLE) AS txn_value
  FROM clean_base
  WHERE start_dtm IS NOT NULL
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
)
""".strip()


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/").replace("'", "''")


def resolve_raw_casino_txn_read_sql(path: Path) -> str:
    """Return a DuckDB ``read_parquet`` source for a file or hive-style part directory."""

    p = Path(path).resolve()
    if p.is_file():
        return f"read_parquet('{_path_esc(p)}')"
    if p.is_dir():
        parts = sorted(p.glob("*.parquet"))
        if not parts:
            raise FileNotFoundError(f"No parquet parts under raw_casino_txn directory: {p}")
        glob_path = _path_esc(p / "*.parquet")
        return f"read_parquet('{glob_path}')"
    raise FileNotFoundError(f"raw_casino_txn path not found: {p}")


def _join_lookback_hours(extra_window_hours: tuple[int, ...]) -> int:
    """Max PIT lookback for txn join (1h default; 24h when ablation windows include 24)."""

    if not extra_window_hours:
        return 1
    return max((1, *extra_window_hours))


def _cash_out_sum_sql(hours: int) -> str:
    """Aggregate CASHOUT sum for one lookback window."""

    suffix = f"w{hours}h"
    return (
        f"CAST(SUM(CASE WHEN type = 'CASHOUT'"
        f" AND start_dtm >= pcd - INTERVAL {hours} HOUR AND start_dtm < pcd"
        f" THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__cash_out_sum__{suffix}"
    )


def _buyin_cash_sum_sql(hours: int) -> str:
    """Aggregate BUYIN/CASH sum for one lookback window."""

    suffix = f"w{hours}h"
    return (
        f"CAST(SUM(CASE WHEN type = 'BUYIN' AND sub_type = 'CASH'"
        f" AND start_dtm >= pcd - INTERVAL {hours} HOUR AND start_dtm < pcd"
        f" THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__buyin_cash_sum__{suffix}"
    )


def _build_materialize_copy_sql(
    *,
    train_esc: str,
    raw_read: str,
    extra_window_hours: tuple[int, ...],
) -> str:
    """Build DuckDB COPY SQL for bet-grain txn_lite features."""

    lookback_h = _join_lookback_hours(extra_window_hours)
    extra_hours = tuple(h for h in extra_window_hours if h > 1)
    extra_agg = []
    for hours in extra_hours:
        extra_agg.append(_cash_out_sum_sql(hours))
        extra_agg.append(_buyin_cash_sum_sql(hours))
    extra_agg_sql = ""
    if extra_agg:
        extra_agg_sql = ",\n  " + ",\n  ".join(extra_agg)

    inner = f"""
WITH {_CLEAN_BASE_CTE.format(raw=raw_read)},
train_rows AS (
  SELECT
    TRY_CAST(bet_id AS DOUBLE) AS bet_id,
    TRY_CAST(player_id AS BIGINT) AS player_id,
    CAST(payout_complete_dtm AS TIMESTAMPTZ) AS pcd
  FROM read_parquet('{train_esc}') AS b
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
    txn.start_dtm
  FROM train_rows AS tr
  LEFT JOIN txn_valid AS txn
    ON tr.player_id = txn.player_id
   AND txn.start_dtm < tr.pcd
   AND txn.start_dtm >= tr.pcd - INTERVAL {lookback_h} HOUR
)
SELECT
  bet_id,
  MAX(CASE
    WHEN type = 'CASHOUT'
     AND start_dtm >= pcd - INTERVAL 15 MINUTE
     AND start_dtm < pcd
    THEN 1 ELSE 0 END) AS txn__has_cash_out__w15m,
  CAST(SUM(CASE
    WHEN type = 'CASHOUT'
     AND start_dtm >= pcd - INTERVAL 1 HOUR AND start_dtm < pcd
    THEN 1 ELSE 0 END) AS DOUBLE) AS txn__cash_out_cnt__w1h,
  CAST(SUM(CASE
    WHEN type = 'CASHOUT'
     AND start_dtm >= pcd - INTERVAL 1 HOUR AND start_dtm < pcd
    THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__cash_out_sum__w1h,
  CAST(SUM(CASE
    WHEN type = 'BUYIN' AND sub_type = 'CASH'
     AND start_dtm >= pcd - INTERVAL 1 HOUR AND start_dtm < pcd
    THEN txn_value ELSE 0 END) AS DOUBLE) AS txn__buyin_cash_sum__w1h,
  MAX(CASE
    WHEN type = 'BUYIN' AND sub_type = 'PRIZE REDEMPTION'
     AND start_dtm >= pcd - INTERVAL 1 HOUR AND start_dtm < pcd
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


def parquet_fingerprint(path: Path) -> str:
    """Return a short SHA-256 hex digest of raw input bytes (file or part directory)."""

    p = Path(path).resolve()
    digest = hashlib.sha256()
    if p.is_file():
        files = [p]
    elif p.is_dir():
        files = sorted(p.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No parquet parts under: {p}")
    else:
        raise FileNotFoundError(f"Parquet source not found for fingerprint: {p}")
    for fp in files:
        digest.update(fp.name.encode("utf-8"))
        with fp.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()[:16]


def materialize_txn_lite_parquet(
    *,
    raw_casino_txn_parquet: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    extra_window_hours: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Build bet-grain ``txn__*`` columns for training rows (player_id × PIT).

    Args:
        raw_casino_txn_parquet: Raw ``t_casino_txn`` partition Parquet.
        training_parquet_for_bet_ids: Step-3 training parquet (needs ``bet_id``,
            ``player_id``, ``payout_complete_dtm``).
        out_parquet: Output Parquet path (``bet_id`` + ``txn__*`` columns).
        duckdb_runtime: DuckDB PRAGMA settings.
        extra_window_hours: Optional longer lookbacks (e.g. ``(4, 24)``) for window
            ablation; adds sum/net columns only (not in registry until promoted).

    Returns:
        Materialization audit dict (row counts, fingerprints, policy id).
    """

    raw = Path(raw_casino_txn_parquet).resolve()
    train = Path(training_parquet_for_bet_ids).resolve()
    out = Path(out_parquet).resolve()
    raw_read = resolve_raw_casino_txn_read_sql(raw)
    if not train.is_file():
        raise FileNotFoundError(f"training_parquet_for_bet_ids missing: {train}")
    out.parent.mkdir(parents=True, exist_ok=True)

    tq, oq = _path_esc(train), _path_esc(out)
    copy_sql = _build_materialize_copy_sql(
        train_esc=tq,
        raw_read=raw_read,
        extra_window_hours=extra_window_hours,
    )
    out_feature_cols = txn_lite_feature_columns(extra_window_hours=extra_window_hours)

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({copy_sql}) TO '{oq}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
        raw_n = int(con.execute(
            f"SELECT COUNT(*) FROM {raw_read}",
        ).fetchone()[0])
        valid_n = int(con.execute(
            f"WITH {_CLEAN_BASE_CTE.format(raw=raw_read)} SELECT COUNT(*) FROM txn_valid",
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
        "materializer_code_version": TXN_LITE_MATERIALIZER_VERSION,
        "types_included": list(TXN_LITE_INCLUDED_TYPES),
        "types_excluded_note": "CHANGE, TD_FILL, TD_CREDIT, TRANSFER, UPDATE_OWNER excluded v0",
        "pit_event_time": "start_dtm",
        "join_grain": "player_id x payout_complete_dtm (strictly before)",
        "raw_input_path": str(raw),
        "raw_read_sql": raw_read,
        "raw_input_fingerprint": parquet_fingerprint(raw),
        "materialized_artifact_fingerprint": parquet_fingerprint(out),
        "raw_row_count": raw_n,
        "valid_txn_row_count": valid_n,
        "training_row_count": train_n,
        "materialized_bet_row_count": out_n,
        "extra_window_hours": list(extra_window_hours),
        "feature_columns": list(out_feature_cols),
    }
    logger.info(
        "[txn_lite] materialized %d bet rows (valid_txn=%d raw=%d) → %s",
        out_n,
        valid_n,
        raw_n,
        out,
    )
    return meta


def default_raw_casino_txn_parquet() -> Path:
    """Default raw partition path for txn_lite v0."""

    return DEFAULT_T_CASINO_TXN_RAW_PARQUET.resolve()


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
                "materializer": "trainer_hightier/feature_experiment/materialize_txn_lite.py",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    dest = root / "materialized_features.parquet"
    if artifact_path != dest.resolve():
        import shutil

        shutil.copy2(artifact_path, dest)
    return mat_path, src_path
