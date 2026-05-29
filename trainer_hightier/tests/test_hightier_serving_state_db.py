"""State DB migration idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trainer_hightier.serving.contracts import NEW_ALERT_COLUMNS
from trainer_hightier.serving.state_db import init_state_db


def test_init_state_db_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    init_state_db(db)
    init_state_db(db)
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
        for name, _ in NEW_ALERT_COLUMNS:
            assert name in cols
