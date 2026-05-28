"""Unit tests for prediction_log ground-truth validation (Phase B)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from trainer_hightier.serving import validator as hv
from trainer_hightier.serving.prediction_log import (
    backfill_prediction_log_bet_ts,
    format_bet_ts_iso,
    init_prediction_log_db,
    load_processed_predictions,
)


def test_format_bet_ts_iso_hk() -> None:
    hk = ZoneInfo("Asia/Hong_Kong")
    ts = datetime(2025, 6, 1, 12, 0, tzinfo=hk)
    out = format_bet_ts_iso(ts)
    assert out is not None
    assert "+08:00" in out


def test_init_prediction_log_db_has_bet_ts_and_validation_tables(tmp_path) -> None:
    db = tmp_path / "prediction_log.db"
    init_prediction_log_db(db)
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(prediction_log)").fetchall()}
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "bet_ts" in cols
    assert "prediction_validation_results" in tables
    assert "processed_predictions" in tables


def test_backfill_missing_bet_ts_poison_pill(tmp_path) -> None:
    db = tmp_path / "prediction_log.db"
    init_prediction_log_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO prediction_log (
                scored_at, bet_id, model_version, score, margin, is_alert, is_rated_obs
            ) VALUES ('2025-01-01T00:00:00+08:00', '999', 'mv', 0.5, 0.0, 0, 1)
            """
        )
        conn.commit()

    def _empty_fetch(_ids, *, chunk_size: int):
        return {}, 0, 0, 0

    stats = backfill_prediction_log_bet_ts(db, chunk_size=10, fetch_by_bet_ids=_empty_fetch)
    assert stats["missing"] == 1
    with sqlite3.connect(db) as conn:
        reason = conn.execute(
            "SELECT reason FROM prediction_validation_results WHERE bet_id='999'"
        ).fetchone()[0]
        processed = load_processed_predictions(conn)
    assert reason == "missing_bet_ts"
    assert "999" in processed


def test_prediction_validation_fetch_window_anchors_on_bet_ts() -> None:
    """Phase B CH window must not use ``now - VALIDATOR_FETCH_MAX_LOOKBACK`` floor."""
    hk = ZoneInfo("Asia/Hong_Kong")
    now_hk = datetime(2026, 5, 27, 14, 0, tzinfo=hk)
    old_bet = now_hk - timedelta(hours=8)
    pending = pd.DataFrame(
        {
            "bet_ts": [old_bet.isoformat(), (old_bet + timedelta(hours=1)).isoformat()],
            "canonical_id": ["10", "10"],
            "player_id": [10, 10],
        }
    )
    window = hv._prediction_validation_fetch_window(
        pending,
        now_hk=now_hk,
        freshness_buffer_min=2,
    )
    assert window is not None
    fetch_start, fetch_end = window
    assert fetch_start == old_bet
    assert fetch_start < now_hk - timedelta(minutes=180)
    assert fetch_end >= now_hk


def test_fetch_prediction_bet_cache_extension_bet_anchored_ch_window() -> None:
    """Phase B must query CH from oldest pending ``bet_ts``, not ``now - 180min``."""
    hk = ZoneInfo("Asia/Hong_Kong")
    now_hk = datetime(2026, 5, 27, 14, 0, tzinfo=hk)
    old_bet = now_hk - timedelta(hours=8)
    pending = pd.DataFrame(
        {
            "bet_ts": [old_bet.isoformat()],
            "canonical_id": ["10"],
            "player_id": [10],
        }
    )
    captured: dict = {}

    def _fake_fetch(
        cid_to_pids: dict,
        start: datetime,
        end: datetime,
    ) -> dict:
        captured["start"] = start
        captured["end"] = end
        return {"10": [old_bet]}

    bet_cache: dict = {}
    with patch.object(hv, "get_clickhouse_client", return_value=object()), patch.object(
        hv, "fetch_bets_by_canonical_id", side_effect=_fake_fetch
    ):
        hv._fetch_prediction_bet_cache_extension(
            pending,
            bet_cache,
            now_hk=now_hk,
            freshness_buffer_min=2,
        )
    assert captured["start"] == old_bet
    assert captured["start"] < now_hk - timedelta(minutes=180)
    assert captured["end"] >= now_hk
    assert "10" in bet_cache


def test_fetch_bet_payout_times_by_bet_ids_handles_duplicate_index() -> None:
    """Duplicate DataFrame index must not yield multi-value tuples in bid_map."""
    hk = ZoneInfo("Asia/Hong_Kong")
    dup_df = pd.DataFrame(
        {
            "bet_id": [99, 99],
            "payout_complete_dtm": [
                datetime(2025, 1, 1, 10, 0, tzinfo=hk),
                datetime(2025, 1, 1, 11, 0, tzinfo=hk),
            ],
            "player_id": [10, 10],
        },
        index=[0, 0],
    )

    class _FakeClient:
        def query_df(self, _query, *, parameters=None):
            del parameters
            return dup_df

    with patch.object(hv, "get_clickhouse_client", return_value=_FakeClient()):
        bid_map, *_rest = hv.fetch_bet_payout_times_by_bet_ids([99], chunk_size=10)

    assert list(bid_map.keys()) == ["99"]
    payout_hk, ch_pid = bid_map["99"]
    assert payout_hk == datetime(2025, 1, 1, 11, 0, tzinfo=hk)
    assert ch_pid == 10


def test_validate_observation_row_matches_alert_row() -> None:
    hk = ZoneInfo("Asia/Hong_Kong")
    bet_ts = datetime(2024, 1, 1, 12, 0, tzinfo=hk)
    alert_row = pd.Series(
        {
            "ts": bet_ts.isoformat(),
            "bet_ts": bet_ts,
            "bet_id": "1",
            "player_id": 10,
            "canonical_id": "10",
            "score": 0.9,
        }
    )
    pred_row = hv._prediction_row_to_validator_series(
        pd.Series(
            {
                "scored_at": bet_ts.isoformat(),
                "bet_ts": bet_ts.isoformat(),
                "bet_id": "1",
                "player_id": 10,
                "canonical_id": "10",
                "score": 0.9,
            }
        )
    )
    cache = {"10": [bet_ts + timedelta(minutes=50)]}
    empty_sessions: dict = {}
    alert_res = hv.validate_observation_row(alert_row, cache, empty_sessions, force_finalize=True)
    pred_res = hv.validate_observation_row(pred_row, cache, empty_sessions, force_finalize=True)
    assert alert_res.get("result") == pred_res.get("result")
    assert alert_res.get("reason") == pred_res.get("reason")


def test_validate_once_runs_prediction_phase_when_no_alerts(tmp_path) -> None:
    state_db = tmp_path / "state.db"
    pl_db = tmp_path / "prediction_log.db"
    from trainer_hightier.serving.state_db import init_state_db

    init_state_db(state_db)
    init_prediction_log_db(pl_db)

    called = {"n": 0}

    def _fake_pred(**kwargs) -> None:
        called["n"] += 1

    with patch.object(hv.config, "PREDICTION_LOG_DB_PATH", pl_db), patch.object(
        hv.config, "PREDICTION_VALIDATION_ENABLED", True
    ), patch.object(hv, "validate_predictions_once", side_effect=_fake_pred):
        conn = sqlite3.connect(state_db)
        hv.validate_once(conn)
        conn.close()
    assert called["n"] == 1


def test_prediction_phase_errors_do_not_break_alert_save(tmp_path) -> None:
    state_db = tmp_path / "state.db"
    pl_db = tmp_path / "prediction_log.db"
    from trainer_hightier.serving.state_db import init_state_db

    init_state_db(state_db)
    hk = ZoneInfo("Asia/Hong_Kong")
    old = datetime.now(hk) - timedelta(hours=2)
    bet_ts = old - timedelta(minutes=60)
    alert_ts = old.isoformat()
    with sqlite3.connect(state_db) as conn:
        conn.execute(
            """
            INSERT INTO alerts (bet_id, ts, bet_ts, player_id, score, canonical_id)
            VALUES ('1', ?, ?, 10, 0.9, '10')
            """,
            (alert_ts, bet_ts.isoformat()),
        )
        conn.commit()

    cache = {"10": [bet_ts + timedelta(minutes=50)]}
    with patch.object(hv.config, "PREDICTION_LOG_DB_PATH", pl_db), patch.object(
        hv, "validate_predictions_once", side_effect=RuntimeError("boom")
    ), patch.object(hv, "fetch_bets_by_canonical_id", return_value=cache):
        conn = sqlite3.connect(state_db)
        hv.validate_once(conn, force_finalize=True)
        with sqlite3.connect(state_db) as c2:
            n = c2.execute("SELECT COUNT(*) FROM validation_results").fetchone()[0]
        conn.close()
    assert n >= 1
