"""Validator gap_within_window deterministic checks (legacy trainer parity lineage)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from trainer_hightier.serving import validator as hv
from trainer_hightier.serving.state_db import init_state_db


def test_find_gap_within_window_known_case() -> None:
    hk = ZoneInfo("Asia/Hong_Kong")
    alert_ts = datetime(2024, 1, 1, 12, 0, tzinfo=hk)
    bets = [
        alert_ts + timedelta(minutes=5),
        alert_ts + timedelta(minutes=50),
    ]
    out = hv.find_gap_within_window(alert_ts, bets)
    gap_start_expected = bets[0]
    horizon_end = alert_ts + timedelta(minutes=int(hv.config.LABEL_LOOKAHEAD_MIN))
    gap_minutes_expected = (horizon_end - gap_start_expected).total_seconds() / 60.0
    assert out[0] is True
    assert out[1] == gap_start_expected
    assert abs(float(out[2]) - gap_minutes_expected) < 1e-6


def test_no_bet_warning_suppressed_after_bet_id_resolved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """bet_id recovery populates cache and suppresses repeat No bet data warnings."""
    hv._NO_BET_BET_ID_RESOLVED_CACHE.clear()
    hv._NO_BET_BET_ID_RESOLVED_BET_IDS.clear()

    hk = ZoneInfo("Asia/Hong_Kong")
    old_enough = datetime.now(hk) - timedelta(minutes=int(hv.config.LABEL_LOOKAHEAD_MIN) + 30)
    bet_ts = old_enough - timedelta(minutes=5)
    row = pd.Series(
        {
            "ts": old_enough,
            "bet_ts": bet_ts,
            "player_id": 148965936,
            "casino_player_id": "97172654",
            "canonical_id": "148965936",
            "bet_id": 699154634,
            "score": 0.9,
            "table_id": 1,
            "position_idx": 1,
            "session_id": "s1",
            "model_version": "m1",
            "scored_at": old_enough,
        }
    )
    bet_cache: dict[str, list[datetime]] = {}

    with caplog.at_level(logging.WARNING, logger="trainer_hightier.serving.validator"):
        res = hv.validate_alert_row(row, bet_cache, {}, defer_no_bet_warning=True)
        assert res.get("_no_bet_data") is True
        assert not any("No bet data" in r.message for r in caplog.records)

        hv._NO_BET_BET_ID_RESOLVED_BET_IDS.add("699154634")
        hv._NO_BET_BET_ID_RESOLVED_CACHE["148965936"] = [bet_ts]
        hv._merge_bet_id_resolved_cache(bet_cache)
        hv._emit_no_bet_warning_if_still_empty(row, bet_cache)
        assert not any("No bet data" in r.message for r in caplog.records)

        res2 = hv.validate_alert_row(row, bet_cache, {})
        assert res2.get("_no_bet_data") is not True
        assert res2.get("result") is not None


def test_bet_id_resolved_cache_survives_bet_cache_clear() -> None:
    """bet_cache.clear() drops per-cycle data but merge restores bet_id recovery."""

    hv._NO_BET_BET_ID_RESOLVED_CACHE.clear()
    hv._NO_BET_BET_ID_RESOLVED_BET_IDS.clear()

    hk = ZoneInfo("Asia/Hong_Kong")
    bet_ts = datetime(2024, 1, 1, 12, 5, tzinfo=hk)
    hv._NO_BET_BET_ID_RESOLVED_CACHE["148965936"] = [bet_ts]
    hv._NO_BET_BET_ID_RESOLVED_BET_IDS.add("699154634")

    bet_cache: dict[str, list[datetime]] = {
        "other_player": [datetime(2024, 1, 1, 10, 0, tzinfo=hk)],
    }
    bet_cache.clear()
    assert "148965936" not in bet_cache

    hv._merge_bet_id_resolved_cache(bet_cache)
    assert bet_cache["148965936"] == [bet_ts]

    old_enough = datetime.now(hk) - timedelta(minutes=int(hv.config.LABEL_LOOKAHEAD_MIN) + 30)
    row = pd.Series(
        {
            "ts": old_enough,
            "bet_ts": bet_ts,
            "player_id": 148965936,
            "casino_player_id": "97172654",
            "canonical_id": "148965936",
            "bet_id": 699154634,
            "score": 0.9,
            "table_id": 1,
            "position_idx": 1,
            "session_id": "s1",
            "model_version": "m1",
            "scored_at": old_enough,
        }
    )
    res = hv.validate_alert_row(row, bet_cache, {})
    assert res.get("_no_bet_data") is not True
    assert res.get("result") is not None


def test_load_existing_results_incremental_cold_start_full_bootstrap(tmp_path) -> None:
    """Empty cache with persisted rowid watermark must full-bootstrap (rolling KPI under-count fix)."""
    db_path = init_state_db(tmp_path / "state.db")
    conn = sqlite3.connect(db_path)
    for i in range(1, 6):
        conn.execute(
            """
            INSERT INTO validation_results (bet_id, alert_ts, validated_at, player_id, result, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(i), f"2024-01-01 12:0{i}:00", f"2024-01-01 12:1{i}:00", "10", 1, "MATCH"),
        )
    conn.commit()
    max_rowid = conn.execute("SELECT MAX(rowid) FROM validation_results").fetchone()[0]
    conn.execute(
        "INSERT INTO validator_runtime_meta (key, value) VALUES (?, ?)",
        (hv._VALIDATOR_META_KEY_LAST_ROWID, str(max_rowid)),
    )
    conn.commit()

    loaded = hv.load_existing_results_incremental(conn, {})
    assert len(loaded) == 5

    conn.execute(
        """
        INSERT INTO validation_results (bet_id, alert_ts, validated_at, player_id, result, reason)
        VALUES ('6', '2024-01-01 12:06:00', '2024-01-01 12:16:00', '10', 0, 'NO_MATCH')
        """,
    )
    conn.commit()

    loaded2 = hv.load_existing_results_incremental(conn, loaded)
    assert len(loaded2) == 6
    conn.close()


def test_rolling_precision_15m_total_leq_1h_window() -> None:
    """Shorter validated_at window must not exceed longer window totals."""
    hk = ZoneInfo("Asia/Hong_Kong")
    now = datetime(2026, 6, 3, 12, 0, tzinfo=hk)
    rows = []
    for minutes_ago in (10, 20, 40, 70):
        ts = now - timedelta(minutes=minutes_ago)
        rows.append(
            {
                "validated_at": ts.isoformat(),
                "result": 1,
                "reason": "MATCH",
            }
        )
    df = pd.DataFrame(rows)
    _, total_15m, _ = hv._rolling_precision_by_validated_at(
        df, now_hk=now, window=timedelta(minutes=15)
    )
    _, total_1h, _ = hv._rolling_precision_by_validated_at(
        df, now_hk=now, window=timedelta(hours=1)
    )
    assert total_15m <= total_1h
    assert total_15m == 1
    assert total_1h == 3
