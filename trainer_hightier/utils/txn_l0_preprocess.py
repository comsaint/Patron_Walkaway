"""``t_casino_txn`` L0 preprocess: raw partition Parquet → cleaned source layer (DuckDB).

Registry-driven logical observed-at correction and delete-aware dedup per
``Data pipeline - SSOT.md`` §5.2. Quarantine: outputs are ``not_model_eligible``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import duckdb

from trainer_hightier.config import (
    TXN_L0_CLEANED_ROOT,
    TXN_L0_CLEANING_POLICY_ID,
    TXN_L0_EVENT_TIME_COLUMN,
    TXN_L0_LOGICAL_KEY_COLUMN,
    TXN_L0_OBSERVED_AT_COLUMN,
    TXN_L0_PARTIAL_MAX_SHARD_COUNT,
    TXN_L0_PARTIAL_MIN_POST_DEDUP_ROWS,
    TXN_L0_PARTIAL_ROW_RATIO_VS_MEDIAN,
    TXN_L0_REGISTRY_TABLE_KEY,
    TXN_L0_SOURCE_CONTRACT_REF,
    DuckDbRuntimeConfig,
)
from trainer_hightier.preprocess_bet_fix_registry import (
    bundled_preprocess_registry_yaml_path,
    duckdb_txn_episode_coverage_sql,
    load_preprocess_txn_ingestion_fix_registry,
    resolve_txn_ingest_fix001_cap_binding,
    txn_bulk_episode_match_sqls,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
from trainer_hightier.utils.txn_l0_schema import (
    TXN_L0_SCHEMA_CONTRACT_ID,
    TXN_L0_SCHEMA_DICTIONARY_REF,
    TXN_L0_SCHEMA_DDL_REF,
    canonical_raw_select_list,
    txn_l0_schema_fingerprint_sha256_hex,
)

logger = logging.getLogger("trainer_hightier")

_PARTITION_DIR_RE: Final[re.Pattern[str]] = re.compile(r"^partition_\d{6}$")
_CODE_VERSION: Final[str] = "txn_l0_preprocess_v2"


class TxnL0PreflightHardFailError(Exception):
    """Raised when raw observed-before-event rows are not covered by registry episodes."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


@dataclass(frozen=True)
class TxnL0PreprocessConfig:
    """Runtime options for one partition materialization."""

    preprocess_registry_yaml: Path
    duckdb: DuckDbRuntimeConfig = DuckDbRuntimeConfig()
    cleaning_policy_id: str = TXN_L0_CLEANING_POLICY_ID
    source_contract_ref: str = TXN_L0_SOURCE_CONTRACT_REF


def default_preprocess_registry_yaml_path() -> Path:
    """Bundled ``preprocess_l0_data_contract_registry.yaml``."""

    return bundled_preprocess_registry_yaml_path()


def _path_posix(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/")


def _path_esc(path: Path) -> str:
    return _path_posix(path).replace("'", "''")


def validate_raw_partition_dir(raw_partition: Path) -> Path:
    """Validate ``partition_YYYYMM/`` layout and return resolved path."""

    p = Path(raw_partition).resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"raw partition directory not found: {p}")
    if not _PARTITION_DIR_RE.match(p.name):
        raise ValueError(
            f"raw partition directory name must match partition_YYYYMM, got {p.name!r}"
        )
    parts = sorted(p.glob("part_*.parquet"))
    if not parts:
        raise FileNotFoundError(f"No part_*.parquet under raw partition: {p}")
    return p


def resolve_raw_partition_read_sql(raw_partition: Path) -> str:
    """Return DuckDB ``read_parquet`` clause for a validated partition directory."""

    p = validate_raw_partition_dir(raw_partition)
    glob_path = _path_esc(p / "part_*.parquet")
    return f"read_parquet('{glob_path}', union_by_name=true)"


def _fingerprint_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint_raw_partition(raw_partition: Path) -> str:
    """Hash sorted ``part_*.parquet`` paths and file digests."""

    p = validate_raw_partition_dir(raw_partition)
    parts = sorted(p.glob("part_*.parquet"))
    h = hashlib.sha256()
    for part in parts:
        h.update(part.name.encode("utf-8"))
        h.update(_fingerprint_file(part).encode("utf-8"))
    return h.hexdigest()


def count_raw_partition_shards(raw_partition: Path) -> int:
    """Return number of ``part_*.parquet`` shards under a validated partition."""

    return len(sorted(validate_raw_partition_dir(raw_partition).glob("part_*.parquet")))


def _sibling_partition_baselines(
    cleaned_root: Path,
    *,
    exclude_partition_name: str,
) -> tuple[float | None, float | None]:
    """Return ``(median_post_dedup_rows, median_shard_count)`` from sibling sidecars."""

    root = Path(cleaned_root).resolve()
    post_values: list[float] = []
    shard_values: list[float] = []
    for part_dir in sorted(root.glob("partition_*")):
        if not part_dir.is_dir() or part_dir.name == exclude_partition_name:
            continue
        report_path = part_dir / "txn_l0_materialization_report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        counts = report.get("counts")
        coverage = report.get("partition_coverage")
        if isinstance(counts, dict) and counts.get("post_dedup_rows") is not None:
            post_values.append(float(counts["post_dedup_rows"]))
        if isinstance(coverage, dict) and coverage.get("shard_count") is not None:
            shard_values.append(float(coverage["shard_count"]))
    post_median = float(sorted(post_values)[len(post_values) // 2]) if post_values else None
    shard_median = float(sorted(shard_values)[len(shard_values) // 2]) if shard_values else None
    return post_median, shard_median


def assess_partial_partition(
    raw_partition: Path,
    *,
    raw_rows: int,
    post_dedup_rows: int,
    cleaned_root: Path = TXN_L0_CLEANED_ROOT,
    min_post_dedup_rows: int = TXN_L0_PARTIAL_MIN_POST_DEDUP_ROWS,
    row_ratio_vs_median: float = TXN_L0_PARTIAL_ROW_RATIO_VS_MEDIAN,
    max_shard_count: int = TXN_L0_PARTIAL_MAX_SHARD_COUNT,
) -> dict[str, Any]:
    """Flag audit-boundary partitions that look incomplete vs sibling months."""

    partition_name = validate_raw_partition_dir(raw_partition).name
    shard_count = count_raw_partition_shards(raw_partition)
    sibling_post_median, sibling_shard_median = _sibling_partition_baselines(
        cleaned_root,
        exclude_partition_name=partition_name,
    )
    reasons: list[str] = []
    row_ratio: float | None = None
    if int(post_dedup_rows) < int(min_post_dedup_rows):
        reasons.append("post_dedup_rows_below_absolute_floor")
    if sibling_post_median is not None and sibling_post_median > 0:
        row_ratio = float(post_dedup_rows) / float(sibling_post_median)
        if row_ratio < float(row_ratio_vs_median):
            reasons.append("post_dedup_rows_below_sibling_median_ratio")
    if int(shard_count) <= int(max_shard_count):
        reasons.append("shard_count_at_or_below_partial_threshold")
    return {
        "partition_name": partition_name,
        "is_partial_partition": bool(reasons),
        "partial_partition_reasons": reasons,
        "shard_count": int(shard_count),
        "raw_rows": int(raw_rows),
        "post_dedup_rows": int(post_dedup_rows),
        "sibling_median_post_dedup_rows": sibling_post_median,
        "sibling_median_shard_count": sibling_shard_median,
        "row_ratio_vs_sibling_median": row_ratio,
        "thresholds": {
            "min_post_dedup_rows_absolute": int(min_post_dedup_rows),
            "row_ratio_vs_sibling_median_threshold": float(row_ratio_vs_median),
            "max_shard_count_for_complete_month": int(max_shard_count),
        },
    }


def _registry_stat_dict(path: Path) -> dict[str, Any]:
    """Small stat block for JSON cache fingerprints."""

    p = Path(path).resolve()
    st = p.stat()
    return {"path": str(p), "mtime_ns": int(st.st_mtime_ns), "size_bytes": int(st.st_size)}


def _txn_l0_preprocess_py_sha256() -> str:
    """SHA-256 of this module (txn L0 pipeline only)."""

    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def build_txn_l0_clean_cache_record(
    raw_partition: Path,
    *,
    preprocess_registry_yaml: Path,
    cleaning_policy_id: str = TXN_L0_CLEANING_POLICY_ID,
) -> dict[str, Any]:
    """Fingerprint txn L0 cleaned cache identity (raw shards + correction policy).

    Cache key binds content-addressed raw partition fingerprint and registry-driven
    ingest cap / applied fix rules so correction policy changes force a cache miss.
    """

    reg = Path(preprocess_registry_yaml).resolve()
    if not reg.is_file():
        raise FileNotFoundError(f"Ingest registry YAML not found: {reg}")
    raw_fp = fingerprint_raw_partition(raw_partition)
    _, cap, fix_id, fix_ver, applied = _load_txn_registry(
        TxnL0PreprocessConfig(preprocess_registry_yaml=reg)
    )
    rec: dict[str, Any] = {
        "manifest_kind": "txn_l0_clean_cache_v1",
        "code_version": _CODE_VERSION,
        "cleaning_policy_id": cleaning_policy_id,
        "txn_l0_preprocess_py_sha256": _txn_l0_preprocess_py_sha256(),
        "txn_ingest_cap_sec": int(cap),
        "applied_registry_fix_rules": applied,
        "fix_rule_id": fix_id,
        "fix_rule_version": fix_ver,
        "preprocess_registry": _registry_stat_dict(reg),
        "raw_partition": _path_posix(validate_raw_partition_dir(raw_partition)),
        "raw_partition_fingerprint_sha256_hex": raw_fp,
        "schema_contract_id": TXN_L0_SCHEMA_CONTRACT_ID,
        "schema_fingerprint_sha256_hex": txn_l0_schema_fingerprint_sha256_hex(),
        "not_model_eligible": True,
    }
    rec["fingerprint_sha256_hex"] = hashlib.sha256(
        json.dumps(rec, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return rec


def _txn_logical_observed_sql(*, cap_sec: int) -> str:
    evt = TXN_L0_EVENT_TIME_COLUMN
    obs = TXN_L0_OBSERVED_AT_COLUMN
    return f"""GREATEST(
  LEAST(
    TRY_CAST("{obs}" AS TIMESTAMPTZ),
    TRY_CAST("{evt}" AS TIMESTAMPTZ) + INTERVAL {int(cap_sec)} SECOND
  ),
  TRY_CAST("{evt}" AS TIMESTAMPTZ)
)"""


def _load_txn_registry(cfg: TxnL0PreprocessConfig) -> tuple[dict[str, Any], int, str, str, list[str]]:
    doc = load_preprocess_txn_ingestion_fix_registry(cfg.preprocess_registry_yaml)
    cap, fix_id, fix_ver, applied = resolve_txn_ingest_fix001_cap_binding(doc)
    return doc, int(cap), fix_id, fix_ver, list(applied)


def run_txn_l0_preflight(
    con: duckdb.DuckDBPyConnection,
    *,
    raw_read: str,
    cfg: TxnL0PreprocessConfig,
) -> dict[str, Any]:
    """Scan raw partition for uncovered observed-before-event rows."""

    doc, cap, fix_id, fix_ver, applied = _load_txn_registry(cfg)
    episodes = txn_bulk_episode_match_sqls(doc)
    coverage = duckdb_txn_episode_coverage_sql(episodes)
    sql = f"""
WITH src AS (
  SELECT * FROM {raw_read}
),
violations AS (
  SELECT
    COUNT(*)::BIGINT AS total_observed_before_event,
    COUNT(*) FILTER (WHERE ({coverage}))::BIGINT AS covered_observed_before_event,
    COUNT(*) FILTER (WHERE NOT ({coverage}))::BIGINT AS uncovered_observed_before_event
  FROM src
  WHERE TRY_CAST("{TXN_L0_OBSERVED_AT_COLUMN}" AS TIMESTAMPTZ) IS NOT NULL
    AND TRY_CAST("{TXN_L0_EVENT_TIME_COLUMN}" AS TIMESTAMPTZ) IS NOT NULL
    AND TRY_CAST("{TXN_L0_OBSERVED_AT_COLUMN}" AS TIMESTAMPTZ)
        < TRY_CAST("{TXN_L0_EVENT_TIME_COLUMN}" AS TIMESTAMPTZ)
)
SELECT * FROM violations
""".strip()
    row = con.execute(sql).fetchone()
    total = int(row[0]) if row else 0
    covered = int(row[1]) if row else 0
    uncovered = int(row[2]) if row else 0
    evidence: dict[str, Any] = {
        "preflight_status": "pass" if uncovered == 0 else "hard_fail",
        "total_observed_before_event_rows": total,
        "covered_observed_before_event_rows": covered,
        "uncovered_observed_before_event_rows": uncovered,
        "registered_episodes": [eid for eid, _ in episodes],
        "ingest_delay_cap_sec": cap,
        "applied_fix_rules": applied,
        "fix_rule_id": fix_id,
        "fix_rule_version": fix_ver,
        "registry_table": TXN_L0_REGISTRY_TABLE_KEY,
    }
    if uncovered > 0:
        raise TxnL0PreflightHardFailError(
            f"txn L0 preflight failed: {uncovered} row(s) with "
            f"{TXN_L0_OBSERVED_AT_COLUMN} < {TXN_L0_EVENT_TIME_COLUMN} "
            "not covered by registered bulk episodes",
            evidence,
        )
    return evidence


def _txn_materialize_select_sql(
    *,
    raw_read: str,
    cap_sec: int,
    fix_id: str,
    fix_ver: str,
) -> str:
    logical = _txn_logical_observed_sql(cap_sec=cap_sec)
    rule_label = f"{fix_id}:{fix_ver}"
    raw_canon = canonical_raw_select_list("d")
    return f"""
WITH raw AS (
  SELECT * FROM {raw_read}
),
ranked AS (
  SELECT
    t.*,
    MAX(CASE WHEN t.__op = 'd' OR t.__deleted = 'True' THEN 1 ELSE 0 END)
      OVER (PARTITION BY t.{TXN_L0_LOGICAL_KEY_COLUMN}) AS _has_delete,
    ROW_NUMBER() OVER (
      PARTITION BY t.{TXN_L0_LOGICAL_KEY_COLUMN}
      ORDER BY t.{TXN_L0_OBSERVED_AT_COLUMN} DESC NULLS LAST,
               t.updated_dtm DESC NULLS LAST
    ) AS _rn
  FROM raw AS t
  WHERE TRY_CAST(t.{TXN_L0_LOGICAL_KEY_COLUMN} AS BIGINT) IS NOT NULL
    AND TRY_CAST(t.{TXN_L0_EVENT_TIME_COLUMN} AS TIMESTAMPTZ) IS NOT NULL
    AND TRY_CAST(t.{TXN_L0_OBSERVED_AT_COLUMN} AS TIMESTAMPTZ) IS NOT NULL
),
deduped AS (
  SELECT * EXCLUDE (_has_delete, _rn)
  FROM ranked
  WHERE _rn = 1 AND _has_delete = 0
),
materialized AS (
  SELECT
    {raw_canon},
    TRY_CAST(d.{TXN_L0_EVENT_TIME_COLUMN} AS TIMESTAMPTZ) AS txn_event_ts,
    TRY_CAST(d.{TXN_L0_OBSERVED_AT_COLUMN} AS TIMESTAMPTZ) AS txn_observed_at_raw,
    {logical} AS txn_available_ts,
    CASE
      WHEN TRY_CAST(d.{TXN_L0_OBSERVED_AT_COLUMN} AS TIMESTAMPTZ) IS NULL THEN NULL
      WHEN {logical} = TRY_CAST(d.{TXN_L0_OBSERVED_AT_COLUMN} AS TIMESTAMPTZ) THEN NULL
      ELSE '{rule_label.replace("'", "''")}'
    END AS observed_at_correction_rule_id,
    CASE
      WHEN TRY_CAST(d.txn_value AS DECIMAL(19,4)) IS NULL
           OR TRY_CAST(d.txn_value AS DECIMAL(19,4)) <= 0
      THEN TRUE ELSE FALSE
    END AS is_suspicious_non_positive_txn_value,
    CASE
      WHEN TRY_CAST(d.{TXN_L0_OBSERVED_AT_COLUMN} AS TIMESTAMPTZ)
           < TRY_CAST(d.{TXN_L0_EVENT_TIME_COLUMN} AS TIMESTAMPTZ)
      THEN TRUE ELSE FALSE
    END AS is_suspicious_observed_before_event_raw
  FROM deduped AS d
)
SELECT *
FROM materialized
WHERE txn_available_ts >= txn_event_ts
""".strip()


def _aggregate_sidecar_counts(
    con: duckdb.DuckDBPyConnection,
    *,
    raw_read: str,
    cleaned_table: str,
) -> dict[str, Any]:
    raw_n = int(con.execute(f"SELECT COUNT(*)::BIGINT FROM {raw_read}").fetchone()[0])
    post = int(con.execute(f"SELECT COUNT(*)::BIGINT FROM {cleaned_table}").fetchone()[0])
    hard_excl = raw_n - int(
        con.execute(
            f"""
SELECT COUNT(*)::BIGINT FROM {raw_read}
WHERE TRY_CAST({TXN_L0_LOGICAL_KEY_COLUMN} AS BIGINT) IS NOT NULL
  AND TRY_CAST({TXN_L0_EVENT_TIME_COLUMN} AS TIMESTAMPTZ) IS NOT NULL
  AND TRY_CAST({TXN_L0_OBSERVED_AT_COLUMN} AS TIMESTAMPTZ) IS NOT NULL
""".strip()
        ).fetchone()[0]
    )
    type_hist = con.execute(
        f"""
SELECT UPPER(TRIM(CAST(type AS VARCHAR))) AS type, COUNT(*)::BIGINT AS n
FROM {cleaned_table}
GROUP BY 1 ORDER BY n DESC
""".strip()
    ).fetchdf()
    delay = con.execute(
        f"""
SELECT
  quantile_cont(
    epoch(txn_available_ts) - epoch(txn_event_ts), 0.5
  ) AS delay_p50_sec,
  quantile_cont(
    epoch(txn_available_ts) - epoch(txn_event_ts), 0.95
  ) AS delay_p95_sec
FROM {cleaned_table}
""".strip()
    ).fetchone()
    correction = con.execute(
        f"""
SELECT
  COUNT(*) FILTER (WHERE is_suspicious_observed_before_event_raw)::BIGINT
    AS raw_observed_before_event_rows,
  COUNT(*) FILTER (
    WHERE observed_at_correction_rule_id IS NOT NULL
  )::BIGINT AS covered_correction_rows
FROM {cleaned_table}
""".strip()
    ).fetchone()
    return {
        "raw_rows": raw_n,
        "post_dedup_rows": post,
        "hard_excluded_rows": hard_excl,
        "raw_observed_before_event_rows": int(correction[0]) if correction else 0,
        "covered_correction_rows": int(correction[1]) if correction else 0,
        "uncovered_correction_rows": 0,
        "type_histogram": type_hist.to_dict(orient="records"),
        "delay_p50_sec": float(delay[0]) if delay and delay[0] is not None else None,
        "delay_p95_sec": float(delay[1]) if delay and delay[1] is not None else None,
    }


def materialize_txn_l0_partition(
    raw_partition: Path,
    output_dir: Path,
    *,
    cfg: TxnL0PreprocessConfig | None = None,
) -> Path:
    """Run preflight, materialize cleaned parquet, and write sidecar JSON."""

    cfg = cfg or TxnL0PreprocessConfig(
        preprocess_registry_yaml=default_preprocess_registry_yaml_path(),
    )
    raw_partition = validate_raw_partition_dir(raw_partition)
    raw_read = resolve_raw_partition_read_sql(raw_partition)
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    _, cap, fix_id, fix_ver, applied = _load_txn_registry(cfg)
    raw_fp = fingerprint_raw_partition(raw_partition)
    cleaned_path = out / "cleaned.parquet"

    con = duckdb.connect(database=":memory:")
    preflight: dict[str, Any]
    counts: dict[str, Any]
    try:
        apply_duckdb_runtime_pragmas(con, cfg.duckdb)
        try:
            preflight = run_txn_l0_preflight(con, raw_read=raw_read, cfg=cfg)
        except TxnL0PreflightHardFailError as exc:
            evidence_path = out / "txn_l0_preflight_evidence.json"
            payload = {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "raw_partition": _path_posix(raw_partition),
                "raw_partition_fingerprint": raw_fp,
                "not_model_eligible": True,
                **exc.evidence,
            }
            evidence_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            raise
        sql = _txn_materialize_select_sql(
            raw_read=raw_read,
            cap_sec=cap,
            fix_id=fix_id,
            fix_ver=fix_ver,
        )
        con.execute(f"CREATE OR REPLACE TEMP TABLE txn_l0_cleaned AS {sql}")
        counts = _aggregate_sidecar_counts(
            con,
            raw_read=raw_read,
            cleaned_table="txn_l0_cleaned",
        )
        partition_coverage = assess_partial_partition(
            raw_partition,
            raw_rows=int(counts["raw_rows"]),
            post_dedup_rows=int(counts["post_dedup_rows"]),
            cleaned_root=TXN_L0_CLEANED_ROOT,
        )
        tmp_dir = Path(tempfile.mkdtemp(prefix="txn_l0_"))
        try:
            tmp_parquet = tmp_dir / "cleaned.parquet"
            con.execute(
                f"COPY txn_l0_cleaned TO '{_path_esc(tmp_parquet)}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            if cleaned_path.exists():
                cleaned_path.unlink()
            shutil.copy2(tmp_parquet, cleaned_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    finally:
        con.close()

    now = datetime.now(timezone.utc).isoformat()
    materialization_report = {
        "generated_at_utc": now,
        "code_version": _CODE_VERSION,
        "cleaning_policy_id": cfg.cleaning_policy_id,
        "source_contract_ref": cfg.source_contract_ref,
        "schema_contract_id": TXN_L0_SCHEMA_CONTRACT_ID,
        "schema_dictionary_ref": TXN_L0_SCHEMA_DICTIONARY_REF,
        "schema_ddl_ref": TXN_L0_SCHEMA_DDL_REF,
        "schema_fingerprint_sha256_hex": txn_l0_schema_fingerprint_sha256_hex(),
        "raw_partition": _path_posix(raw_partition),
        "raw_partition_fingerprint": raw_fp,
        "output_cleaned_parquet": _path_posix(cleaned_path),
        "preflight": preflight,
        "partition_coverage": partition_coverage,
        "applied_fix_rules": applied,
        "counts": counts,
        "not_model_eligible": True,
    }
    cache_record = build_txn_l0_clean_cache_record(
        raw_partition,
        preprocess_registry_yaml=cfg.preprocess_registry_yaml,
        cleaning_policy_id=cfg.cleaning_policy_id,
    )
    report_path = out / "txn_l0_materialization_report.json"
    meta_path = out / "source_metadata.json"
    preflight_path = out / "txn_l0_preflight_report.json"
    materialization_report["clean_cache_fingerprint_sha256_hex"] = cache_record[
        "fingerprint_sha256_hex"
    ]
    materialization_report["materialization_report_fingerprint"] = hashlib.sha256(
        json.dumps(materialization_report, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    report_path.write_text(
        json.dumps(materialization_report, indent=2, default=str),
        encoding="utf-8",
    )
    preflight_payload = {
        "generated_at_utc": now,
        "raw_partition": _path_posix(raw_partition),
        "raw_partition_fingerprint": raw_fp,
        "shard_count": partition_coverage["shard_count"],
        "not_model_eligible": True,
        **preflight,
    }
    preflight_path.write_text(
        json.dumps(preflight_payload, indent=2, default=str),
        encoding="utf-8",
    )
    source_metadata = {
        "generated_at_utc": now,
        "source_name": "t_casino_txn",
        "cleaning_policy_id": cfg.cleaning_policy_id,
        "source_contract_ref": cfg.source_contract_ref,
        "raw_partition_fingerprint": raw_fp,
        "clean_cache_fingerprint_sha256_hex": cache_record["fingerprint_sha256_hex"],
        "code_version": _CODE_VERSION,
        "not_model_eligible": True,
        "registry_table": TXN_L0_REGISTRY_TABLE_KEY,
        "schema_contract_id": TXN_L0_SCHEMA_CONTRACT_ID,
        "schema_fingerprint_sha256_hex": txn_l0_schema_fingerprint_sha256_hex(),
        "is_partial_partition": partition_coverage["is_partial_partition"],
        "partial_partition_reasons": partition_coverage["partial_partition_reasons"],
        "applied_fix_rules": applied,
    }
    meta_path.write_text(json.dumps(source_metadata, indent=2), encoding="utf-8")
    logger.info(
        "txn L0 materialized partition=%s rows=%s -> %s",
        raw_partition.name,
        counts.get("post_dedup_rows"),
        cleaned_path,
    )
    return cleaned_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize t_casino_txn L0 cleaned partition")
    parser.add_argument(
        "--raw-partition",
        type=Path,
        required=True,
        help="Path to data/t_casino_txn/partition_YYYYMM",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory (default: artifacts/cleaned/.../partition_YYYYMM)",
    )
    parser.add_argument(
        "--registry-yaml",
        type=Path,
        default=default_preprocess_registry_yaml_path(),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for one partition L0 materialization."""

    args = _parse_args(argv)
    raw_partition = validate_raw_partition_dir(args.raw_partition)
    out = args.output_dir
    if out is None:
        out = TXN_L0_CLEANED_ROOT / raw_partition.name
    cfg = TxnL0PreprocessConfig(preprocess_registry_yaml=Path(args.registry_yaml))
    materialize_txn_l0_partition(raw_partition, Path(out), cfg=cfg)


if __name__ == "__main__":
    main()
