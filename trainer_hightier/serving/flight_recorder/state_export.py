"""Export SQLite state databases into a flight recording bundle."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot

_STATE_DB_LABELS: tuple[tuple[str, str], ...] = (
    ("state_db", "state.db"),
    ("prediction_log_db", "prediction_log.db"),
    ("feature_state_db", "feature_state.db"),
)


def _sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    """List user tables in an SQLite database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [str(r[0]) for r in rows]


def _sqlite_connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open SQLite database read-only."""
    uri = f"file:{db_path.resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _export_one_db(
    recording: RecordingRoot,
    db_path: Path,
    *,
    export_label: str,
) -> None:
    """Export all tables from one SQLite DB to Parquet under ``state/``."""
    step_name = f"export_{export_label}"
    if not db_path.is_file():
        recording.append_step(step_name, "skipped", detail=f"missing {db_path}")
        return
    out_dir = recording.root / "state" / f"{export_label}_export"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with _sqlite_connect_ro(db_path) as conn:
            tables = _sqlite_tables(conn)
            if not tables:
                recording.append_step(step_name, "skipped", detail="no tables")
                return
            for table in tables:
                frame = pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
                out_path = out_dir / f"{table}.parquet"
                frame.to_parquet(out_path, index=False)
                recording.register_file(out_path, row_count=int(len(frame)))
        recording.append_step(
            step_name,
            "ok",
            detail=f"tables={len(tables)} path={db_path}",
        )
    except OSError as exc:
        recording.append_step(
            step_name,
            "error",
            error=f"{type(exc).__name__}: {exc}",
        )


def export_state_databases(
    recording: RecordingRoot,
    bundle_root: Path,
    rel: dict[str, Any],
) -> None:
    """Export state, prediction_log, and feature_state SQLite DBs."""
    local_state = bundle_root / str(rel.get("local_state_dir", "local_state"))
    for label, filename in _STATE_DB_LABELS:
        db_path = local_state / filename
        _export_one_db(recording, db_path, export_label=label)
