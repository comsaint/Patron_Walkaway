"""Rolling validator cumulative precision uses ``validated_at`` for the time window."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

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
