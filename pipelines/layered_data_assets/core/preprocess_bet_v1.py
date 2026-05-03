"""DuckDB-driven ``preprocess_bet_v1`` filter + dedup + ordered COPY to Parquet.

BET-DQ-03 (rated-only) uses an optional ``player_id`` allow-list Parquet
(``--eligible-player-ids-parquet``). Build that list with
``trainer.identity.build_rated_eligible_player_ids_df(sessions_df, cutoff_dtm)`` (same
semantics as ``build_canonical_mapping_from_df``). Standalone LDA: write that
frame to Parquet and pass the path; future in-trainer runs should call the same
function instead of duplicating rules here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipelines.layered_data_assets.io.ingestion_delay_summary_v1 import manifest_ingestion_delay_placeholder
from pipelines.layered_data_assets.io.l0_paths import validate_source_snapshot_id
from pipelines.layered_data_assets.core.preprocess_bet_ingestion_fix_registry_v1 import (
    load_preprocess_bet_ingestion_fix_registry,
    resolve_bet_ingest_fix004_cap_binding,
)

_PREPROCESS_RULE_ID = "preprocess_bet_v1"
_PREPROCESS_RULE_VERSION = "v1"

# Columns referenced unconditionally by preprocess SQL (WHERE / PARTITION BY / ORDER BY).
_PREPROCESS_T_BET_REQUIRED_COLUMNS: frozenset[str] = frozenset({"player_id", "bet_id", "gaming_day"})


def validate_preprocess_bet_input_columns(columns: set[str]) -> None:
    """Reject non-``t_bet`` inputs before DuckDB bind (clearer than BinderException).

    Args:
        columns: Union of column names from all input Parquet files.

    Raises:
        ValueError: If a required L0 ``t_bet`` column is missing.
    """
    missing = sorted(_PREPROCESS_T_BET_REQUIRED_COLUMNS - columns)
    if not missing:
        return
    sample_n = 25
    sorted_cols = sorted(columns)
    head = ", ".join(sorted_cols[:sample_n])
    tail = f" (+{len(sorted_cols) - sample_n} more)" if len(sorted_cols) > sample_n else ""
    raise ValueError(
        "preprocess_bet_v1 requires L0-style t_bet columns "
        f"{sorted(_PREPROCESS_T_BET_REQUIRED_COLUMNS)}; missing: {missing}. "
        f"Input columns (sample): {head}{tail}. "
        "Training/feature slices (e.g. baseline_for_baseline_models.parquet) are not valid sources."
    )


def manifest_output_relative_uri(output_parquet: Path, uri_anchor: Path) -> str:
    """Return ``output_parquet`` as a POSIX path relative to ``uri_anchor`` (e.g. repo root).

    Manifest ``output_relative_uri`` must not be an absolute filesystem path.
    """
    out = output_parquet.resolve()
    anchor = uri_anchor.resolve()
    try:
        return out.relative_to(anchor).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"output_parquet must resolve under uri_anchor; output={out}, anchor={anchor}"
        ) from exc


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


def _preprocess_where_fragments(
    gaming_day: str,
    columns: set[str],
    dummy_ids_table_sql: str | None,
    eligible_ids_table_sql: str | None,
) -> list[str]:
    """Return SQL fragments joined with AND for the ``filtered`` CTE."""
    gd = _gaming_day_literal(gaming_day)
    parts = [
        "player_id IS NOT NULL",
        "player_id <> -1",
        "bet_id IS NOT NULL",
        f"gaming_day = {gd}",
    ]
    if "is_deleted" in columns:
        parts.append("(TRY_CAST(is_deleted AS INTEGER) IS NULL OR TRY_CAST(is_deleted AS INTEGER) = 0)")
    if "is_canceled" in columns:
        parts.append("(TRY_CAST(is_canceled AS INTEGER) IS NULL OR TRY_CAST(is_canceled AS INTEGER) = 0)")
    if "is_manual" in columns:
        parts.append("(TRY_CAST(is_manual AS INTEGER) IS NULL OR TRY_CAST(is_manual AS INTEGER) = 0)")
    if dummy_ids_table_sql:
        parts.append("player_id NOT IN (SELECT player_id FROM dummy_ids)")
    if eligible_ids_table_sql:
        parts.append("player_id IN (SELECT player_id FROM eligible_ids)")
    return parts


def _preprocess_with_clause_prefix(
    dummy_ids_table_sql: str | None,
    eligible_ids_table_sql: str | None,
) -> str:
    """Return DuckDB ``WITH`` prefix including optional ``dummy_ids`` / ``eligible_ids`` CTEs."""
    ctes: list[str] = []
    if dummy_ids_table_sql:
        ctes.append(f"dummy_ids AS ({dummy_ids_table_sql})")
    if eligible_ids_table_sql:
        ctes.append(f"eligible_ids AS ({eligible_ids_table_sql})")
    if not ctes:
        return "WITH "
    return "WITH " + ", ".join(ctes) + ", "


def _preprocess_order_columns(columns: set[str]) -> tuple[str, str]:
    """Return ``(order_etl, order_payout)`` expressions for dedupe and final sort."""
    order_etl = "__etl_insert_Dtm" if "__etl_insert_Dtm" in columns else "CAST(NULL AS TIMESTAMP)"
    order_payout = "payout_complete_dtm" if "payout_complete_dtm" in columns else "CAST(NULL AS TIMESTAMP)"
    return order_etl, order_payout


def _preprocess_pipeline_select_sql(
    *,
    rp_list: str,
    with_prefix: str,
    where_sql: str,
    order_etl: str,
    order_payout: str,
    ingest_delay_cap_sec: int | None,
) -> str:
    """Assemble dedupe pipeline SQL (no ``COPY`` wrapper)."""
    if ingest_delay_cap_sec is None:
        ranked_from = "filtered"
        order_by_observed = f"{order_etl} DESC NULLS LAST, bet_id DESC"
    else:
        ranked_from = "with_capped"
        order_by_observed = "__etl_insert_Dtm_synthetic DESC NULLS LAST, bet_id DESC"
    capped_cte = ""
    if ingest_delay_cap_sec is not None:
        cap = int(ingest_delay_cap_sec)
        capped_cte = f""",
with_capped AS (
  SELECT
    filtered.*,
    CASE
      WHEN TRY_CAST(__etl_insert_Dtm AS TIMESTAMP) IS NOT NULL
       AND TRY_CAST(payout_complete_dtm AS TIMESTAMP) IS NOT NULL
      THEN LEAST(
        TRY_CAST(__etl_insert_Dtm AS TIMESTAMP),
        TRY_CAST(payout_complete_dtm AS TIMESTAMP) + INTERVAL {cap} SECOND
      )
      ELSE TRY_CAST(__etl_insert_Dtm AS TIMESTAMP)
    END AS __etl_insert_Dtm_synthetic
  FROM filtered
)"""
    return f"""
{with_prefix}src AS (
  SELECT * FROM read_parquet([{rp_list}])
),
filtered AS (
  SELECT * FROM src
  WHERE {where_sql}
){capped_cte},
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY bet_id
      ORDER BY {order_by_observed}
    ) AS _rn
  FROM {ranked_from}
),
deduped AS (
  SELECT * EXCLUDE (_rn) FROM ranked WHERE _rn = 1
)
SELECT * FROM deduped
ORDER BY {order_payout} ASC NULLS LAST, bet_id ASC
"""


def build_preprocess_sql(
    *,
    input_paths: list[Path],
    output_parquet: Path,
    gaming_day: str,
    dummy_ids_table_sql: str | None,
    eligible_ids_table_sql: str | None,
    columns: set[str],
    ingest_delay_cap_sec: int | None = None,
) -> str:
    """Build a single DuckDB ``COPY (SELECT ...) TO ...`` statement (no bind params)."""
    rp_list = _read_parquet_list_sql(input_paths)
    where_sql = " AND ".join(
        _preprocess_where_fragments(gaming_day, columns, dummy_ids_table_sql, eligible_ids_table_sql)
    )
    order_etl, order_payout = _preprocess_order_columns(columns)
    with_prefix = _preprocess_with_clause_prefix(dummy_ids_table_sql, eligible_ids_table_sql)
    inner = _preprocess_pipeline_select_sql(
        rp_list=rp_list,
        with_prefix=with_prefix,
        where_sql=where_sql,
        order_etl=order_etl,
        order_payout=order_payout,
        ingest_delay_cap_sec=ingest_delay_cap_sec,
    )
    out = str(output_parquet.resolve()).replace("\\", "/").replace("'", "''")
    return f"COPY ({inner.strip()}) TO '{out}' (FORMAT PARQUET);\n"


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
    if row is None:
        return None, None
    return row[0], row[1]


def _union_input_parquet_columns(con: Any, input_paths: list[Path]) -> set[str]:
    """Union of column names across all input Parquet files."""
    cols = parquet_columns(con, input_paths[0])
    for p in input_paths[1:]:
        cols = cols | parquet_columns(con, p)
    return cols


def _dummy_ids_sql(dummy_player_ids_parquet: Path | None, gaps: list[str]) -> str | None:
    """Return subselect SQL for dummy ``player_id`` set, or ``None`` with a preprocessing gap."""
    if dummy_player_ids_parquet is None:
        gaps.append("BET-DQ-02 skipped: no dummy_player_ids parquet")
        return None
    if not dummy_player_ids_parquet.is_file():
        raise FileNotFoundError(f"dummy_player_ids parquet not found: {dummy_player_ids_parquet}")
    dp = dummy_player_ids_parquet.resolve().as_posix().replace("'", "''")
    return f"SELECT player_id FROM read_parquet('{dp}')"


def _eligible_ids_sql(eligible_player_ids_parquet: Path | None, gaps: list[str]) -> str | None:
    """Return subselect SQL for eligible ``player_id`` set, or ``None`` with a preprocessing gap."""
    if eligible_player_ids_parquet is None:
        gaps.append("BET-DQ-03 skipped: no eligible_player_ids parquet")
        return None
    if not eligible_player_ids_parquet.is_file():
        raise FileNotFoundError(f"eligible_player_ids parquet not found: {eligible_player_ids_parquet}")
    ep = eligible_player_ids_parquet.resolve().as_posix().replace("'", "''")
    return f"SELECT player_id FROM read_parquet('{ep}')"


def _preprocess_subrules_applied(
    dummy_sql: str | None,
    elig_sql: str | None,
    *,
    ingestion_fix_rule_ids: list[str] | None = None,
) -> list[str]:
    """List preprocess subrule ids applied for this run."""
    subrules = ["BET-PK-01", "BET-PK-02", "BET-DQ-01", "BET-ORD-01"]
    if dummy_sql:
        subrules.append("BET-DQ-02")
    if elig_sql:
        subrules.append("BET-DQ-03")
    for rid in ingestion_fix_rule_ids or []:
        subrules.append(rid)
    return subrules


def _validate_columns_for_ingest_cap(columns: set[str]) -> None:
    """Require ETL + payout columns when applying synthetic observed-at cap."""
    need = frozenset({"__etl_insert_Dtm", "payout_complete_dtm"})
    missing = sorted(need - columns)
    if missing:
        raise ValueError(
            "ingestion fix registry cap requires L0 t_bet columns "
            f"{sorted(need)}; missing: {missing}."
        )


def run_preprocess_bet_v1(
    *,
    con: Any,
    input_paths: list[Path],
    output_parquet: Path,
    gaming_day: str,
    dummy_player_ids_parquet: Path | None,
    eligible_player_ids_parquet: Path | None,
    ingestion_fix_registry_path: Path | None = None,
    ingestion_fix_registry_version_expected: str | None = None,
) -> dict[str, Any]:
    """Execute preprocess SQL; return stats dict (row_count, subrules_applied, gaps)."""
    if not input_paths:
        raise ValueError("input_paths must be non-empty")
    gaps: list[str] = []
    cols = _union_input_parquet_columns(con, input_paths)
    validate_preprocess_bet_input_columns(cols)
    ingest_cap: int | None = None
    fix_rule_id: str | None = None
    fix_rule_version: str | None = None
    applied_fix_rules: list[str] = []
    ingestion_fix_ids: list[str] = []
    if ingestion_fix_registry_path is not None:
        reg = load_preprocess_bet_ingestion_fix_registry(ingestion_fix_registry_path.resolve())
        if ingestion_fix_registry_version_expected is not None:
            got_ver = reg.get("registry_version")
            if got_ver != ingestion_fix_registry_version_expected:
                raise ValueError(
                    "ingestion fix registry_version mismatch: "
                    f"expected {ingestion_fix_registry_version_expected!r}, got {got_ver!r}"
                )
        ingest_cap, fix_rule_id, fix_rule_version, applied_fix_rules = resolve_bet_ingest_fix004_cap_binding(reg)
        _validate_columns_for_ingest_cap(cols)
        ingestion_fix_ids.append(fix_rule_id)
    dummy_sql = _dummy_ids_sql(dummy_player_ids_parquet, gaps)
    elig_sql = _eligible_ids_sql(eligible_player_ids_parquet, gaps)
    output_parquet.parent.mkdir(parents=True, exist_ok=True)
    stmt = build_preprocess_sql(
        input_paths=input_paths,
        output_parquet=output_parquet,
        gaming_day=gaming_day,
        dummy_ids_table_sql=dummy_sql,
        eligible_ids_table_sql=elig_sql,
        columns=cols,
        ingest_delay_cap_sec=ingest_cap,
    )
    con.execute(stmt)
    count_row = con.execute(
        "SELECT COUNT(*) FROM read_parquet(?)", [str(output_parquet.resolve())]
    ).fetchone()
    if count_row is None:
        raise RuntimeError("COUNT(*) on output parquet returned no row")
    n = count_row[0]
    out_cols = parquet_columns(con, output_parquet)
    tr = _time_range_from_output(con, output_parquet, out_cols)
    subrules = _preprocess_subrules_applied(dummy_sql, elig_sql, ingestion_fix_rule_ids=ingestion_fix_ids)
    out: dict[str, Any] = {
        "row_count": int(n),
        "time_range_min": tr[0],
        "time_range_max": tr[1],
        "preprocess_subrules_applied": subrules,
        "preprocessing_gaps": gaps,
    }
    if ingest_cap is not None:
        out["ingest_delay_cap_sec_applied"] = int(ingest_cap)
        out["ingestion_fix_rule_id"] = fix_rule_id
        out["ingestion_fix_rule_version"] = fix_rule_version
        out["applied_fix_rules"] = applied_fix_rules
        out["ingestion_fix_registry_path"] = str(ingestion_fix_registry_path.resolve().as_posix())
    return out


def _source_hashes_from_l0_fingerprint(l0_fingerprint_path: Path | None) -> list[str]:
    """Extract ``sha256:...`` entries from fingerprint ``inputs``; may be empty."""
    hashes: list[str] = []
    if l0_fingerprint_path is None or not l0_fingerprint_path.is_file():
        return hashes
    fp = json.loads(l0_fingerprint_path.read_text(encoding="utf-8"))
    inputs = fp.get("inputs")
    if not isinstance(inputs, list):
        return hashes
    for item in inputs:
        if isinstance(item, dict) and "sha256" in item:
            hashes.append(f"sha256:{item['sha256']}")
    return hashes


def _manifest_hashes_for_output(l0_fingerprint_path: Path | None) -> list[str]:
    """Fingerprint-derived hashes, padded to at least one entry for schema stability."""
    hashes = _source_hashes_from_l0_fingerprint(l0_fingerprint_path)
    while len(hashes) < 1:
        hashes.append("sha256:unknown")
    return hashes


def _l1_bet_clean_manifest_dict(
    *,
    source_snapshot_id: str,
    gaming_day: str,
    part_id: str,
    source_hashes: list[str],
    built_at: str,
    min_event_time: str,
    max_event_time: str,
    stats: dict[str, Any],
    output_relative_uri: str,
    ingestion_delay_summary: dict[str, Any] | None = None,
    ingestion_fix_rule_id: str | None = None,
    ingestion_fix_rule_version: str | None = None,
    applied_fix_rules: list[str] | None = None,
) -> dict[str, Any]:
    """Build the ``l1_t_bet_clean`` manifest object."""
    ids = ingestion_delay_summary if ingestion_delay_summary is not None else manifest_ingestion_delay_placeholder()
    body: dict[str, Any] = {
        "artifact_kind": "l1_t_bet_clean",
        "partition_keys": {"gaming_day": gaming_day.strip(), "source_snapshot_id": source_snapshot_id.strip()},
        "definition_version": "layered_data_assets_v1",
        "feature_version": "na_l1_preprocess",
        "transform_version": _PREPROCESS_RULE_VERSION,
        "source_partitions": [part_id],
        "source_hashes": source_hashes,
        "source_snapshot_id": source_snapshot_id.strip(),
        "preprocessing_rule_id": _PREPROCESS_RULE_ID,
        "preprocessing_rule_version": _PREPROCESS_RULE_VERSION,
        "published_snapshot_id": None,
        "ingestion_fix_rule_id": ingestion_fix_rule_id,
        "ingestion_fix_rule_version": ingestion_fix_rule_version,
        "row_count": int(stats["row_count"]),
        "time_range": {"min_event_time": min_event_time, "max_event_time": max_event_time},
        "built_at": built_at,
        "ingestion_delay_summary": ids,
        "preprocess_subrules_applied": stats.get("preprocess_subrules_applied", []),
        "preprocessing_gaps": stats.get("preprocessing_gaps", []),
        "output_relative_uri": output_relative_uri,
    }
    if applied_fix_rules:
        body["applied_fix_rules"] = list(applied_fix_rules)
    return body


def build_preprocess_manifest(
    *,
    source_snapshot_id: str,
    gaming_day: str,
    l0_fingerprint_path: Path | None,
    output_parquet: Path,
    manifest_uri_anchor: Path,
    stats: dict[str, Any],
    ingestion_delay_summary: dict[str, Any] | None = None,
    ingestion_fix_rule_id: str | None = None,
    ingestion_fix_rule_version: str | None = None,
    applied_fix_rules: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble a manifest dict valid against ``manifest_layered_data_assets.schema.json`` (L1 bet clean).

    ``manifest_uri_anchor`` is usually the repository root so ``output_relative_uri`` matches
    ``data/l1_layered/...`` style paths.
    """
    validate_source_snapshot_id(source_snapshot_id)
    part_id = f"l0/t_bet/gaming_day={gaming_day.strip()}"
    hashes = _manifest_hashes_for_output(l0_fingerprint_path)[:1]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    min_ev = stats.get("time_range_min") or "1970-01-01T00:00:00Z"
    max_ev = stats.get("time_range_max") or min_ev
    out_uri = manifest_output_relative_uri(output_parquet, manifest_uri_anchor)
    fid = ingestion_fix_rule_id
    fver = ingestion_fix_rule_version
    if fid is None and stats.get("ingestion_fix_rule_id") is not None:
        fid = str(stats["ingestion_fix_rule_id"])
    if fver is None and stats.get("ingestion_fix_rule_version") is not None:
        fver = str(stats["ingestion_fix_rule_version"])
    afr = applied_fix_rules
    if afr is None and stats.get("applied_fix_rules") is not None:
        afr = list(stats["applied_fix_rules"])
    return _l1_bet_clean_manifest_dict(
        source_snapshot_id=source_snapshot_id,
        gaming_day=gaming_day,
        part_id=part_id,
        source_hashes=hashes,
        built_at=now,
        min_event_time=str(min_ev),
        max_event_time=str(max_ev),
        stats=stats,
        output_relative_uri=out_uri,
        ingestion_delay_summary=ingestion_delay_summary,
        ingestion_fix_rule_id=fid,
        ingestion_fix_rule_version=fver,
        applied_fix_rules=afr,
    )
