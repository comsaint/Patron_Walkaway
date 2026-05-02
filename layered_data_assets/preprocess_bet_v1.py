"""DuckDB-driven ``preprocess_bet_v1`` filter + dedup + ordered COPY to Parquet."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from layered_data_assets.l0_paths import validate_source_snapshot_id

_PREPROCESS_RULE_ID = "preprocess_bet_v1"
_PREPROCESS_RULE_VERSION = "v1"


def parquet_columns(con: Any, parquet_path: Path) -> set[str]:
    """Return column names for a single Parquet file via DuckDB ``DESCRIBE``."""
    rows = con.execute(
        "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))", [str(parquet_path.resolve())]
    ).fetchall()
    return {str(r[0]) for r in rows}


def _read_parquet_list_sql(paths: list[Path]) -> str:
    """Build ``'path1', 'path2'`` list elements for ``read_parquet([...])`` (SQL-escaped)."""
    parts: list[str] = []
    for p in paths:
        s = p.resolve().as_posix().replace("'", "''")
        parts.append(f"'{s}'")
    return ", ".join(parts)


def _gaming_day_literal(gaming_day: str) -> str:
    """Validate ``gaming_day`` and return SQL date literal ``DATE '...'``."""
    s = gaming_day.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"gaming_day must be YYYY-MM-DD, got {gaming_day!r}")
    return f"DATE '{s}'"


def build_preprocess_sql(
    *,
    input_paths: list[Path],
    output_parquet: Path,
    gaming_day: str,
    dummy_ids_table_sql: str | None,
    eligible_ids_table_sql: str | None,
    columns: set[str],
) -> str:
    """Build a single DuckDB ``COPY (SELECT ...) TO ...`` statement (no bind params)."""
    rp_list = _read_parquet_list_sql(input_paths)
    gd = _gaming_day_literal(gaming_day)
    where_parts = [
        "player_id IS NOT NULL",
        "player_id <> -1",
        "bet_id IS NOT NULL",
        f"gaming_day = {gd}",
    ]
    if "is_deleted" in columns:
        where_parts.append("(TRY_CAST(is_deleted AS INTEGER) IS NULL OR TRY_CAST(is_deleted AS INTEGER) = 0)")
    if "is_canceled" in columns:
        where_parts.append("(TRY_CAST(is_canceled AS INTEGER) IS NULL OR TRY_CAST(is_canceled AS INTEGER) = 0)")
    if "is_manual" in columns:
        where_parts.append("(TRY_CAST(is_manual AS INTEGER) IS NULL OR TRY_CAST(is_manual AS INTEGER) = 0)")
    if dummy_ids_table_sql:
        where_parts.append("player_id NOT IN (SELECT player_id FROM dummy_ids)")
    if eligible_ids_table_sql:
        where_parts.append("player_id IN (SELECT player_id FROM eligible_ids)")
    where_sql = " AND ".join(where_parts)
    order_etl = "__etl_insert_Dtm" if "__etl_insert_Dtm" in columns else "CAST(NULL AS TIMESTAMP)"
    order_payout = "payout_complete_dtm" if "payout_complete_dtm" in columns else "CAST(NULL AS TIMESTAMP)"

    ctes: list[str] = []
    if dummy_ids_table_sql:
        ctes.append(f"dummy_ids AS ({dummy_ids_table_sql})")
    if eligible_ids_table_sql:
        ctes.append(f"eligible_ids AS ({eligible_ids_table_sql})")
    with_prefix = "WITH " + ", ".join(ctes) + ", " if ctes else "WITH "
    sql = f"""
{with_prefix}src AS (
  SELECT * FROM read_parquet([{rp_list}])
),
filtered AS (
  SELECT * FROM src
  WHERE {where_sql}
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY bet_id
      ORDER BY {order_etl} DESC NULLS LAST, bet_id DESC
    ) AS _rn
  FROM filtered
),
deduped AS (
  SELECT * EXCLUDE (_rn) FROM ranked WHERE _rn = 1
)
SELECT * FROM deduped
ORDER BY {order_payout} ASC NULLS LAST, bet_id ASC
"""
    out = str(output_parquet.resolve()).replace("\\", "/").replace("'", "''")
    return f"COPY ({sql.strip()}) TO '{out}' (FORMAT PARQUET);\n"


def _time_range_from_output(con: Any, output_parquet: Path, out_cols: set[str]) -> tuple[Any, Any]:
    """Return ``(min_payout, max_payout)`` strings or ``(None, None)`` if column absent."""
    if "payout_complete_dtm" not in out_cols:
        return None, None
    row = con.execute(
        """
        SELECT
          CAST(MIN(payout_complete_dtm) AS VARCHAR),
          CAST(MAX(payout_complete_dtm) AS VARCHAR)
        FROM read_parquet(?)
        """,
        [str(output_parquet.resolve())],
    ).fetchone()
    return row[0], row[1]


def run_preprocess_bet_v1(
    *,
    con: Any,
    input_paths: list[Path],
    output_parquet: Path,
    gaming_day: str,
    dummy_player_ids_parquet: Path | None,
    eligible_player_ids_parquet: Path | None,
) -> dict[str, Any]:
    """Execute preprocess SQL; return stats dict (row_count, subrules_applied, gaps)."""
    if not input_paths:
        raise ValueError("input_paths must be non-empty")
    cols = parquet_columns(con, input_paths[0])
    for p in input_paths[1:]:
        cols = cols | parquet_columns(con, p)

    dummy_sql: str | None = None
    gaps: list[str] = []
    if dummy_player_ids_parquet is not None:
        if not dummy_player_ids_parquet.is_file():
            raise FileNotFoundError(f"dummy_player_ids parquet not found: {dummy_player_ids_parquet}")
        dp = dummy_player_ids_parquet.resolve().as_posix().replace("'", "''")
        dummy_sql = f"SELECT player_id FROM read_parquet('{dp}')"
    else:
        gaps.append("BET-DQ-02 skipped: no dummy_player_ids parquet")

    elig_sql: str | None = None
    if eligible_player_ids_parquet is not None:
        if not eligible_player_ids_parquet.is_file():
            raise FileNotFoundError(f"eligible_player_ids parquet not found: {eligible_player_ids_parquet}")
        ep = eligible_player_ids_parquet.resolve().as_posix().replace("'", "''")
        elig_sql = f"SELECT player_id FROM read_parquet('{ep}')"
    else:
        gaps.append("BET-DQ-03 skipped: no eligible_player_ids parquet")

    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    stmt = build_preprocess_sql(
        input_paths=input_paths,
        output_parquet=output_parquet,
        gaming_day=gaming_day,
        dummy_ids_table_sql=dummy_sql,
        eligible_ids_table_sql=elig_sql,
        columns=cols,
    )
    con.execute(stmt)
    n = con.execute(
        "SELECT COUNT(*) FROM read_parquet(?)", [str(output_parquet.resolve())]
    ).fetchone()[0]
    out_cols = parquet_columns(con, output_parquet)
    tr = _time_range_from_output(con, output_parquet, out_cols)
    subrules = ["BET-PK-01", "BET-PK-02", "BET-DQ-01", "BET-ORD-01"]
    if dummy_sql:
        subrules.append("BET-DQ-02")
    if elig_sql:
        subrules.append("BET-DQ-03")
    return {
        "row_count": int(n),
        "time_range_min": tr[0],
        "time_range_max": tr[1],
        "preprocess_subrules_applied": subrules,
        "preprocessing_gaps": gaps,
    }


def build_preprocess_manifest(
    *,
    source_snapshot_id: str,
    gaming_day: str,
    l0_fingerprint_path: Path | None,
    output_parquet: Path,
    stats: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a manifest dict valid against ``manifest_layered_data_assets.schema.json`` (L1 bet clean)."""
    validate_source_snapshot_id(source_snapshot_id)
    part_id = f"l0/t_bet/gaming_day={gaming_day.strip()}"
    hashes: list[str] = []
    if l0_fingerprint_path and l0_fingerprint_path.is_file():
        fp = json.loads(l0_fingerprint_path.read_text(encoding="utf-8"))
        inputs = fp.get("inputs")
        if isinstance(inputs, list):
            for item in inputs:
                if isinstance(item, dict) and "sha256" in item:
                    hashes.append(f"sha256:{item['sha256']}")
    while len(hashes) < 1:
        hashes.append("sha256:unknown")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    min_ev = stats.get("time_range_min") or "1970-01-01T00:00:00Z"
    max_ev = stats.get("time_range_max") or min_ev
    manifest: dict[str, Any] = {
        "artifact_kind": "l1_t_bet_clean",
        "partition_keys": {"gaming_day": gaming_day.strip(), "source_snapshot_id": source_snapshot_id.strip()},
        "definition_version": "layered_data_assets_v1",
        "feature_version": "na_l1_preprocess",
        "transform_version": _PREPROCESS_RULE_VERSION,
        "source_partitions": [part_id],
        "source_hashes": hashes[:1],
        "source_snapshot_id": source_snapshot_id.strip(),
        "preprocessing_rule_id": _PREPROCESS_RULE_ID,
        "preprocessing_rule_version": _PREPROCESS_RULE_VERSION,
        "published_snapshot_id": None,
        "ingestion_fix_rule_id": None,
        "ingestion_fix_rule_version": None,
        "row_count": int(stats["row_count"]),
        "time_range": {"min_event_time": str(min_ev), "max_event_time": str(max_ev)},
        "built_at": now,
        "ingestion_delay_summary": {
            "ingest_delay_p50_sec": None,
            "ingest_delay_p95_sec": None,
            "ingest_delay_p99_sec": None,
            "ingest_delay_max_sec": None,
            "late_row_count": None,
            "late_row_ratio": None,
            "affected_run_count": None,
            "affected_trip_count": None,
        },
        "preprocess_subrules_applied": stats.get("preprocess_subrules_applied", []),
        "preprocessing_gaps": stats.get("preprocessing_gaps", []),
        "output_relative_uri": output_parquet.as_posix(),
    }
    return manifest
