"""DuckDB-backed materialization state for LDA-E1-09 (day-range resume)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

MATERIALIZATION_DEFINITION_VERSION = "layered_data_assets_v1"
MATERIALIZATION_TRANSFORM_VERSION = "v1"

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


def _stat_triple(path: Path) -> list[Any]:
    """Return ``[posix_path, size, mtime_ns]`` for ``path`` (must exist)."""
    st = path.stat()
    return [path.resolve().as_posix(), int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))]


def hash_preprocess_inputs(
    *,
    source_snapshot_id: str,
    gaming_day: str,
    preprocess_input_paths: list[Path],
    fingerprint_path: Path | None,
    ingestion_fix_registry_path: Path | None = None,
    ingestion_fix_registry_version_expected: str | None = None,
) -> str:
    """Build input_hash for preprocess (L0 parts or bet-parquet)."""
    if not preprocess_input_paths:
        raise ValueError("preprocess_input_paths must be non-empty")
    stats = [_stat_triple(p) for p in preprocess_input_paths]
    fp_raw = _read_text_fingerprint(fingerprint_path)
    payload: dict[str, Any] = {
        "artifact": ARTIFACT_PREPROCESS_BET,
        "definition_version": MATERIALIZATION_DEFINITION_VERSION,
        "transform_version": MATERIALIZATION_TRANSFORM_VERSION,
        "gaming_day": gaming_day.strip(),
        "source_snapshot_id": source_snapshot_id.strip(),
        "preprocess_inputs_stats": stats,
        "fingerprint_json_raw": fp_raw,
    }
    if ingestion_fix_registry_path is not None:
        rp = ingestion_fix_registry_path.resolve()
        if not rp.is_file():
            raise FileNotFoundError(f"ingestion_fix_registry_path not found: {rp}")
        payload["ingestion_fix_registry_stats"] = _stat_triple(rp)
    if ingestion_fix_registry_version_expected is not None:
        payload["ingestion_fix_registry_version_expected"] = str(ingestion_fix_registry_version_expected).strip()
    return compute_input_hash(payload)


def hash_run_materialize_inputs(
    *,
    artifact_kind: str,
    source_snapshot_id: str,
    gaming_day: str,
    cleaned_parquet: Path,
    fingerprint_path: Path | None,
) -> str:
    """Build input_hash for ``run_fact`` / ``run_bet_map`` / ``run_day_bridge``."""
    if artifact_kind not in (ARTIFACT_RUN_FACT, ARTIFACT_RUN_BET_MAP, ARTIFACT_RUN_DAY_BRIDGE):
        raise ValueError(f"unexpected artifact_kind for run materialize: {artifact_kind!r}")
    if not cleaned_parquet.is_file():
        raise FileNotFoundError(f"cleaned parquet not found: {cleaned_parquet}")
    fp_raw = _read_text_fingerprint(fingerprint_path)
    return compute_input_hash(
        {
            "artifact": artifact_kind,
            "definition_version": MATERIALIZATION_DEFINITION_VERSION,
            "transform_version": MATERIALIZATION_TRANSFORM_VERSION,
            "gaming_day": gaming_day.strip(),
            "source_snapshot_id": source_snapshot_id.strip(),
            "cleaned_parquet_stats": _stat_triple(cleaned_parquet),
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


def ensure_materialization_state_schema(con: Any) -> None:
    """Create ``materialization_state`` table if missing (idempotent)."""
    sql = _materialization_state_schema_path().read_text(encoding="utf-8")
    con.execute(sql)


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
               input_hash, status, attempt, output_uri, row_count, row_hash, error_summary, updated_at
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
        INSERT INTO materialization_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version)
        DO UPDATE SET
          input_hash = excluded.input_hash,
          status = excluded.status,
          attempt = excluded.attempt,
          output_uri = NULL,
          row_count = NULL,
          row_hash = NULL,
          error_summary = NULL,
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
) -> None:
    """Persist ``succeeded`` with optional output stats."""
    now = datetime.now(timezone.utc)
    con.execute(
        """
        INSERT INTO materialization_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version)
        DO UPDATE SET
          input_hash = excluded.input_hash,
          status = excluded.status,
          attempt = excluded.attempt,
          output_uri = excluded.output_uri,
          row_count = excluded.row_count,
          row_hash = excluded.row_hash,
          error_summary = NULL,
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
) -> None:
    """Persist ``failed`` with a short error string."""
    now = datetime.now(timezone.utc)
    msg = error_summary[:4000]
    con.execute(
        """
        INSERT INTO materialization_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (artifact_kind, gaming_day, source_snapshot_id, definition_version, transform_version)
        DO UPDATE SET
          input_hash = excluded.input_hash,
          status = excluded.status,
          attempt = excluded.attempt,
          output_uri = NULL,
          row_count = NULL,
          row_hash = NULL,
          error_summary = excluded.error_summary,
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
