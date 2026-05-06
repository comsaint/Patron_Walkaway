"""DuckDB-backed materialization state for LDA-E1-09 (day-range resume)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pipelines.layered_data_assets.io.l0_fingerprint import sha256_file

MATERIALIZATION_DEFINITION_VERSION = "layered_data_assets_v1"
MATERIALIZATION_TRANSFORM_VERSION = "v1"

# Phase D: fixed-point / observability (shared by lda_l1_gate1_day_range_v1 and run_mvp)
RECOMPUTE_STOP_SINGLE_PASS = "single_pass"
RECOMPUTE_STOP_FIXED_POINT = "fixed_point"
RECOMPUTE_STOP_MAX_ROUNDS = "max_rounds"
RECOMPUTE_STOP_FALLBACK_FULL = "fallback_full"

METRIC_KEY_RECOMPUTE_ROUNDS = "recompute_rounds"
METRIC_KEY_RECOMPUTE_STOP_REASON = "recompute_stop_reason"
METRIC_KEY_ROW_FINGERPRINT_CHANGED = "row_fingerprint_changed"

ARTIFACT_PREPROCESS_BET = "preprocess_bet"
ARTIFACT_RUN_FACT = "run_fact"
ARTIFACT_RUN_BET_MAP = "run_bet_map"
ARTIFACT_RUN_DAY_BRIDGE = "run_day_bridge"
ARTIFACT_GATE1_RUN_FACT = "gate1_run_fact"
ARTIFACT_GATE1_RUN_BET_MAP = "gate1_run_bet_map"
ARTIFACT_GATE1_RUN_DAY_BRIDGE = "gate1_run_day_bridge"

_STATUS_OK = frozenset({"pending", "running", "succeeded", "failed", "skipped"})


def default_state_store_path(data_root: Path) -> Path:
    """Return default DuckDB path under ``<data_root>/l1_layered/``."""
    return (data_root / "l1_layered" / "materialization_state.duckdb").resolve()


def _stable_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize ``payload`` to canonical UTF-8 JSON bytes."""
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_input_hash(payload: Mapping[str, Any]) -> str:
    """Return ``sha256:`` hex digest of stable JSON for ``payload``."""
    h = hashlib.sha256(_stable_json_bytes(payload)).hexdigest()
    return f"sha256:{h}"


def _read_text_fingerprint(fp: Path | None) -> str | None:
    """Return raw fingerprint file text, or ``None`` if path missing."""
    if fp is None or not fp.is_file():
        return None
    return fp.read_text(encoding="utf-8")


def _file_content_sha256_uri(path: Path) -> str:
    """Return ``sha256:<hex>`` for full file contents (streaming ``sha256_file``)."""
    return f"sha256:{sha256_file(path.resolve())}"


def _normalized_preprocess_paths(paths: list[Path]) -> list[Path]:
    """Deduplicate by resolved path and sort for stable ``input_hash``."""
    uniq: dict[str, Path] = {}
    for p in paths:
        rp = p.resolve()
        uniq[rp.as_posix()] = rp
    return sorted(uniq.values(), key=lambda x: x.as_posix())


def hash_preprocess_inputs(
    *,
    source_snapshot_id: str,
    gaming_day: str,
    preprocess_input_paths: list[Path],
    fingerprint_path: Path | None,
    eligible_player_ids_parquet: Path | None = None,
    ingestion_fix_registry_path: Path | None = None,
    ingestion_fix_registry_version_expected: str | None = None,
) -> str:
    """Build content-based ``input_hash`` for preprocess (L0 parts or bet-parquet).

    Each input file contributes a ``sha256:<hex>`` over full file bytes (streaming reads via
    :func:`sha256_file`). Large Parquet inputs therefore cost one sequential pass per path per
    hash computation; prefer smaller day partitions over scanning multi-GB monoliths when possible.
    """
    if not preprocess_input_paths:
        raise ValueError("preprocess_input_paths must be non-empty")
    fp_raw = _read_text_fingerprint(fingerprint_path)
    rows: list[dict[str, str]] = []
    for p in _normalized_preprocess_paths(preprocess_input_paths):
        if not p.is_file():
            raise FileNotFoundError(f"preprocess input not found: {p}")
        rows.append(
            {
                "path": p.resolve().as_posix(),
                "content_sha256": _file_content_sha256_uri(p),
            }
        )
    payload: dict[str, Any] = {
        "artifact": ARTIFACT_PREPROCESS_BET,
        "definition_version": MATERIALIZATION_DEFINITION_VERSION,
        "transform_version": MATERIALIZATION_TRANSFORM_VERSION,
        "gaming_day": gaming_day.strip(),
        "source_snapshot_id": source_snapshot_id.strip(),
        "preprocess_inputs": rows,
        "fingerprint_json_raw": fp_raw,
    }
    if ingestion_fix_registry_path is not None:
        rp = ingestion_fix_registry_path.resolve()
        if not rp.is_file():
            raise FileNotFoundError(f"ingestion_fix_registry_path not found: {rp}")
        payload["ingestion_fix_registry_sha256"] = _file_content_sha256_uri(rp)
    if ingestion_fix_registry_version_expected is not None:
        payload["ingestion_fix_registry_version_expected"] = str(ingestion_fix_registry_version_expected).strip()
    if eligible_player_ids_parquet is not None:
        ep = eligible_player_ids_parquet.resolve()
        if not ep.is_file():
            raise FileNotFoundError(f"eligible_player_ids_parquet not found: {ep}")
        payload["eligible_player_ids_sha256"] = _file_content_sha256_uri(ep)
    return compute_input_hash(payload)


def hash_run_materialize_inputs(
    *,
    artifact_kind: str,
    source_snapshot_id: str,
    gaming_day: str,
    cleaned_parquet: Path,
    fingerprint_path: Path | None,
) -> str:
    """Build content-based ``input_hash`` for ``run_fact`` / ``run_bet_map`` / ``run_day_bridge``."""
    if artifact_kind not in (ARTIFACT_RUN_FACT, ARTIFACT_RUN_BET_MAP, ARTIFACT_RUN_DAY_BRIDGE):
        raise ValueError(f"unexpected artifact_kind for run materialize: {artifact_kind!r}")
    if not cleaned_parquet.is_file():
        raise FileNotFoundError(f"cleaned parquet not found: {cleaned_parquet}")
    fp_raw = _read_text_fingerprint(fingerprint_path)
    cleaned_uri = _file_content_sha256_uri(cleaned_parquet)
    return compute_input_hash(
        {
            "artifact": artifact_kind,
            "definition_version": MATERIALIZATION_DEFINITION_VERSION,
            "transform_version": MATERIALIZATION_TRANSFORM_VERSION,
            "gaming_day": gaming_day.strip(),
            "source_snapshot_id": source_snapshot_id.strip(),
            "cleaned_parquet_sha256": cleaned_uri,
            "fingerprint_json_raw": fp_raw,
        }
    )


def hash_gate1_inputs(
    *,
    artifact_kind: str,
    source_snapshot_id: str,
    gaming_day: str,
    gate1_output_dir: Path,
    profiles_json: str | None,
) -> str:
    """Build input_hash for Gate1 (output dir + profile set)."""
    if not re.match(r"^gate1_", artifact_kind):
        raise ValueError(f"expected gate1_* artifact_kind, got {artifact_kind!r}")
    return compute_input_hash(
        {
            "artifact": artifact_kind,
            "definition_version": MATERIALIZATION_DEFINITION_VERSION,
            "transform_version": MATERIALIZATION_TRANSFORM_VERSION,
            "gaming_day": gaming_day.strip(),
            "source_snapshot_id": source_snapshot_id.strip(),
            "gate1_output_dir": gate1_output_dir.resolve().as_posix(),
            "profiles_json": profiles_json,
        }
    )


def format_row_fingerprint_tag(*, artifact_kind: str, fp_value: str) -> str:
    """Stable string stored in ``row_hash`` for output row-fingerprint (Phase D).

    ``fp_value`` is the hex / varchar fingerprint fragment from DuckDB (see
    :mod:`pipelines.layered_data_assets.core.l1_determinism_gate_v1`).
    """
    return f"v1|{artifact_kind}|{fp_value}"


def compute_output_row_fingerprint_pair(
    con: Any,
    *,
    parquet_path: Path,
    fingerprint_artifact_kind: str,
) -> tuple[int, str]:
    """Return ``(row_count, raw_fp_fragment)`` for one materialized Parquet output.

    ``fingerprint_artifact_kind`` must be one of the preprocess / run_* artifact
    kinds that have a dedicated fingerprint SQL in ``l1_determinism_gate_v1``.
    """
    from pipelines.layered_data_assets.core.l1_determinism_gate_v1 import (
        cleaned_bet_parquet_row_fingerprint,
        run_bet_map_parquet_row_fingerprint,
        run_day_bridge_parquet_row_fingerprint,
        run_fact_parquet_row_fingerprint,
    )

    p = Path(parquet_path)
    if not p.is_file():
        raise FileNotFoundError(f"parquet not found for fingerprint: {p}")
    if fingerprint_artifact_kind == ARTIFACT_PREPROCESS_BET:
        return cleaned_bet_parquet_row_fingerprint(con, p)
    if fingerprint_artifact_kind == ARTIFACT_RUN_FACT:
        return run_fact_parquet_row_fingerprint(con, p)
    if fingerprint_artifact_kind == ARTIFACT_RUN_BET_MAP:
        return run_bet_map_parquet_row_fingerprint(con, p)
    if fingerprint_artifact_kind == ARTIFACT_RUN_DAY_BRIDGE:
        return run_day_bridge_parquet_row_fingerprint(con, p)
    raise ValueError(
        f"unsupported fingerprint_artifact_kind for row fingerprint: {fingerprint_artifact_kind!r}"
    )


def format_impact_metrics_json(
    *,
    recompute_rounds: int,
    recompute_stop_reason: str,
    row_fingerprint_changed: bool | None = None,
) -> str:
    """One-line JSON for stderr logs / MVP summaries (Phase D PR-D4)."""
    payload = {
        METRIC_KEY_RECOMPUTE_ROUNDS: int(recompute_rounds),
        METRIC_KEY_RECOMPUTE_STOP_REASON: str(recompute_stop_reason),
        METRIC_KEY_ROW_FINGERPRINT_CHANGED: row_fingerprint_changed,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _materialization_state_schema_path() -> Path:
    """Walk ancestors of this file until ``schema/materialization_state.schema.sql`` exists.

    Returns:
        Absolute path to the SQL DDL file.

    Raises:
        FileNotFoundError: If no ancestor contains the expected schema file.
    """
    here = Path(__file__).resolve()
    for anc in here.parents:
        candidate = anc / "schema" / "materialization_state.schema.sql"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "schema/materialization_state.schema.sql not found when walking parents from "
        f"{here}"
    )


def _migrate_materialization_state_extra_columns(con: Any) -> None:
    """Add Phase D columns to existing DuckDB tables (CREATE IF NOT EXISTS is not enough)."""
    stmts = (
        "ALTER TABLE materialization_state ADD COLUMN IF NOT EXISTS impacted_entity_count BIGINT;",
        "ALTER TABLE materialization_state ADD COLUMN IF NOT EXISTS impacted_day_count BIGINT;",
        "ALTER TABLE materialization_state ADD COLUMN IF NOT EXISTS recompute_rounds INTEGER;",
        "ALTER TABLE materialization_state ADD COLUMN IF NOT EXISTS recompute_stop_reason VARCHAR;",
    )
    for stmt in stmts:
        con.execute(stmt)


def ensure_materialization_state_schema(con: Any) -> None:
    """Create ``materialization_state`` table if missing (idempotent)."""
    sql = _materialization_state_schema_path().read_text(encoding="utf-8")
    con.execute(sql)
    _migrate_materialization_state_extra_columns(con)


def fetch_state_row(
    con: Any,
    *,
    artifact_kind: str,
    gaming_day: str,
    source_snapshot_id: str,
) -> dict[str, Any] | None:
    """Return one state row as dict, or ``None``."""
    row = con.execute(
        """
        SELECT artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version,
               input_hash, status, attempt, output_uri, row_count, row_hash, error_summary, updated_at,
               impacted_entity_count, impacted_day_count, recompute_rounds, recompute_stop_reason
        FROM materialization_state
        WHERE artifact_kind = ? AND gaming_day = ? AND source_snapshot_id = ?
          AND definition_version = ? AND transform_version = ?
        """,
        [
            artifact_kind,
            gaming_day.strip(),
            source_snapshot_id.strip(),
            MATERIALIZATION_DEFINITION_VERSION,
            MATERIALIZATION_TRANSFORM_VERSION,
        ],
    ).fetchone()
    if row is None:
        return None
    cols = [
        "artifact_kind",
        "gaming_day",
        "source_snapshot_id",
        "definition_version",
        "transform_version",
        "input_hash",
        "status",
        "attempt",
        "output_uri",
        "row_count",
        "row_hash",
        "error_summary",
        "updated_at",
        "impacted_entity_count",
        "impacted_day_count",
        "recompute_rounds",
        "recompute_stop_reason",
    ]
    return dict(zip(cols, row))


def should_skip_step(
    *,
    resume: bool,
    force: bool,
    row: Mapping[str, Any] | None,
    input_hash: str,
) -> bool:
    """Return ``True`` if this step should be skipped (resume + succeeded + same hash)."""
    if force or not resume:
        return False
    if row is None:
        return False
    if str(row.get("status")) != "succeeded":
        return False
    return str(row.get("input_hash")) == input_hash


def verify_stored_row_fingerprint_matches_output(
    con: Any,
    *,
    artifact_kind: str,
    gaming_day: str,
    source_snapshot_id: str,
    fingerprint_artifact_kind: str,
    parquet_path: Path,
) -> bool:
    """Return ``True`` if stored ``row_hash`` matches a fresh fingerprint (or legacy empty).

    Used when ``--verify-row-fingerprint-on-resume`` is enabled: drift between
    stored tag and recomputed Parquet fingerprint forces a rerun even if
    ``input_hash`` matches.
    """
    row = fetch_state_row(
        con,
        artifact_kind=artifact_kind,
        gaming_day=gaming_day,
        source_snapshot_id=source_snapshot_id,
    )
    if row is None:
        return True
    stored = row.get("row_hash")
    if stored is None or str(stored).strip() == "":
        return True
    n, fp_frag = compute_output_row_fingerprint_pair(
        con,
        parquet_path=parquet_path,
        fingerprint_artifact_kind=fingerprint_artifact_kind,
    )
    _ = n
    tag = format_row_fingerprint_tag(artifact_kind=fingerprint_artifact_kind, fp_value=str(fp_frag))
    return str(stored) == tag


def mark_step_running(con: Any, *, artifact_kind: str, gaming_day: str, source_snapshot_id: str, input_hash: str) -> int:
    """Insert or update row to ``running``; return new ``attempt`` (>=1)."""
    now = datetime.now(timezone.utc)
    prev = fetch_state_row(
        con,
        artifact_kind=artifact_kind,
        gaming_day=gaming_day,
        source_snapshot_id=source_snapshot_id,
    )
    attempt = 1 if prev is None else int(prev["attempt"]) + 1
    con.execute(
        """
        INSERT INTO materialization_state (
          artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version,
          input_hash, status, attempt, output_uri, row_count, row_hash, error_summary, updated_at,
          impacted_entity_count, impacted_day_count, recompute_rounds, recompute_stop_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version)
        DO UPDATE SET
          input_hash = excluded.input_hash,
          status = excluded.status,
          attempt = excluded.attempt,
          output_uri = NULL,
          row_count = NULL,
          row_hash = NULL,
          error_summary = NULL,
          impacted_entity_count = excluded.impacted_entity_count,
          impacted_day_count = excluded.impacted_day_count,
          recompute_rounds = excluded.recompute_rounds,
          recompute_stop_reason = excluded.recompute_stop_reason,
          updated_at = excluded.updated_at
        """,
        [
            artifact_kind,
            gaming_day.strip(),
            source_snapshot_id.strip(),
            MATERIALIZATION_DEFINITION_VERSION,
            MATERIALIZATION_TRANSFORM_VERSION,
            input_hash,
            "running",
            attempt,
            None,
            None,
            None,
            None,
            now,
            None,
            None,
            None,
            None,
        ],
    )
    return attempt


def mark_step_succeeded(
    con: Any,
    *,
    artifact_kind: str,
    gaming_day: str,
    source_snapshot_id: str,
    input_hash: str,
    attempt: int,
    output_uri: str | None,
    row_count: int | None,
    row_hash: str | None = None,
    impacted_entity_count: int | None = None,
    impacted_day_count: int | None = None,
    recompute_rounds: int | None = None,
    recompute_stop_reason: str | None = None,
) -> None:
    """Persist ``succeeded`` with optional output stats and Phase D observability."""
    now = datetime.now(timezone.utc)
    con.execute(
        """
        INSERT INTO materialization_state (
          artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version,
          input_hash, status, attempt, output_uri, row_count, row_hash, error_summary, updated_at,
          impacted_entity_count, impacted_day_count, recompute_rounds, recompute_stop_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version)
        DO UPDATE SET
          input_hash = excluded.input_hash,
          status = excluded.status,
          attempt = excluded.attempt,
          output_uri = excluded.output_uri,
          row_count = excluded.row_count,
          row_hash = excluded.row_hash,
          error_summary = NULL,
          impacted_entity_count = excluded.impacted_entity_count,
          impacted_day_count = excluded.impacted_day_count,
          recompute_rounds = excluded.recompute_rounds,
          recompute_stop_reason = excluded.recompute_stop_reason,
          updated_at = excluded.updated_at
        """,
        [
            artifact_kind,
            gaming_day.strip(),
            source_snapshot_id.strip(),
            MATERIALIZATION_DEFINITION_VERSION,
            MATERIALIZATION_TRANSFORM_VERSION,
            input_hash,
            "succeeded",
            attempt,
            output_uri,
            row_count,
            row_hash,
            None,
            now,
            impacted_entity_count,
            impacted_day_count,
            recompute_rounds,
            recompute_stop_reason,
        ],
    )


def mark_step_failed(
    con: Any,
    *,
    artifact_kind: str,
    gaming_day: str,
    source_snapshot_id: str,
    input_hash: str,
    attempt: int,
    error_summary: str,
    impacted_entity_count: int | None = None,
    impacted_day_count: int | None = None,
    recompute_rounds: int | None = None,
    recompute_stop_reason: str | None = None,
) -> None:
    """Persist ``failed`` with a short error string."""
    now = datetime.now(timezone.utc)
    msg = error_summary[:4000]
    con.execute(
        """
        INSERT INTO materialization_state (
          artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version,
          input_hash, status, attempt, output_uri, row_count, row_hash, error_summary, updated_at,
          impacted_entity_count, impacted_day_count, recompute_rounds, recompute_stop_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version)
        DO UPDATE SET
          input_hash = excluded.input_hash,
          status = excluded.status,
          attempt = excluded.attempt,
          output_uri = NULL,
          row_count = NULL,
          row_hash = NULL,
          error_summary = excluded.error_summary,
          impacted_entity_count = excluded.impacted_entity_count,
          impacted_day_count = excluded.impacted_day_count,
          recompute_rounds = excluded.recompute_rounds,
          recompute_stop_reason = excluded.recompute_stop_reason,
          updated_at = excluded.updated_at
        """,
        [
            artifact_kind,
            gaming_day.strip(),
            source_snapshot_id.strip(),
            MATERIALIZATION_DEFINITION_VERSION,
            MATERIALIZATION_TRANSFORM_VERSION,
            input_hash,
            "failed",
            attempt,
            None,
            None,
            None,
            msg,
            now,
            impacted_entity_count,
            impacted_day_count,
            recompute_rounds,
            recompute_stop_reason,
        ],
    )


def patch_run_day_bridge_recompute_metrics(
    con: Any,
    *,
    gaming_day: str,
    source_snapshot_id: str,
    recompute_rounds: int,
    recompute_stop_reason: str,
) -> None:
    """Update Phase D reconcile fields on the latest ``run_day_bridge`` row for one day."""
    now = datetime.now(timezone.utc)
    con.execute(
        """
        UPDATE materialization_state
        SET recompute_rounds = ?,
            recompute_stop_reason = ?,
            updated_at = ?
        WHERE artifact_kind = ?
          AND gaming_day = ?
          AND source_snapshot_id = ?
          AND definition_version = ?
          AND transform_version = ?
          AND status = 'succeeded'
        """,
        [
            int(recompute_rounds),
            str(recompute_stop_reason),
            now,
            ARTIFACT_RUN_DAY_BRIDGE,
            gaming_day.strip(),
            source_snapshot_id.strip(),
            MATERIALIZATION_DEFINITION_VERSION,
            MATERIALIZATION_TRANSFORM_VERSION,
        ],
    )


def parquet_row_count(con: Any, parquet_path: Path) -> int | None:
    """Return ``COUNT(*)`` for one Parquet file, or ``None`` on failure."""
    try:
        row = con.execute("SELECT COUNT(*)::BIGINT FROM read_parquet(?)", [str(parquet_path.resolve())]).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return int(row[0])
