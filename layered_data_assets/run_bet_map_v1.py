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
    _copy_select_to_parquet,
    _run_id_sql_expr,
    _stats_from_copied_parquet,
    _validate_gaming_day_partition_value,
    materialize_run_boundary_temp_tables,
)
_RUN_BET_MAP_TRANSFORM_VERSION = "v1"

_RUN_BET_MAP_TIME_RANGE_SQL = """
SELECT
  CAST(MIN(payout_complete_dtm) AS VARCHAR),
  CAST(MAX(payout_complete_dtm) AS VARCHAR)
FROM read_parquet(?)
"""


def _run_bet_map_copy_inner_sql(*, day_sql_escaped: str, run_id_expr: str) -> str:
    """Build inner SELECT for one ``run_end_gaming_day`` ``run_bet_map`` partition."""
    return f"""
SELECT
  {run_id_expr} AS run_id,
  b.bet_id,
  b.player_id,
  b.payout_complete_dtm,
  CAST(b.gaming_day AS VARCHAR) AS bet_gaming_day,
  CAST(r.run_end_gaming_day AS VARCHAR) AS run_end_gaming_day
FROM run_boundary_bets b
INNER JOIN run_fact_staging r
  ON b.player_id = r.player_id AND b.run_seq = r.run_seq
WHERE CAST(r.run_end_gaming_day AS VARCHAR) = '{day_sql_escaped}'
ORDER BY run_id, b.payout_complete_dtm ASC, b.bet_id ASC
"""


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
    inner = _run_bet_map_copy_inner_sql(
        day_sql_escaped=day.replace("'", "''"),
        run_id_expr=_run_id_sql_expr(table_prefix="r."),
    )
    _copy_select_to_parquet(con, inner, output_parquet)
    return _stats_from_copied_parquet(
        con,
        output_parquet,
        count_error_msg="COUNT(*) on run_bet_map parquet returned no row",
        time_range_sql=_RUN_BET_MAP_TIME_RANGE_SQL,
    )


def _run_bet_map_manifest_dict(
    *,
    source_snapshot_id: str,
    run_end_gaming_day: str,
    source_partitions: list[str],
    source_hashes: list[str],
    built_at: str,
    min_event_time: str,
    max_event_time: str,
    stats: dict[str, Any],
    output_relative_uri: str,
) -> dict[str, Any]:
    """Build the ``run_bet_map`` manifest object."""
    return {
        "artifact_kind": "run_bet_map",
        "partition_keys": {"run_end_gaming_day": run_end_gaming_day, "source_snapshot_id": source_snapshot_id.strip()},
        "definition_version": RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
        "feature_version": "na_l1_run_bet_map",
        "transform_version": _RUN_BET_MAP_TRANSFORM_VERSION,
        "source_partitions": source_partitions,
        "source_hashes": source_hashes,
        "source_snapshot_id": source_snapshot_id.strip(),
        "preprocessing_rule_id": "preprocess_bet_v1",
        "preprocessing_rule_version": "v1",
        "published_snapshot_id": None,
        "ingestion_fix_rule_id": None,
        "ingestion_fix_rule_version": None,
        "row_count": int(stats["row_count"]),
        "time_range": {"min_event_time": min_event_time, "max_event_time": max_event_time},
        "built_at": built_at,
        "ingestion_delay_summary": _manifest_ingestion_delay_placeholder(),
        "output_relative_uri": output_relative_uri,
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
    return _run_bet_map_manifest_dict(
        source_snapshot_id=source_snapshot_id,
        run_end_gaming_day=day,
        source_partitions=parts,
        source_hashes=hashes,
        built_at=now,
        min_event_time=str(min_ev),
        max_event_time=str(max_ev),
        stats=stats,
        output_relative_uri=out_uri,
    )
