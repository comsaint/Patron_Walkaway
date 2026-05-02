"""L1 ``run_bet_map`` membership: one row per (``run_id``, ``bet``) aligned with ``run_fact_v1``."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from layered_data_assets.l0_paths import validate_source_snapshot_id
from layered_data_assets.preprocess_bet_v1 import (
    _manifest_hashes_for_output,
    _manifest_ingestion_delay_placeholder,
    manifest_output_relative_uri,
)
from layered_data_assets.run_fact_v1 import (
    RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
    SOURCE_NAMESPACE_DEFAULT,
    materialize_run_boundary_temp_tables,
    _run_id_sql_expr,
    _validate_gaming_day_partition_value,
)
_RUN_BET_MAP_TRANSFORM_VERSION = "v1"


def materialize_run_bet_map_v1(
    *,
    con: Any,
    input_paths: list[Path],
    output_parquet: Path,
    run_end_gaming_day: str,
    run_break_min: float,
    run_definition_version: str = RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
    source_namespace: str = SOURCE_NAMESPACE_DEFAULT,
) -> dict[str, Any]:
    """Emit ``run_bet_map`` rows for runs ending on ``run_end_gaming_day`` (same filter as ``run_fact``).

    Requires the same ``input_paths`` / boundary params as ``materialize_run_fact_v1`` so ``run_id``
    matches. Creates temp ``run_boundary_bets`` + ``run_fact_staging`` then COPY join result.

    Returns stats: ``row_count``, ``time_range_min``, ``time_range_max`` (from ``payout_complete_dtm``).
    """
    if not input_paths:
        raise ValueError("input_paths must be non-empty")
    day = _validate_gaming_day_partition_value(run_end_gaming_day)
    materialize_run_boundary_temp_tables(
        con,
        input_paths=input_paths,
        run_break_min=run_break_min,
        run_definition_version=run_definition_version,
        source_namespace=source_namespace,
    )
    day_sql = day.replace("'", "''")
    rid = _run_id_sql_expr(table_prefix="r.")
    inner = f"""
SELECT
  {rid} AS run_id,
  b.bet_id,
  b.player_id,
  b.payout_complete_dtm,
  CAST(b.gaming_day AS VARCHAR) AS bet_gaming_day,
  CAST(r.run_end_gaming_day AS VARCHAR) AS run_end_gaming_day
FROM run_boundary_bets b
INNER JOIN run_fact_staging r
  ON b.player_id = r.player_id AND b.run_seq = r.run_seq
WHERE CAST(r.run_end_gaming_day AS VARCHAR) = '{day_sql}'
ORDER BY run_id, b.payout_complete_dtm ASC, b.bet_id ASC
"""
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    out = str(output_parquet.resolve()).replace("\\", "/").replace("'", "''")
    con.execute(f"COPY ({inner.strip()}) TO '{out}' (FORMAT PARQUET);\n")
    count_row = con.execute(
        "SELECT COUNT(*) FROM read_parquet(?)", [str(output_parquet.resolve())]
    ).fetchone()
    if count_row is None:
        raise RuntimeError("COUNT(*) on run_bet_map parquet returned no row")
    n = int(count_row[0])
    tr = con.execute(
        """
        SELECT
          CAST(MIN(payout_complete_dtm) AS VARCHAR),
          CAST(MAX(payout_complete_dtm) AS VARCHAR)
        FROM read_parquet(?)
        """,
        [str(output_parquet.resolve())],
    ).fetchone()
    t0, t1 = (None, None) if tr is None else (tr[0], tr[1])
    return {
        "row_count": n,
        "time_range_min": t0,
        "time_range_max": t1,
    }


def build_run_bet_map_manifest(
    *,
    source_snapshot_id: str,
    run_end_gaming_day: str,
    l0_fingerprint_path: Path | None,
    l1_preprocess_gaming_day: str,
    output_parquet: Path,
    manifest_uri_anchor: Path,
    stats: dict[str, Any],
) -> dict[str, Any]:
    """Build manifest dict for ``run_bet_map``."""
    validate_source_snapshot_id(source_snapshot_id)
    day = _validate_gaming_day_partition_value(run_end_gaming_day)
    pre_gd = l1_preprocess_gaming_day.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pre_gd):
        raise ValueError(f"l1_preprocess_gaming_day must be YYYY-MM-DD, got {l1_preprocess_gaming_day!r}")
    part_run_fact = f"l1/run_fact/run_end_gaming_day={day}"
    part_preprocess = f"l1/t_bet/gaming_day={pre_gd}"
    parts = [part_run_fact, part_preprocess]
    base_hashes = _manifest_hashes_for_output(l0_fingerprint_path)
    hashes = [base_hashes[0]] * len(parts)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    min_ev = stats.get("time_range_min") or "1970-01-01T00:00:00Z"
    max_ev = stats.get("time_range_max") or min_ev
    out_uri = manifest_output_relative_uri(output_parquet, manifest_uri_anchor)
    return {
        "artifact_kind": "run_bet_map",
        "partition_keys": {"run_end_gaming_day": day, "source_snapshot_id": source_snapshot_id.strip()},
        "definition_version": RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
        "feature_version": "na_l1_run_bet_map",
        "transform_version": _RUN_BET_MAP_TRANSFORM_VERSION,
        "source_partitions": parts,
        "source_hashes": hashes,
        "source_snapshot_id": source_snapshot_id.strip(),
        "preprocessing_rule_id": "preprocess_bet_v1",
        "preprocessing_rule_version": "v1",
        "published_snapshot_id": None,
        "ingestion_fix_rule_id": None,
        "ingestion_fix_rule_version": None,
        "row_count": int(stats["row_count"]),
        "time_range": {"min_event_time": str(min_ev), "max_event_time": str(max_ev)},
        "built_at": now,
        "ingestion_delay_summary": _manifest_ingestion_delay_placeholder(),
        "output_relative_uri": out_uri,
    }
