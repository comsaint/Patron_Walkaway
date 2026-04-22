"""W2: state DB runtime_rated_threshold.selection_mode column + upsert."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trainer.serving.scorer import ensure_runtime_rated_threshold_schema, upsert_runtime_rated_threshold


def test_schema_adds_selection_mode_and_upsert_persists(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        ensure_runtime_rated_threshold_schema(conn)
        upsert_runtime_rated_threshold(
            conn, 0.62, source="test", selection_mode="field_test"
        )
        conn.commit()
        row = conn.execute(
            "SELECT rated_threshold, selection_mode FROM runtime_rated_threshold WHERE id = 1"
        ).fetchone()
    assert row is not None
    assert abs(float(row[0]) - 0.62) < 1e-9
    assert row[1] == "field_test"


def test_migration_old_table_without_column_gets_alter(tmp_path: Path) -> None:
    db = tmp_path / "legacy_state.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE runtime_rated_threshold (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                rated_threshold REAL NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT,
                n_mature INTEGER,
                n_pos INTEGER,
                window_hours REAL,
                recall_at_threshold REAL,
                precision_at_threshold REAL
            )
            """
        )
        conn.commit()
    with sqlite3.connect(db) as conn:
        ensure_runtime_rated_threshold_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runtime_rated_threshold)")}
        assert "selection_mode" in cols
        upsert_runtime_rated_threshold(conn, 0.55, source="mig", selection_mode="legacy")
        conn.commit()
        sm = conn.execute(
            "SELECT selection_mode FROM runtime_rated_threshold WHERE id = 1"
        ).fetchone()[0]
    assert sm == "legacy"
