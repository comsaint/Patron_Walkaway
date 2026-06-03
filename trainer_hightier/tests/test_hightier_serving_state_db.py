"""State DB migration idempotency."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.serving.contracts import ALERTS_MIGRATION_COLUMNS
from trainer_hightier.serving.state_db import append_alerts, init_state_db


def test_init_state_db_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    init_state_db(db)
    init_state_db(db)
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
        for name, _ in ALERTS_MIGRATION_COLUMNS:
            assert name in cols


def test_append_alerts_persists_player_game_metadata(tmp_path: Path) -> None:
    """Alerts upsert stores game_id and player-game aggregation metadata."""

    db = tmp_path / "state.db"
    init_state_db(db)
    alerts = pd.DataFrame(
        [
            {
                "bet_id": "1",
                "ts": "2025-06-01T10:00:00+08:00",
                "bet_ts": "2025-06-01T10:00:00+08:00",
                "player_id": 10,
                "casino_player_id": None,
                "table_id": 1,
                "position_idx": 1,
                "visit_start_ts": None,
                "visit_end_ts": None,
                "session_count": None,
                "bet_count": 3,
                "visit_avg_bet": None,
                "historical_avg_bet": None,
                "score": 0.9,
                "session_id": "s1",
                "loss_streak": 0,
                "bets_last_5m": 0.0,
                "bets_last_15m": 0.0,
                "bets_last_30m": 0.0,
                "wager_last_10m": 0.0,
                "wager_last_30m": 0.0,
                "cum_bets": 0.0,
                "cum_wager": 0.0,
                "avg_wager_sofar": 0.0,
                "session_duration_min": 0.0,
                "bets_per_minute": 0.0,
                "canonical_id": "c10",
                "is_rated_obs": 0,
                "reason_codes": None,
                "model_version": "test",
                "margin": 0.4,
                "scored_at": "2025-06-01T10:00:00+08:00",
                "game_id": 900.0,
                "player_game_score": 0.9,
                "player_game_bet_count": 3,
            }
        ]
    )
    with sqlite3.connect(db) as conn:
        append_alerts(conn, alerts)
        row = conn.execute(
            "SELECT game_id, player_game_score, player_game_bet_count FROM alerts WHERE bet_id = '1'"
        ).fetchone()
    assert row is not None
    assert row[0] == "900.0"
    assert float(row[1]) == pytest.approx(0.9)
    assert int(row[2]) == 3
