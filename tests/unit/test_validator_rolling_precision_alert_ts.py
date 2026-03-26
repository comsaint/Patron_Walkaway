"""Rolling validator cumulative precision uses ``validated_at`` for the time window."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trainer.serving.validator import _rolling_precision_by_validated_at

HK_TZ = ZoneInfo("Asia/Hong_Kong")


def _hk(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=HK_TZ)


def test_rolling_precision_includes_row_when_validated_at_in_window() -> None:
    now_hk = datetime(2026, 1, 1, 12, 0, 0, tzinfo=HK_TZ)
    df = pd.DataFrame(
        {
            "bet_ts": [_hk("2026-01-01 08:00:00")],
            "alert_ts": [_hk("2026-01-01 11:55:00")],
            "validated_at": [_hk("2026-01-01 11:55:00")],
            "reason": ["MATCH"],
        }
    )
    p, m, t = _rolling_precision_by_validated_at(df, now_hk=now_hk, window=timedelta(minutes=15))
    assert t == 1 and m == 1 and p == 1.0


def test_rolling_precision_excludes_row_when_only_alert_ts_inside_window() -> None:
    """``alert_ts`` inside window does not count; KPI follows ``validated_at``."""
    now_hk = datetime(2026, 1, 1, 12, 0, 0, tzinfo=HK_TZ)
    df = pd.DataFrame(
        {
            "bet_ts": [_hk("2026-01-01 11:50:00")],
            "alert_ts": [_hk("2026-01-01 11:55:00")],
            "validated_at": [_hk("2026-01-01 10:00:00")],
            "reason": ["MATCH"],
        }
    )
    p, m, t = _rolling_precision_by_validated_at(df, now_hk=now_hk, window=timedelta(minutes=15))
    assert t == 0 and m == 0 and p == 0.0


def test_rolling_precision_validated_at_after_stale_now_needs_cycle_end_anchor() -> None:
    """Task 11: per-row validated_at can be later than validate_once cycle-start; KPI uses cycle-end now."""
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=HK_TZ)
    validated_mid = (t0 + timedelta(seconds=45)).isoformat()
    df = pd.DataFrame({"validated_at": [validated_mid], "reason": ["MATCH"]})
    p_stale, m_stale, tot_stale = _rolling_precision_by_validated_at(
        df, now_hk=t0, window=timedelta(minutes=15)
    )
    assert (p_stale, m_stale, tot_stale) == (0.0, 0, 0)
    t_end = t0 + timedelta(minutes=2)
    p_ok, m_ok, tot_ok = _rolling_precision_by_validated_at(
        df, now_hk=t_end, window=timedelta(minutes=15)
    )
    assert tot_ok == 1 and m_ok == 1 and p_ok == 1.0


def test_rolling_precision_excludes_validated_at_after_now_hk_future_clock_skew() -> None:
    """STATUS review #1: ``validated_at`` strictly after upper-bound ``now_hk`` stays excluded (intentional)."""
    now_hk = datetime(2026, 1, 1, 12, 0, 0, tzinfo=HK_TZ)
    future = (now_hk + timedelta(minutes=5)).isoformat()
    df = pd.DataFrame({"validated_at": [future], "reason": ["MATCH"]})
    _, _, tot = _rolling_precision_by_validated_at(df, now_hk=now_hk, window=timedelta(hours=1))
    assert tot == 0


def test_rolling_precision_includes_row_when_validated_at_equals_now_hk() -> None:
    """Upper bound is inclusive: ``vt <= now_hk`` (boundary)."""
    now_hk = datetime(2026, 1, 1, 12, 0, 0, tzinfo=HK_TZ)
    df = pd.DataFrame({"validated_at": [now_hk.isoformat()], "reason": ["MATCH"]})
    _, _, tot = _rolling_precision_by_validated_at(df, now_hk=now_hk, window=timedelta(minutes=15))
    assert tot == 1


@pytest.mark.parametrize(
    "now_minute,expected_total",
    [
        (9, 0),  # row at 12:10, now 12:09 -> after now_hk
        (10, 1),  # on boundary
        (11, 1),
        (25, 1),
    ],
)
def test_rolling_precision_window_inclusion_vs_now_hk(now_minute: int, expected_total: int) -> None:
    """STATUS review #3: single-row inclusion vs upper bound and 15m cutoff."""
    row_t = datetime(2026, 1, 1, 12, 10, 0, tzinfo=HK_TZ)
    df = pd.DataFrame({"validated_at": [row_t.isoformat()], "reason": ["MATCH"]})
    w = timedelta(minutes=15)
    now_hk = datetime(2026, 1, 1, 12, now_minute, 0, tzinfo=HK_TZ)
    _, _, tot = _rolling_precision_by_validated_at(df, now_hk=now_hk, window=w)
    assert tot == expected_total


def test_rolling_precision_total_non_decreasing_as_now_hk_advances() -> None:
    """STATUS review #3: advancing ``now_hk`` never shrinks in-window count (monotonicity)."""
    row_t = datetime(2026, 1, 1, 12, 10, 0, tzinfo=HK_TZ)
    df = pd.DataFrame({"validated_at": [row_t.isoformat()], "reason": ["MATCH"]})
    w = timedelta(minutes=15)
    totals: list[int] = []
    for nm in (9, 10, 11, 25):
        nh = datetime(2026, 1, 1, 12, nm, 0, tzinfo=HK_TZ)
        _, _, t = _rolling_precision_by_validated_at(df, now_hk=nh, window=w)
        totals.append(t)
    assert totals == sorted(totals)


def test_rolling_precision_excludes_row_when_validated_at_nat() -> None:
    now_hk = datetime(2026, 1, 1, 12, 0, 0, tzinfo=HK_TZ)
    df = pd.DataFrame(
        {
            "bet_ts": [_hk("2026-01-01 11:55:00")],
            "alert_ts": [_hk("2026-01-01 11:55:00")],
            "validated_at": [pd.NaT],
            "reason": ["MISS"],
        }
    )
    p, m, t = _rolling_precision_by_validated_at(df, now_hk=now_hk, window=timedelta(minutes=15))
    assert t == 0 and m == 0 and p == 0.0
