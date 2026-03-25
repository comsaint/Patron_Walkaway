"""avg alerts/hour over state DB uses bet_ts (COALESCE with ts)."""

from __future__ import annotations

import sqlite3

import pytest

from trainer.serving.scorer import _avg_alerts_per_hour_db_by_bet_ts


@pytest.fixture()
def memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE alerts (bet_id TEXT PRIMARY KEY, ts TEXT, bet_ts TEXT)"
    )
    conn.commit()
    return conn


def test_avg_rate_empty_table(memory_conn: sqlite3.Connection) -> None:
    assert _avg_alerts_per_hour_db_by_bet_ts(memory_conn) is None


def test_avg_rate_two_alerts_two_hours_apart_by_bet_ts(memory_conn: sqlite3.Connection) -> None:
    memory_conn.executemany(
        "INSERT INTO alerts(bet_id, ts, bet_ts) VALUES (?, ?, ?)",
        [
            ("b1", "2026-01-01T10:00:00+08:00", "2026-01-01T10:00:00+08:00"),
            ("b2", "2026-01-01T12:00:00+08:00", "2026-01-01T12:00:00+08:00"),
        ],
    )
    memory_conn.commit()
    r = _avg_alerts_per_hour_db_by_bet_ts(memory_conn)
    assert r is not None and abs(r - 1.0) < 1e-6


def test_avg_rate_uses_bet_ts_not_ts_for_span(memory_conn: sqlite3.Connection) -> None:
    """Same alert ts, different bet_ts 1h apart -> span 1h, rate = 2/h."""
    memory_conn.executemany(
        "INSERT INTO alerts(bet_id, ts, bet_ts) VALUES (?, ?, ?)",
        [
            ("b1", "2026-01-01T15:00:00+08:00", "2026-01-01T10:00:00+08:00"),
            ("b2", "2026-01-01T15:00:00+08:00", "2026-01-01T11:00:00+08:00"),
        ],
    )
    memory_conn.commit()
    r = _avg_alerts_per_hour_db_by_bet_ts(memory_conn)
    assert r is not None and abs(r - 2.0) < 1e-6


def test_avg_rate_falls_back_to_ts_when_bet_ts_null(memory_conn: sqlite3.Connection) -> None:
    memory_conn.executemany(
        "INSERT INTO alerts(bet_id, ts, bet_ts) VALUES (?, ?, ?)",
        [
            ("b1", "2026-01-01T10:00:00+08:00", None),
            ("b2", "2026-01-01T12:00:00+08:00", None),
        ],
    )
    memory_conn.commit()
    r = _avg_alerts_per_hour_db_by_bet_ts(memory_conn)
    assert r is not None and abs(r - 1.0) < 1e-6


def test_avg_rate_single_row_uses_minimum_span(memory_conn: sqlite3.Connection) -> None:
    memory_conn.execute(
        "INSERT INTO alerts(bet_id, ts, bet_ts) VALUES (?, ?, ?)",
        ("b1", "2026-01-01T10:00:00+08:00", "2026-01-01T10:00:00+08:00"),
    )
    memory_conn.commit()
    r = _avg_alerts_per_hour_db_by_bet_ts(memory_conn)
    assert r is not None and abs(r - 1.0) < 1e-6
