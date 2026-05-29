"""Tests for unified short-term PIT scoring context."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.serving.short_term_scoring_context import (
    DEFAULT_EXPAND_CANONICAL_ALIASES,
    ShortTermScoringContext,
    default_short_term_scoring_context,
    sort_bets_for_scoring_batch,
    split_short_term_column_names,
)


def test_default_expand_canonical_aliases_false() -> None:
    ctx = ShortTermScoringContext()
    assert ctx.expand_canonical_aliases is False
    assert DEFAULT_EXPAND_CANONICAL_ALIASES is False


def test_batch_size_from_serving_config() -> None:
    from trainer_hightier.config import default_hightier_serving_config

    cfg = default_hightier_serving_config()
    ctx = default_short_term_scoring_context(cfg)
    assert ctx.batch_size == int(cfg.hightier_scorer_max_bets_per_cycle)


def test_sort_bets_chronological() -> None:
    bets = pd.DataFrame(
        {
            "bet_id": [3.0, 1.0, 2.0],
            "payout_complete_dtm": pd.to_datetime(
                ["2024-06-01 12:00:00", "2024-06-01 10:00:00", "2024-06-01 11:00:00"],
                utc=True,
            ),
        },
    )
    out = sort_bets_for_scoring_batch(bets)
    assert out["bet_id"].tolist() == [1.0, 2.0, 3.0]


def test_split_short_term_column_names() -> None:
    trial, fe = split_short_term_column_names(
        ("bet__bets_cnt__w1h", "fe__wager_sum__w15m", "wager"),
    )
    assert trial == ("bet__bets_cnt__w1h",)
    assert fe == ("fe__wager_sum__w15m",)


def test_context_rejects_invalid_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        ShortTermScoringContext(batch_size=0)


def test_per_bet_scoring_bounds_batch_invariant_for_fe_w15m() -> None:
    """Co-batching an earlier bet must not change fe__ for a later scoring bet."""
    from trainer_hightier.config import default_hightier_serving_config
    from trainer_hightier.feature_experiment.materialize_fe_derived import (
        compute_fe_derived_features_from_pool,
    )
    from trainer_hightier.serving.scorer import compute_scoring_bounds_for_bets

    hk = "Asia/Hong_Kong"
    day = pd.Timestamp("2025-06-01").date()
    t_early = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    t_late = pd.Timestamp("2025-06-01 17:00:00", tz=hk)
    pool = pd.DataFrame(
        {
            "bet_id": [100.0, 200.0],
            "player_id": [10, 10],
            "canonical_id": ["c10", "c10"],
            "session_id": [1, 1],
            "table_id": [1, 1],
            "gaming_day_event": pd.to_datetime([day, day]),
            "payout_complete_dtm": [t_early, t_late],
            "wager": [5000.0, 7000.0],
            "payout_odds": [2.0, 2.0],
            "casino_win": [0.0, 0.0],
        },
    )
    cfg = default_hightier_serving_config()
    late_only = pd.DataFrame(
        {
            "bet_id": [200.0],
            "player_id": [10],
            "canonical_id": ["c10"],
            "payout_complete_dtm": [t_late],
            "gaming_day_event": [day],
        },
    )
    both = pd.DataFrame(
        {
            "bet_id": [100.0, 200.0],
            "player_id": [10, 10],
            "canonical_id": ["c10", "c10"],
            "payout_complete_dtm": [t_early, t_late],
            "gaming_day_event": [day, day],
        },
    )
    solo = compute_fe_derived_features_from_pool(
        pool,
        pd.Series([200.0]),
        scoring_bounds=compute_scoring_bounds_for_bets(late_only, cfg=cfg),
    )
    batch = compute_fe_derived_features_from_pool(
        pool,
        pd.Series([200.0]),
        scoring_bounds=compute_scoring_bounds_for_bets(both, cfg=cfg),
    )
    assert float(solo.loc[solo.bet_id == 200.0, "fe__wager_sum__w15m"].iloc[0]) == pytest.approx(
        float(batch.loc[batch.bet_id == 200.0, "fe__wager_sum__w15m"].iloc[0]),
    )
