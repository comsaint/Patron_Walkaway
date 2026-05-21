"""SQLite + JSON manifest for high-tier feature snapshots (independent of ``state.db``)."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from trainer_hightier.config import (
    HK_TZ,
    MANIFEST_KEY_FE_SHORT_TERM,
    MANIFEST_KEY_MID_TERM_SNAPSHOT,
    default_hightier_serving_config,
)
from trainer_hightier.serving.contracts import (
    META_KEY_FEAST_READINESS_LATEST_GENERATED_AT,
    META_KEY_FEAST_READINESS_LATEST_JSON,
    META_KEY_FEAST_READINESS_LATEST_RUN_ID,
    META_KEY_FEAST_READINESS_LATEST_SHA256,
)

logger = logging.getLogger(__name__)


def _hk_now_iso() -> str:
    return datetime.now(ZoneInfo(HK_TZ)).isoformat()


@dataclass(frozen=True)
class ActiveSnapshotManifest:
    """Pointers to on-disk Parquet artifacts for serving layers."""

    version: str
    slow_patron_parquet: Path
    fe_derived_parquet: Path | None
    trial_bet_behavior_parquet: Path | None
    adt_allowlist_parquet: Path | None
    adt_allowlist_version: str | None
    coverage_end_exclusive: str | None
    training_cutoff_iso: str | None
    mid_term_snapshot_parquet: Path | None
    fe_short_term_parquet: Path | None
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, manifest_dir: Path | None = None) -> ActiveSnapshotManifest:
        def _resolve_path(raw: Any) -> Path | None:
            if raw is None:
                return None
            s = str(raw).strip()
            if not s:
                return None
            p = Path(s)
            if manifest_dir is not None and not p.is_absolute():
                return (Path(manifest_dir).resolve() / p).resolve()
            return p.expanduser().resolve()

        slow = _resolve_path(d.get("slow_patron_parquet")) or Path("")
        fe_derived = _resolve_path(d.get("fe_derived_parquet"))
        mid_term = _resolve_path(d.get(MANIFEST_KEY_MID_TERM_SNAPSHOT))
        fe_short = _resolve_path(d.get(MANIFEST_KEY_FE_SHORT_TERM)) or fe_derived
        trial = _resolve_path(d.get("trial_bet_behavior_parquet"))
        allow = _resolve_path(d.get("adt_allowlist_parquet"))
        ver = d.get("adt_allowlist_version")
        return cls(
            version=str(d.get("version", "")),
            slow_patron_parquet=slow,
            fe_derived_parquet=fe_derived,
            trial_bet_behavior_parquet=trial,
            adt_allowlist_parquet=allow,
            adt_allowlist_version=(str(ver).strip() if ver is not None and str(ver).strip() else None),
            coverage_end_exclusive=(
                str(d["coverage_end_exclusive"]) if d.get("coverage_end_exclusive") else None
            ),
            training_cutoff_iso=(str(d["training_cutoff_iso"]) if d.get("training_cutoff_iso") else None),
            mid_term_snapshot_parquet=mid_term,
            fe_short_term_parquet=fe_short,
            raw=d,
        )


def apply_feature_state_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")


def init_feature_state_db(path: Optional[Path] = None) -> Path:
    """Create feature-state tables if absent."""
    cfg = default_hightier_serving_config()
    db_path = Path(path or cfg.feature_state_db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feature_state_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_watermark (
                layer TEXT PRIMARY KEY,
                coverage_end_exclusive TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_job_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                detail TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS adt_allowlist_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                path TEXT NOT NULL,
                version TEXT,
                sha256_hex TEXT,
                row_count INTEGER,
                generated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feast_refresh_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                layers TEXT NOT NULL,
                feast_repo TEXT,
                readiness_path TEXT,
                apply_seconds REAL,
                materialize_seconds REAL,
                summary_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feast_refresh_layer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                layer TEXT NOT NULL,
                artifact_path TEXT,
                row_count INTEGER,
                anchor_gaming_day_max TEXT,
                source_scope TEXT,
                feature_view TEXT,
                export_rows INTEGER,
                export_seconds REAL,
                compute_seconds REAL,
                smoke_sample_size INTEGER,
                smoke_entity_present_rate REAL,
                status TEXT NOT NULL,
                detail_json TEXT,
                UNIQUE(run_id, layer)
            )
            """
        )
        conn.commit()
    return db_path


def manifest_dir() -> Path:
    cfg = default_hightier_serving_config()
    d = Path(cfg.snapshot_manifest_dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_manifest_path() -> Path:
    return manifest_dir() / "active_manifest.json"


def read_active_manifest() -> ActiveSnapshotManifest | None:
    p = active_manifest_path()
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("[feature_state] failed to read manifest %s: %s", p, exc)
        return None
    if not isinstance(d, dict):
        return None
    try:
        return ActiveSnapshotManifest.from_dict(d, manifest_dir=p.parent)
    except Exception as exc:
        logger.warning("[feature_state] invalid manifest payload %s: %s", p, exc)
        return None


def publish_manifest_atomic(payload: dict[str, Any]) -> Path:
    """Write ``active_manifest.json`` via temp+replace."""
    final = active_manifest_path()
    final.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, default=str)
    fd, tmp = tempfile.mkstemp(prefix="manifest_", suffix=".json", dir=str(final.parent))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(body)
        Path(tmp).replace(final)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    logger.info("[feature_state] published manifest %s", final)
    return final


def feature_state_meta_get(key: str, *, path: Optional[Path] = None) -> str | None:
    """Read one key from ``feature_state_meta``."""

    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        row = conn.execute(
            "SELECT value FROM feature_state_meta WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    val = row[0]
    return str(val) if val is not None else None


def feature_state_meta_set(key: str, value: str, *, path: Optional[Path] = None) -> None:
    """Upsert one key in ``feature_state_meta``."""

    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            INSERT INTO feature_state_meta(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()


def log_job_start(
    run_id: str,
    *,
    status: str = "running",
    detail: str = "",
    path: Optional[Path] = None,
) -> None:
    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            INSERT INTO snapshot_job_log(run_id, started_at, finished_at, status, detail)
            VALUES (?, ?, NULL, ?, ?)
            """,
            (run_id, _hk_now_iso(), status, detail[:2000]),
        )
        conn.commit()


def log_job_finish(
    run_id: str,
    *,
    status: str,
    detail: str = "",
    path: Optional[Path] = None,
) -> None:
    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            UPDATE snapshot_job_log
            SET finished_at = ?, status = ?, detail = ?
            WHERE id = (
                SELECT id FROM snapshot_job_log WHERE run_id = ? ORDER BY id DESC LIMIT 1
            )
            """,
            (_hk_now_iso(), status, detail[:2000], run_id),
        )
        conn.commit()


def update_watermark(layer: str, coverage_end_exclusive: str, *, path: Optional[Path] = None) -> None:
    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            INSERT INTO snapshot_watermark(layer, coverage_end_exclusive, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(layer) DO UPDATE SET
                coverage_end_exclusive=excluded.coverage_end_exclusive,
                updated_at=excluded.updated_at
            """,
            (layer, coverage_end_exclusive, _hk_now_iso()),
        )
        conn.commit()


def feast_refresh_run_start(
    run_id: str,
    *,
    source: str,
    layers: str,
    feast_repo: str | None = None,
    readiness_path: str | None = None,
    path: Optional[Path] = None,
) -> None:
    """Insert a ``feast_refresh_run`` row with ``status=running``."""
    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            INSERT INTO feast_refresh_run(
                run_id, started_at, finished_at, status, source, layers,
                feast_repo, readiness_path, apply_seconds, materialize_seconds, summary_json
            )
            VALUES (?, ?, NULL, 'running', ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            (run_id, _hk_now_iso(), source, layers, feast_repo, readiness_path),
        )
        conn.commit()


def feast_refresh_run_finish(
    run_id: str,
    *,
    status: str,
    apply_seconds: float | None = None,
    materialize_seconds: float | None = None,
    summary_json: str | None = None,
    path: Optional[Path] = None,
) -> None:
    """Mark a Feast refresh run finished."""
    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            UPDATE feast_refresh_run
            SET finished_at = ?, status = ?, apply_seconds = ?,
                materialize_seconds = ?, summary_json = ?
            WHERE run_id = ?
            """,
            (
                _hk_now_iso(),
                status,
                apply_seconds,
                materialize_seconds,
                (summary_json or "")[:8000] if summary_json else None,
                run_id,
            ),
        )
        conn.commit()


def upsert_feast_refresh_layer(
    run_id: str,
    *,
    layer: str,
    status: str,
    artifact_path: str | None = None,
    row_count: int | None = None,
    anchor_gaming_day_max: str | None = None,
    source_scope: str | None = None,
    feature_view: str | None = None,
    export_rows: int | None = None,
    export_seconds: float | None = None,
    compute_seconds: float | None = None,
    smoke_sample_size: int | None = None,
    smoke_entity_present_rate: float | None = None,
    detail_json: str | None = None,
    path: Optional[Path] = None,
) -> None:
    """Upsert one layer outcome for a Feast refresh run."""
    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            INSERT INTO feast_refresh_layer(
                run_id, layer, artifact_path, row_count, anchor_gaming_day_max,
                source_scope, feature_view, export_rows, export_seconds, compute_seconds,
                smoke_sample_size, smoke_entity_present_rate, status, detail_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, layer) DO UPDATE SET
                artifact_path=excluded.artifact_path,
                row_count=excluded.row_count,
                anchor_gaming_day_max=excluded.anchor_gaming_day_max,
                source_scope=excluded.source_scope,
                feature_view=excluded.feature_view,
                export_rows=excluded.export_rows,
                export_seconds=excluded.export_seconds,
                compute_seconds=excluded.compute_seconds,
                smoke_sample_size=excluded.smoke_sample_size,
                smoke_entity_present_rate=excluded.smoke_entity_present_rate,
                status=excluded.status,
                detail_json=excluded.detail_json
            """,
            (
                run_id,
                layer,
                artifact_path,
                row_count,
                anchor_gaming_day_max,
                source_scope,
                feature_view,
                export_rows,
                export_seconds,
                compute_seconds,
                smoke_sample_size,
                smoke_entity_present_rate,
                status,
                (detail_json or "")[:8000] if detail_json else None,
            ),
        )
        conn.commit()


def persist_feast_online_readiness_latest(
    run_id: str,
    readiness_payload: dict[str, Any],
    *,
    path: Optional[Path] = None,
) -> str:
    """Persist latest Feast readiness document and sha256 in ``feature_state_meta``."""
    body = json.dumps(readiness_payload, sort_keys=True, default=str)
    sha256_hex = hashlib.sha256(body.encode("utf-8")).hexdigest()
    generated_at = str(readiness_payload.get("generated_at") or "")
    feature_state_meta_set(META_KEY_FEAST_READINESS_LATEST_JSON, body, path=path)
    feature_state_meta_set(META_KEY_FEAST_READINESS_LATEST_SHA256, sha256_hex, path=path)
    feature_state_meta_set(META_KEY_FEAST_READINESS_LATEST_RUN_ID, str(run_id), path=path)
    feature_state_meta_set(META_KEY_FEAST_READINESS_LATEST_GENERATED_AT, generated_at, path=path)
    return sha256_hex


def upsert_adt_allowlist_meta(
    *,
    artifact_path: Path,
    version: str,
    sha256_hex: str,
    row_count: int,
    path: Optional[Path] = None,
) -> None:
    """Persist active ADT allowlist artifact audit row (single-row table, ``id=1``)."""
    init_feature_state_db(path)
    db_path = Path(path or default_hightier_serving_config().feature_state_db_path).resolve()
    with sqlite3.connect(db_path) as conn:
        apply_feature_state_pragmas(conn)
        conn.execute(
            """
            INSERT INTO adt_allowlist_meta(id, path, version, sha256_hex, row_count, generated_at)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                path=excluded.path,
                version=excluded.version,
                sha256_hex=excluded.sha256_hex,
                row_count=excluded.row_count,
                generated_at=excluded.generated_at
            """,
            (
                str(Path(artifact_path).resolve()),
                version,
                sha256_hex,
                int(row_count),
                _hk_now_iso(),
            ),
        )
        conn.commit()
