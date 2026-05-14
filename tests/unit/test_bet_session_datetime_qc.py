"""Unit tests for ``trainer.core.bet_session_datetime_qc``."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trainer.core.bet_session_datetime_qc import (
    assert_bet_payout_within_session_or_raise,
    summarize_bet_payout_vs_session_window,
)

HK = ZoneInfo("Asia/Hong_Kong")


def _session_row(
    session_id: int,
    start: datetime,
    end: datetime,
    lud: datetime,
) -> dict:
    return {
        "session_id": session_id,
        "session_start_dtm": start,
        "session_end_dtm": end,
        "lud_dtm": lud,
        "__etl_insert_Dtm": lud,
    }


def test_payout_inside_session_inclusive() -> None:
    s0 = datetime(2025, 1, 1, 10, 0, tzinfo=HK)
    s1 = datetime(2025, 1, 1, 12, 0, tzinfo=HK)
    bets = pd.DataFrame(
        {
            "session_id": [1],
            "payout_complete_dtm": [datetime(2025, 1, 1, 11, 0, tzinfo=HK)],
        }
    )
    sessions = pd.DataFrame([_session_row(1, s0, s1, s1)])
    r = summarize_bet_payout_vs_session_window(bets, sessions, hk_zone=HK)
    assert r["n_violations"] == 0
    assert r["n_in_window"] == 1
    assert r["n_evaluable"] == 1


def test_payout_on_session_bounds_counts_in_window() -> None:
    s0 = datetime(2025, 1, 1, 10, 0, tzinfo=HK)
    s1 = datetime(2025, 1, 1, 12, 0, tzinfo=HK)
    bets = pd.DataFrame(
        {
            "session_id": [1, 1],
            "payout_complete_dtm": [s0, s1],
        }
    )
    sessions = pd.DataFrame([_session_row(1, s0, s1, s1)])
    r = summarize_bet_payout_vs_session_window(bets, sessions, hk_zone=HK)
    assert r["n_violations"] == 0
    assert r["n_in_window"] == 2


def test_payout_before_start_is_violation() -> None:
    s0 = datetime(2025, 1, 1, 10, 0, tzinfo=HK)
    s1 = datetime(2025, 1, 1, 12, 0, tzinfo=HK)
    bets = pd.DataFrame(
        {
            "bet_id": [99],
            "session_id": [1],
            "payout_complete_dtm": [datetime(2025, 1, 1, 9, 59, tzinfo=HK)],
        }
    )
    sessions = pd.DataFrame([_session_row(1, s0, s1, s1)])
    r = summarize_bet_payout_vs_session_window(bets, sessions, hk_zone=HK)
    assert r["n_violations"] == 1
    assert r["n_in_window"] == 0


def test_payout_after_end_is_violation() -> None:
    s0 = datetime(2025, 1, 1, 10, 0, tzinfo=HK)
    s1 = datetime(2025, 1, 1, 12, 0, tzinfo=HK)
    bets = pd.DataFrame(
        {
            "session_id": [1],
            "payout_complete_dtm": [datetime(2025, 1, 1, 12, 1, tzinfo=HK)],
        }
    )
    sessions = pd.DataFrame([_session_row(1, s0, s1, s1)])
    r = summarize_bet_payout_vs_session_window(bets, sessions, hk_zone=HK)
    assert r["n_violations"] == 1


def test_fnd01_dedup_uses_latest_session_row() -> None:
    """Later ``lud_dtm`` wins; bet must align to that row's window."""
    s0 = datetime(2025, 1, 1, 10, 0, tzinfo=HK)
    s1 = datetime(2025, 1, 1, 12, 0, tzinfo=HK)
    old_end = datetime(2025, 1, 1, 11, 0, tzinfo=HK)
    lud_old = datetime(2025, 1, 1, 13, 0, tzinfo=HK)
    lud_new = datetime(2025, 1, 1, 14, 0, tzinfo=HK)
    sessions = pd.DataFrame(
        [
            _session_row(1, s0, old_end, lud_old),
            _session_row(1, s0, s1, lud_new),
        ]
    )
    payout_ok = datetime(2025, 1, 1, 11, 30, tzinfo=HK)
    bets = pd.DataFrame({"session_id": [1], "payout_complete_dtm": [payout_ok]})
    r = summarize_bet_payout_vs_session_window(bets, sessions, hk_zone=HK)
    assert r["n_violations"] == 0

    bets_bad = pd.DataFrame(
        {"session_id": [1], "payout_complete_dtm": [datetime(2025, 1, 1, 12, 0, 1, tzinfo=HK)]}
    )
    r2 = summarize_bet_payout_vs_session_window(bets_bad, sessions, hk_zone=HK)
    assert r2["n_violations"] == 1


def test_missing_session_reported() -> None:
    bets = pd.DataFrame(
        {
            "session_id": [42],
            "payout_complete_dtm": [datetime(2025, 1, 1, 11, 0, tzinfo=HK)],
        }
    )
    sessions = pd.DataFrame(
        [
            _session_row(
                1,
                datetime(2025, 1, 1, 10, 0, tzinfo=HK),
                datetime(2025, 1, 1, 12, 0, tzinfo=HK),
                datetime(2025, 1, 1, 12, 0, tzinfo=HK),
            )
        ]
    )
    r = summarize_bet_payout_vs_session_window(bets, sessions, hk_zone=HK)
    assert r["n_missing_session"] == 1
    assert r["n_evaluable"] == 0


def test_dedupe_requires_lud_dtm() -> None:
    bets = pd.DataFrame({"session_id": [1], "payout_complete_dtm": [pd.Timestamp("2025-01-01")]})
    sessions = pd.DataFrame(
        {
            "session_id": [1],
            "session_start_dtm": [pd.Timestamp("2025-01-01 10:00")],
            "session_end_dtm": [pd.Timestamp("2025-01-01 12:00")],
        }
    )
    with pytest.raises(ValueError, match="lud_dtm"):
        summarize_bet_payout_vs_session_window(bets, sessions, dedupe_sessions=True)


def test_assert_raises_on_violation() -> None:
    s0 = datetime(2025, 1, 1, 10, 0, tzinfo=HK)
    s1 = datetime(2025, 1, 1, 12, 0, tzinfo=HK)
    bets = pd.DataFrame(
        {"session_id": [1], "payout_complete_dtm": [datetime(2025, 1, 1, 9, 0, tzinfo=HK)]}
    )
    sessions = pd.DataFrame([_session_row(1, s0, s1, s1)])
    with pytest.raises(AssertionError, match="n_violations=1"):
        assert_bet_payout_within_session_or_raise(bets, sessions, hk_zone=HK)
