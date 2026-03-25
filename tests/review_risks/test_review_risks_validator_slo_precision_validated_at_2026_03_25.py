"""Review risks MRE — Validator SLO rolling precision uses ``validated_at``.

Tests-only: no production changes.

Targets reviewer-noted risks for `_rolling_precision_by_validated_at` and KPI log strings.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import trainer.serving.validator as validator_mod

HK_TZ = ZoneInfo("Asia/Hong_Kong")


def _hk(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=HK_TZ)


def test_mre_rolling_precision_now_hk_naive_does_not_crash_and_counts() -> None:
    df = pd.DataFrame(
        {
            "validated_at": [_hk("2026-01-01 11:55:00")],
            "reason": ["MATCH"],
        }
    )
    # tz-naive: common footgun if caller forgets tzinfo
    now_hk = datetime(2026, 1, 1, 12, 0, 0)
    p, m, t = validator_mod._rolling_precision_by_validated_at(df, now_hk=now_hk, window=timedelta(minutes=15))
    assert (t, m, p) == (1, 1, 1.0)

def test_mre_rolling_precision_mixed_validated_at_tz_does_not_crash() -> None:
    now_hk = datetime(2026, 1, 1, 12, 0, 0, tzinfo=HK_TZ)
    df = pd.DataFrame(
        {
            # Mix: tz-aware string + tz-naive string
            "validated_at": ["2026-01-01T11:55:00+08:00", "2026-01-01 11:56:00"],
            "reason": ["MATCH", "MISS"],
        }
    )
    p, m, t = validator_mod._rolling_precision_by_validated_at(df, now_hk=now_hk, window=timedelta(minutes=15))
    assert (t, m) == (2, 1)
    assert abs(p - 0.5) < 1e-12


def test_contract_rolling_precision_requires_only_validated_at_and_reason() -> None:
    now_hk = datetime(2026, 1, 1, 12, 0, 0, tzinfo=HK_TZ)
    df = pd.DataFrame(
        {
            "validated_at": [_hk("2026-01-01 11:55:00"), _hk("2026-01-01 11:30:00")],
            "reason": ["MATCH", "MISS"],
        }
    )
    p, m, t = validator_mod._rolling_precision_by_validated_at(df, now_hk=now_hk, window=timedelta(minutes=15))
    assert (t, m, p) == (1, 1, 1.0)


def test_contract_kpi_log_strings_include_by_validated_at() -> None:
    """Contract: prevent silent reversion of KPI semantics in console logs."""

    import inspect

    src = inspect.getsource(validator_mod)
    assert "Cumulative Precision (15m window, by validated_at)" in src
    assert "Cumulative Precision (1h window, by validated_at)" in src

