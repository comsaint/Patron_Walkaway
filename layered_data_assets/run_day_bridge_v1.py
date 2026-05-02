"""L1 ``run_day_bridge``: distinct ``run_id`` per ``bet_gaming_day`` (SSOT §5.2 影響範圍掃描)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from layered_data_assets.l0_paths import validate_source_snapshot_id
from layered_data_assets.ingestion_delay_summary_v1 import manifest_ingestion_delay_placeholder
from layered_data_assets.preprocess_bet_v1 import (
    _manifest_hashes_for_output,
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

_RUN_DAY_BRIDGE_TRANSFORM_VERSION = "v1"

_RUN_DAY_BRIDGE_TIME_RANGE_SQL = """
SELECT
  CAST(MIN(run_start_ts) AS VARCHAR),
  CAST(MAX(run_end_ts) AS VARCHAR)
FROM read_parquet(?)
"""


def _run_day_bridge_copy_inner_sql(*, bet_day_sql_escaped: str, run_id_expr: str) -> str:
    """Build inner SELECT for one ``bet_gaming_day`` ``run_day_bridge`` partition."""
    return f"""
SELECT DISTINCT
  {run_id_expr} AS run_id,
  b.player_id,
  CAST(b.gaming_day AS VARCHAR) AS bet_gaming_day,
  CAST(r.run_end_gaming_day AS VARCHAR) AS run_end_gaming_day,
  r.run_start_ts,
  r.run_end_ts
FROM run_boundary_bets b
INNER JOIN run_fact_staging r
  ON b.player_id = r.player_id AND b.run_seq = r.run_seq
WHERE CAST(b.gaming_day AS VARCHAR) = '{bet_day_sql_escaped}'
ORDER BY run_id, b.player_id
"""


def materialize_run_day_bridge_v1(
    *,
    con: Any,
    input_paths: list[Path],
    output_parquet: Path,
    bet_gaming_day: str,
    run_break_min: float,
    run_definition_version: str = RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
    source_namespace: str = SOURCE_NAMESPACE_DEFAULT,
) -> dict[str, Any]:
    """Emit distinct ``(run_id, …)`` for runs that have at least one bet on ``bet_gaming_day``.

    Uses the same ``input_paths`` / boundary params as ``materialize_run_fact_v1`` / ``run_bet_map``.
    """
    if not input_paths:
        raise ValueError("input_paths must be non-empty")
    day = _validate_gaming_day_partition_value(bet_gaming_day, param_name="bet_gaming_day")
    materialize_run_boundary_temp_tables(
        con,
        input_paths=input_paths,
        run_break_min=run_break_min,
        run_definition_version=run_definition_version,
        source_namespace=source_namespace,
    )
    inner = _run_day_bridge_copy_inner_sql(
        bet_day_sql_escaped=day.replace("'", "''"),
        run_id_expr=_run_id_sql_expr(table_prefix="r."),
    )
    _copy_select_to_parquet(con, inner, output_parquet)
    return _stats_from_copied_parquet(
        con,
        output_parquet,
        count_error_msg="COUNT(*) on run_day_bridge parquet returned no row",
        time_range_sql=_RUN_DAY_BRIDGE_TIME_RANGE_SQL,
    )


def _run_day_bridge_manifest_dict(
    *,
    source_snapshot_id: str,
    bet_gaming_day: str,
    source_partitions: list[str],
    source_hashes: list[str],
    built_at: str,
    min_event_time: str,
    max_event_time: str,
    stats: dict[str, Any],
    output_relative_uri: str,
    ingestion_delay_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``run_day_bridge`` manifest object."""
    ids = ingestion_delay_summary if ingestion_delay_summary is not None else manifest_ingestion_delay_placeholder()
    return {
        "artifact_kind": "run_day_bridge",
        "partition_keys": {"bet_gaming_day": bet_gaming_day, "source_snapshot_id": source_snapshot_id.strip()},
        "definition_version": RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
        "feature_version": "na_l1_run_day_bridge",
        "transform_version": _RUN_DAY_BRIDGE_TRANSFORM_VERSION,
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
        "ingestion_delay_summary": ids,
        "output_relative_uri": output_relative_uri,
    }


def build_run_day_bridge_manifest(
    *,
    source_snapshot_id: str,
    bet_gaming_day: str,
    l0_fingerprint_path: Path | None,
    l1_preprocess_gaming_day: str,
    output_parquet: Path,
    manifest_uri_anchor: Path,
    stats: dict[str, Any],
    ingestion_delay_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build manifest dict for ``run_day_bridge`` (partition ``bet_gaming_day``)."""
    validate_source_snapshot_id(source_snapshot_id)
    day = _validate_gaming_day_partition_value(bet_gaming_day, param_name="bet_gaming_day")
    pre = _validate_gaming_day_partition_value(l1_preprocess_gaming_day, param_name="l1_preprocess_gaming_day")
    part_preprocess = f"l1/t_bet/gaming_day={pre}"
    hashes = _manifest_hashes_for_output(l0_fingerprint_path)[:1]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    min_ev = stats.get("time_range_min") or "1970-01-01T00:00:00Z"
    max_ev = stats.get("time_range_max") or min_ev
    out_uri = manifest_output_relative_uri(output_parquet, manifest_uri_anchor)
    return _run_day_bridge_manifest_dict(
        source_snapshot_id=source_snapshot_id,
        bet_gaming_day=day,
        source_partitions=[part_preprocess],
        source_hashes=hashes,
        built_at=now,
        min_event_time=str(min_ev),
        max_event_time=str(max_ev),
        stats=stats,
        output_relative_uri=out_uri,
        ingestion_delay_summary=ingestion_delay_summary,
    )
