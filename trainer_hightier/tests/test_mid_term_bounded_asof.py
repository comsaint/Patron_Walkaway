"""Unit tests for Option B bounded mid-term ASOF helpers."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from trainer_hightier.serving.mid_term_bounded_asof import (
    apply_mid_term_bounded_asof,
    is_mid_anchor_valid,
    mid_asof_lateral_lower_bound_sql,
    resolve_mid_asof_backfill_days,
)


def test_resolve_mid_asof_backfill_days_default() -> None:
    assert resolve_mid_asof_backfill_days() == 30


def test_is_mid_anchor_valid_in_window() -> None:
    assert is_mid_anchor_valid(date(2026, 5, 19), date(2026, 5, 18), n_days=30)
    assert not is_mid_anchor_valid(date(2026, 5, 19), date(2026, 5, 19), n_days=30)
    assert not is_mid_anchor_valid(date(2026, 5, 19), None, n_days=30)
    assert not is_mid_anchor_valid(date(2026, 5, 19), date(2026, 4, 18), n_days=30)


def test_mid_asof_lateral_lower_bound_sql_uses_n() -> None:
    frag = mid_asof_lateral_lower_bound_sql("bw._gday", n_days=30)
    assert "INTERVAL '30' DAY" in frag
    assert "bw._gday" in frag


def test_apply_mid_term_bounded_asof_nulls_outside_window() -> None:
    df = pd.DataFrame(
        {
            "gaming_day": [pd.Timestamp("2026-05-19"), pd.Timestamp("2026-05-19")],
            "anchor_gaming_day": [pd.Timestamp("2026-05-18"), pd.Timestamp("2026-04-01")],
            "fe__bets_cnt__w1d": [2.0, 9.0],
        }
    )
    out = apply_mid_term_bounded_asof(
        df,
        mid_primitive_columns=("fe__bets_cnt__w1d",),
        anchor_column="anchor_gaming_day",
        n_days=30,
    )
    assert float(out.iloc[0]["fe__bets_cnt__w1d"]) == pytest.approx(2.0)
    assert int(out.iloc[0]["mid_term_snapshot_missing_flag"]) == 0
    assert pd.isna(out.iloc[1]["fe__bets_cnt__w1d"])
    assert int(out.iloc[1]["mid_term_snapshot_missing_flag"]) == 1
