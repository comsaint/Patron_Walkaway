"""SQLite + JSON manifest for high-tier feature snapshots (independent of ``state.db``)."""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from trainer_hightier.config import default_hightier_serving_config
from trainer_hightier.serving.runtime_config import HK_TZ

logger = logging.getLogger(__name__)


def _hk_now_iso() -> str:
    return datetime.now(ZoneInfo(HK_TZ)).isoformat()


@dataclass(frozen=True)
class ActiveSnapshotManifest:
    """Pointers to on-disk Parquet artifacts for serving layers."""

    version: str
    slow_patron_parquet: Path
    trial_bet_behavior_parquet: Path | None
    adt_allowlist_parquet: Path | None
    adt_allowlist_version: str | None
    coverage_end_exclusive: str | None
    training_cutoff_iso: str | None
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ActiveSnapshotManifest:
        slow = Path(str(d.get("slow_patron_parquet", ""))).resolve()
        trial_raw = d.get("trial_bet_behavior_parquet")
        trial = Path(str(trial_raw)).resolve() if trial_raw else None
        al_raw = d.get("adt_allowlist_parquet")
        allow = Path(str(al_raw)).resolve() if al_raw else None
        ver = d.get("adt_allowlist_version")
        return cls(
            version=str(d.get("version", "")),
            slow_patron_parquet=slow,
            trial_bet_behavior_parquet=trial,
            adt_allowlist_parquet=allow,
            adt_allowlist_version=(str(ver).strip() if ver is not None and str(ver).strip() else None),
            coverage_end_exclusive=(
                str(d["coverage_end_exclusive"]) if d.get("coverage_end_exclusive") else None
            ),
            training_cutoff_iso=(str(d["training_cutoff_iso"]) if d.get("training_cutoff_iso") else None),
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
        return ActiveSnapshotManifest.from_dict(d)
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
