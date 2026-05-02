"""L1 ``run_fact`` materialization (gap-based runs, ``run_end_gaming_day`` partition).

MVP 限制：僅依輸入 Parquet 內之列切 run；若需跨 ``gaming_day`` 之連續段，請餵入含前後日之列
（與 preprocess 單日分區檔并用），否則日界會誤判為新 run。
"""
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
RUN_BREAK_MIN_DEFAULT = 30
RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT = "run_boundary_v1"
SOURCE_NAMESPACE_DEFAULT = "layered_data_assets_l1"
_RUN_FACT_TRANSFORM_VERSION = "v1"


def _read_parquet_list_sql(paths: list[Path]) -> str:
    """Build ``'path1', 'path2'`` list for ``read_parquet([...])`` (SQL-escaped)."""
    parts: list[str] = []
    for p in paths:
        s = p.resolve().as_posix().replace("'", "''")
        parts.append(f"'{s}'")
    return ", ".join(parts)


def _validate_run_break_min(run_break_min: float) -> None:
    """Guardrail aligned with trainer lookback bounds (see ``trainer/features/features.py``)."""
    if run_break_min < 0 or run_break_min > 10_000:
        raise ValueError(f"run_break_min must be in [0, 10000], got {run_break_min!r}")


def _validate_gaming_day_partition_value(run_end_gaming_day: str) -> str:
    """Return stripped ``YYYY-MM-DD`` partition value for path segments."""
    s = run_end_gaming_day.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"run_end_gaming_day must be YYYY-MM-DD, got {run_end_gaming_day!r}")
    return s


def build_create_run_boundary_bets_sql(
    *,
    input_paths: list[Path],
    run_break_min: float,
) -> str:
    """Create temp ``run_boundary_bets``: one row per bet with ``run_seq`` (no ``run_id`` yet)."""
    _validate_run_break_min(run_break_min)
    rp = _read_parquet_list_sql(input_paths)
    gap = float(run_break_min)
    return f"""
CREATE OR REPLACE TEMP TABLE run_boundary_bets AS
WITH src AS (
  SELECT * FROM read_parquet([{rp}])
),
ord AS (
  SELECT
    *,
    LAG(payout_complete_dtm) OVER (
      PARTITION BY player_id
      ORDER BY payout_complete_dtm ASC, bet_id ASC
    ) AS prev_payout
  FROM src
),
marked AS (
  SELECT
    *,
    CASE
      WHEN prev_payout IS NULL THEN 1
      WHEN (EPOCH(payout_complete_dtm) - EPOCH(prev_payout)) / 60.0 >= {gap} THEN 1
      ELSE 0
    END AS is_new_run
  FROM ord
),
grp AS (
  SELECT
    *,
    SUM(is_new_run) OVER (
      PARTITION BY player_id
      ORDER BY payout_complete_dtm ASC, bet_id ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS run_seq
  FROM marked
),
ord2 AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      ORDER BY player_id, run_seq, payout_complete_dtm ASC, bet_id ASC
    ) AS global_ord
  FROM grp
)
SELECT * FROM ord2;
"""


def build_create_run_fact_staging_sql(
    *,
    run_definition_version: str,
    source_namespace: str,
) -> str:
    """Aggregate ``run_boundary_bets`` into temp ``run_fact_staging`` (per-run stats, no ``run_id``)."""
    rd = run_definition_version.replace("'", "''")
    sn = source_namespace.replace("'", "''")
    return f"""
CREATE OR REPLACE TEMP TABLE run_fact_staging AS
SELECT
  player_id,
  run_seq,
  ARG_MIN(bet_id, global_ord) AS first_bet_id,
  MIN(payout_complete_dtm) AS run_start_ts,
  MAX(payout_complete_dtm) AS run_end_ts,
  ARG_MAX(bet_id, global_ord) AS last_bet_id,
  ARG_MAX(CAST(gaming_day AS VARCHAR), global_ord) AS run_end_gaming_day,
  COUNT(*)::BIGINT AS bet_count,
  '{rd}' AS run_definition_version,
  '{sn}' AS source_namespace
FROM run_boundary_bets
GROUP BY player_id, run_seq;
"""


def materialize_run_boundary_temp_tables(
    con: Any,
    *,
    input_paths: list[Path],
    run_break_min: float,
    run_definition_version: str,
    source_namespace: str,
) -> None:
    """Create ``run_boundary_bets`` and ``run_fact_staging`` temp tables (shared by run_fact / run_bet_map)."""
    if not input_paths:
        raise ValueError("input_paths must be non-empty")
    con.execute(
        build_create_run_boundary_bets_sql(input_paths=input_paths, run_break_min=run_break_min)
    )
    con.execute(
        build_create_run_fact_staging_sql(
            run_definition_version=run_definition_version,
            source_namespace=source_namespace,
        )
    )


def _run_id_sql_expr(*, table_prefix: str = "") -> str:
    """SQL fragment for ``run_id`` matching :func:`derive_run_id` canonical JSON + SHA-256.

    ``table_prefix`` example: ``'r.'`` when joining ``run_fact_staging r``.
    """
    p = table_prefix
    canon = (
        f"'{{\"first_bet_id\":\"' || CAST({p}first_bet_id AS VARCHAR) || '\",\"player_id\":' || CAST({p}player_id AS VARCHAR) "
        f"|| ',\"run_definition_version\":\"' || {p}run_definition_version || '\",\"run_start_ts\":\"' "
        f"|| strftime({p}run_start_ts, '%Y-%m-%dT%H:%M:%S.%f') || '\",\"source_namespace\":\"' || {p}source_namespace || '\"}}'"
    )
    return f"'run_' || substr(sha256({canon}), 1, 32)"


_RUN_FACT_TIME_RANGE_SQL = """
SELECT
  CAST(MIN(run_start_ts) AS VARCHAR),
  CAST(MAX(run_end_ts) AS VARCHAR)
FROM read_parquet(?)
"""


def _run_fact_copy_inner_sql(*, day_sql_escaped: str, run_id_expr: str) -> str:
    """Build inner SELECT for one ``run_end_gaming_day`` ``run_fact`` partition."""
    return f"""
SELECT
  {run_id_expr} AS run_id,
  player_id,
  first_bet_id,
  last_bet_id,
  run_start_ts,
  run_end_ts,
  CAST(run_end_gaming_day AS VARCHAR) AS run_end_gaming_day,
  bet_count,
  run_definition_version,
  source_namespace
FROM run_fact_staging
WHERE CAST(run_end_gaming_day AS VARCHAR) = '{day_sql_escaped}'
ORDER BY run_id
"""


def _copy_select_to_parquet(con: Any, inner_sql: str, output_parquet: Path) -> None:
    """COPY ``(inner_sql)`` to ``output_parquet``; create parent directories."""
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    out = str(output_parquet.resolve()).replace("\\", "/").replace("'", "''")
    con.execute(f"COPY ({inner_sql.strip()}) TO '{out}' (FORMAT PARQUET);\n")


def _stats_from_copied_parquet(
    con: Any,
    output_parquet: Path,
    *,
    count_error_msg: str,
    time_range_sql: str,
) -> dict[str, Any]:
    """Row count and min/max time strings from a Parquet written by :func:`_copy_select_to_parquet`."""
    path = str(output_parquet.resolve())
    count_row = con.execute("SELECT COUNT(*) FROM read_parquet(?)", [path]).fetchone()
    if count_row is None:
        raise RuntimeError(count_error_msg)
    n = int(count_row[0])
    tr = con.execute(time_range_sql, [path]).fetchone()
    t0, t1 = (None, None) if tr is None else (tr[0], tr[1])
    return {"row_count": n, "time_range_min": t0, "time_range_max": t1}


def materialize_run_fact_v1(
    *,
    con: Any,
    input_paths: list[Path],
    output_parquet: Path,
    run_end_gaming_day: str,
    run_break_min: float = RUN_BREAK_MIN_DEFAULT,
    run_definition_version: str = RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
    source_namespace: str = SOURCE_NAMESPACE_DEFAULT,
) -> dict[str, Any]:
    """Stage runs, assign ``run_id``, COPY one ``run_end_gaming_day`` partition to Parquet.

    Returns stats: ``row_count``, ``time_range_min``, ``time_range_max``.
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
    inner = _run_fact_copy_inner_sql(
        day_sql_escaped=day.replace("'", "''"),
        run_id_expr=_run_id_sql_expr(),
    )
    _copy_select_to_parquet(con, inner, output_parquet)
    return _stats_from_copied_parquet(
        con,
        output_parquet,
        count_error_msg="COUNT(*) on run_fact parquet returned no row",
        time_range_sql=_RUN_FACT_TIME_RANGE_SQL,
    )


def _run_fact_manifest_dict(
    *,
    source_snapshot_id: str,
    run_end_gaming_day: str,
    part_preprocess: str,
    source_hashes: list[str],
    built_at: str,
    min_event_time: str,
    max_event_time: str,
    stats: dict[str, Any],
    output_relative_uri: str,
) -> dict[str, Any]:
    """Build the ``run_fact`` manifest object (``artifact_kind`` = ``run_fact``)."""
    return {
        "artifact_kind": "run_fact",
        "partition_keys": {"run_end_gaming_day": run_end_gaming_day, "source_snapshot_id": source_snapshot_id.strip()},
        "definition_version": RUN_BOUNDARY_DEFINITION_VERSION_DEFAULT,
        "feature_version": "na_l1_run_fact",
        "transform_version": _RUN_FACT_TRANSFORM_VERSION,
        "source_partitions": [part_preprocess],
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


def build_run_fact_manifest(
    *,
    source_snapshot_id: str,
    run_end_gaming_day: str,
    l0_fingerprint_path: Path | None,
    l1_preprocess_gaming_day: str,
    output_parquet: Path,
    manifest_uri_anchor: Path,
    stats: dict[str, Any],
) -> dict[str, Any]:
    """Build manifest dict for ``run_fact`` (``artifact_kind`` = ``run_fact``)."""
    validate_source_snapshot_id(source_snapshot_id)
    day = _validate_gaming_day_partition_value(run_end_gaming_day)
    pre_gd = l1_preprocess_gaming_day.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", pre_gd):
        raise ValueError(f"l1_preprocess_gaming_day must be YYYY-MM-DD, got {l1_preprocess_gaming_day!r}")
    part_preprocess = f"l1/t_bet/gaming_day={pre_gd}"
    hashes = _manifest_hashes_for_output(l0_fingerprint_path)[:1]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    min_ev = stats.get("time_range_min") or "1970-01-01T00:00:00Z"
    max_ev = stats.get("time_range_max") or min_ev
    out_uri = manifest_output_relative_uri(output_parquet, manifest_uri_anchor)
    return _run_fact_manifest_dict(
        source_snapshot_id=source_snapshot_id,
        run_end_gaming_day=day,
        part_preprocess=part_preprocess,
        source_hashes=hashes,
        built_at=now,
        min_event_time=str(min_ev),
        max_event_time=str(max_ev),
        stats=stats,
        output_relative_uri=out_uri,
    )
