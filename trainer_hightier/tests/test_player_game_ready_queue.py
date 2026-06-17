"""Tests for Wave 4 player-game serving ready queue dry-run."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.config import SCORER_POLL_INTERVAL_SECONDS
from trainer_hightier.serving.player_game_ready_queue import (
    enqueue_player_games_from_bets,
    init_player_game_ready_queue_tables,
    process_due_player_games,
    process_one_pending_player_game,
    refetch_player_game_from_frame,
    run_player_game_ready_queue_dry_run_cycle,
    summarize_dry_run_metrics,
)
from trainer_hightier.serving.state_db import (
    get_last_processed_etl_insert,
    init_state_db,
    set_last_processed_etl_insert,
)


def _conn(db_path: Path) -> sqlite3.Connection:
    """Open one initialized state DB connection."""

    init_state_db(db_path)
    return sqlite3.connect(db_path)


def _bet(
    *,
    bet_id: int,
    player_id: int = 10,
    game_id: int = 100,
    pv: str,
    pcd: str = "2026-05-28 14:52:45+00:00",
    type_of_bet: str = "MAIN_BET",
) -> dict[str, object]:
    """Build one minimal incremental bet row."""

    return {
        "bet_id": bet_id,
        "player_id": player_id,
        "game_id": game_id,
        "payout_complete_dtm": pd.Timestamp(pcd),
        "prediction_visible_ts_cf": pd.Timestamp(pv),
        "wager": 100.0,
        "type_of_bet": type_of_bet,
        "__etl_insert_Dtm": pd.Timestamp("2026-05-28 14:53:00+00:00"),
    }


def test_init_creates_queue_tables(tmp_path: Path) -> None:
    """State DB init creates player-game queue tables."""

    db = tmp_path / "state.db"
    init_state_db(db)
    with sqlite3.connect(db) as conn:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            ).fetchall()
        }
    assert "pg_pending_player_games" in tables
    assert "pg_completed_player_games" in tables
    assert "pg_ready_queue_dry_run_cycles" in tables


def test_enqueue_sets_due_ts_after_holdback(tmp_path: Path) -> None:
    """Pending due_ts equals first_seen_pv + holdback."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    pv = "2026-05-28T22:54:45+00:00"
    now = datetime(2026, 5, 28, 22, 54, 0, tzinfo=timezone.utc)
    bets = pd.DataFrame([_bet(bet_id=1, pv=pv)])
    enqueue_player_games_from_bets(conn, bets, now_ts=now)
    row = conn.execute(
        "SELECT first_seen_prediction_visible_ts, due_ts FROM pg_pending_player_games",
    ).fetchone()
    conn.close()
    assert row is not None
    first = pd.Timestamp(row[0])
    due = pd.Timestamp(row[1])
    assert due == first + pd.Timedelta(seconds=SCORER_POLL_INTERVAL_SECONDS)


def test_defer_when_late_bet_extends_visibility(tmp_path: Path) -> None:
    """Re-fetch with max_pv > now defers pending and increments attempt_count."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    main_pv = "2026-05-28T22:54:45+00:00"
    late_pv = "2026-05-28T22:55:35+00:00"
    all_bets = pd.DataFrame(
        [
            _bet(bet_id=1, pv=main_pv),
            _bet(bet_id=2, pv=late_pv, type_of_bet="SIDE_BET"),
        ],
    )
    enqueue_ts = datetime(2026, 5, 28, 22, 54, 50, tzinfo=timezone.utc)
    enqueue_player_games_from_bets(conn, all_bets.iloc[[0]], now_ts=enqueue_ts)

    def fetch(_pid: int, _gid: int) -> pd.DataFrame:
        return refetch_player_game_from_frame(all_bets, _pid, _gid)

    due_ts = datetime(2026, 5, 28, 22, 55, 30, tzinfo=timezone.utc)
    result = process_one_pending_player_game(
        conn,
        10,
        100,
        now_ts=due_ts,
        fetch_fn=fetch,
    )
    assert result.action == "deferred"
    attempt = conn.execute(
        "SELECT attempt_count FROM pg_pending_player_games WHERE player_id=10 AND game_id=100",
    ).fetchone()
    assert attempt is not None
    assert int(attempt[0]) == 1
    conn.close()


def test_complete_after_holdback_and_visibility(tmp_path: Path) -> None:
    """Due pending completes dry-run when max_pv <= now."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    pv = "2026-05-28T22:54:45+00:00"
    bets = pd.DataFrame([_bet(bet_id=1, pv=pv)])
    enqueue_player_games_from_bets(
        conn,
        bets,
        now_ts=datetime(2026, 5, 28, 22, 54, 50, tzinfo=timezone.utc),
    )

    def fetch(_pid: int, _gid: int) -> pd.DataFrame:
        return refetch_player_game_from_frame(bets, _pid, _gid)

    now = datetime(2026, 5, 28, 22, 56, 0, tzinfo=timezone.utc)
    result = process_one_pending_player_game(conn, 10, 100, now_ts=now, fetch_fn=fetch)
    assert result.action == "completed"
    pending = conn.execute("SELECT COUNT(*) FROM pg_pending_player_games").fetchone()[0]
    completed = conn.execute(
        "SELECT bet_count, late_after_score_hypothetical FROM pg_completed_player_games",
    ).fetchone()
    conn.close()
    assert int(pending) == 0
    assert completed is not None
    assert int(completed[0]) == 1
    assert int(completed[1]) == 0


def test_completed_keys_are_not_re_enqueued(tmp_path: Path) -> None:
    """Idempotency: completed player-games skip enqueue."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    pv = "2026-05-28T22:54:45+00:00"
    bets = pd.DataFrame([_bet(bet_id=1, pv=pv)])

    def fetch(_pid: int, _gid: int) -> pd.DataFrame:
        return refetch_player_game_from_frame(bets, _pid, _gid)

    run_player_game_ready_queue_dry_run_cycle(
        conn,
        incremental_bets=bets,
        now_ts=datetime(2026, 5, 28, 22, 56, 0, tzinfo=timezone.utc),
        fetch_fn=fetch,
    )
    n_touch = enqueue_player_games_from_bets(
        conn,
        bets,
        now_ts=datetime(2026, 5, 28, 22, 57, 0, tzinfo=timezone.utc),
    )
    pending = conn.execute("SELECT COUNT(*) FROM pg_pending_player_games").fetchone()[0]
    conn.close()
    assert n_touch == 0
    assert int(pending) == 0


def test_cursor_advances_independently_of_queue(tmp_path: Path) -> None:
    """ETL cursor watermark is separate from pending/completed queue state."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    watermark = datetime(2026, 5, 28, 22, 50, 0, tzinfo=timezone.utc)
    set_last_processed_etl_insert(conn, watermark)
    pv = "2026-05-28T22:54:45+00:00"
    bets = pd.DataFrame([_bet(bet_id=1, pv=pv)])

    def fetch(_pid: int, _gid: int) -> pd.DataFrame:
        return refetch_player_game_from_frame(bets, _pid, _gid)

    run_player_game_ready_queue_dry_run_cycle(
        conn,
        incremental_bets=bets,
        now_ts=datetime(2026, 5, 28, 22, 54, 50, tzinfo=timezone.utc),
        fetch_fn=fetch,
    )
    after = get_last_processed_etl_insert(conn)
    pending = conn.execute("SELECT COUNT(*) FROM pg_pending_player_games").fetchone()[0]
    conn.close()
    assert after == pd.Timestamp(watermark)
    assert int(pending) == 1


def test_late_side_bet_defers_then_completes(tmp_path: Path) -> None:
    """Late side bet extends ready_ts; second pass completes with late flag."""

    db = tmp_path / "state.db"
    conn = _conn(db)
    main_pv = "2026-05-28T22:54:45+00:00"
    late_pv = "2026-05-28T22:55:35+00:00"
    all_bets = pd.DataFrame(
        [
            _bet(bet_id=1, pv=main_pv),
            _bet(bet_id=2, pv=late_pv, type_of_bet="SIDE_BET"),
        ],
    )

    def fetch(_pid: int, _gid: int) -> pd.DataFrame:
        return refetch_player_game_from_frame(all_bets, _pid, _gid)

    run_player_game_ready_queue_dry_run_cycle(
        conn,
        incremental_bets=all_bets.iloc[[0]],
        now_ts=datetime(2026, 5, 28, 22, 55, 30, tzinfo=timezone.utc),
        fetch_fn=fetch,
    )
    summary = process_due_player_games(
        conn,
        now_ts=datetime(2026, 5, 28, 22, 56, 25, tzinfo=timezone.utc),
        fetch_fn=fetch,
    )
    metrics = summarize_dry_run_metrics(conn)
    conn.close()
    assert summary.n_completed == 1
    assert metrics["n_completed"] == 1
    assert metrics["n_late_hypothetical"] >= 1


def test_process_due_chunked_clickhouse_batches_incremental_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ClickHouse path issues one incremental fetch per player-id chunk."""

    from dataclasses import replace

    from trainer_hightier.config import default_hightier_serving_config

    db = tmp_path / "state.db"
    conn = _conn(db)
    pv = "2026-05-28T22:54:45+00:00"
    bets = pd.DataFrame(
        [
            {
                "bet_id": 1,
                "player_id": 10,
                "game_id": 100,
                "payout_complete_dtm": pd.Timestamp("2026-05-28 14:52:45+00:00"),
                "prediction_visible_ts_cf": pd.Timestamp(pv),
                "wager": 100.0,
                "casino_win": 0.0,
                "is_back_bet": 0,
                "bet_type": "MAIN",
                "type_of_bet": "MAIN_BET",
                "__etl_insert_Dtm": pd.Timestamp("2026-05-28 14:53:00+00:00"),
            },
            {
                "bet_id": 2,
                "player_id": 11,
                "game_id": 101,
                "payout_complete_dtm": pd.Timestamp("2026-05-28 14:52:50+00:00"),
                "prediction_visible_ts_cf": pd.Timestamp(pv),
                "wager": 50.0,
                "casino_win": 0.0,
                "is_back_bet": 0,
                "bet_type": "MAIN",
                "type_of_bet": "MAIN_BET",
                "__etl_insert_Dtm": pd.Timestamp("2026-05-28 14:53:05+00:00"),
            },
        ],
    )
    enqueue_player_games_from_bets(
        conn,
        bets,
        now_ts=datetime(2026, 5, 28, 14, 53, 0, tzinfo=timezone.utc),
    )
    fetch_calls: list[frozenset[int] | None] = []

    def _fake_incremental(
        _last_etl: object,
        *,
        lookback_hours: float,
        limit_rows: int,
        allowlist_player_ids: frozenset[int] | None = None,
    ) -> pd.DataFrame:
        fetch_calls.append(allowlist_player_ids)
        return bets.copy()

    monkeypatch.setattr(
        "trainer_hightier.serving.scorer.fetch_bets_incremental",
        _fake_incremental,
    )
    cfg = replace(default_hightier_serving_config(), hightier_scorer_player_id_chunk_size=500)
    monkeypatch.setattr(
        "trainer_hightier.serving.player_game_ready_queue.default_hightier_serving_config",
        lambda: cfg,
    )
    summary = process_due_player_games(
        conn,
        now_ts=datetime(2026, 5, 28, 22, 56, 0, tzinfo=timezone.utc),
        fetch_fn=None,
    )
    conn.close()
    assert len(fetch_calls) == 1
    assert fetch_calls[0] == frozenset({10, 11})
    assert summary.n_completed == 2
