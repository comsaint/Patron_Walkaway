from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List
from zoneinfo import ZoneInfo

import pandas as pd

import trainer.serving.validator as v


HK_TZ = ZoneInfo("Asia/Hong_Kong")


def _init_min_validator_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE alerts (
            bet_id TEXT PRIMARY KEY,
            ts TEXT,
            bet_ts TEXT,
            player_id TEXT,
            casino_player_id TEXT,
            canonical_id TEXT,
            score REAL,
            scored_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE processed_alerts (
            bet_id TEXT PRIMARY KEY,
            processed_ts TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE validation_results (
            bet_id TEXT PRIMARY KEY,
            alert_ts TEXT,
            validated_at TEXT,
            player_id TEXT,
            casino_player_id TEXT,
            canonical_id TEXT,
            table_id TEXT,
            position_idx REAL,
            session_id TEXT,
            score REAL,
            result INTEGER,
            gap_start TEXT,
            gap_minutes REAL,
            reason TEXT,
            bet_ts TEXT,
            model_version TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE validator_runtime_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()


def test_task9_fetch_window_includes_old_bet_ts_and_finalizes() -> None:
    # bet_ts is 2 hours ago, but alert is scored recently -> must still be verifiable.
    now_hk = datetime(2026, 3, 25, 13, 0, 0, tzinfo=HK_TZ)
    bet_ts = now_hk - timedelta(hours=2)
    score_ts = now_hk - timedelta(minutes=1)

    conn = sqlite3.connect(":memory:")
    _init_min_validator_schema(conn)
    conn.execute(
        "INSERT INTO alerts(bet_id, ts, bet_ts, player_id, casino_player_id, canonical_id, score, scored_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "b1",
            score_ts.isoformat(),
            bet_ts.isoformat(),
            "123",
            "CARD123",
            "CARD123",
            0.9,
            score_ts.isoformat(),
        ),
    )
    conn.commit()

    captured: Dict[str, object] = {}

    def _fake_fetch_bets_by_canonical_id(
        cid_to_pids: Dict[str, List[int]],
        start: datetime,
        end: datetime,
    ) -> Dict[str, List[datetime]]:
        captured["start"] = start
        captured["end"] = end
        # Provide minimal bet_list so validator can conclude (no late arrivals in horizon -> MATCH).
        return {"CARD123": [bet_ts]}

    # Freeze time inside validator module and bypass ClickHouse client requirement.
    old_dt = v.datetime
    old_fetch = v.fetch_bets_by_canonical_id
    old_ch = v.get_clickhouse_client
    try:
        class _FrozenDateTime(datetime):  # type: ignore[misc]
            @classmethod
            def now(cls, tz=None):
                return now_hk if tz is None else now_hk.astimezone(tz)

        v.datetime = _FrozenDateTime
        v.fetch_bets_by_canonical_id = _fake_fetch_bets_by_canonical_id
        v.get_clickhouse_client = lambda: object()

        v.validate_once(conn)
    finally:
        v.datetime = old_dt
        v.fetch_bets_by_canonical_id = old_fetch
        v.get_clickhouse_client = old_ch

    # Fetch window should start before bet_ts (includes pre-context).
    assert "start" in captured and "end" in captured
    assert isinstance(captured["start"], datetime)
    assert isinstance(captured["end"], datetime)
    assert captured["start"] <= bet_ts
    assert captured["end"] == now_hk

    df = pd.read_sql_query("SELECT bet_id, reason FROM validation_results", conn)
    assert df.loc[0, "bet_id"] == "b1"
    assert df.loc[0, "reason"] in ("MATCH", "MISS")

