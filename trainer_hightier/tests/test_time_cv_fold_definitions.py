"""Unit tests for Time-CV fold generation and decision framework."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from trainer_hightier.config import FeatureSelectionTimeCvConfig
from trainer_hightier.feature_experiment.time_cv.fold_definitions import (
    GAMING_DAY_COLUMN,
    generate_expanding_folds,
    unique_gaming_days_from_series,
)
from trainer_hightier.feature_experiment.time_cv.report import (
    aggregate_arm_decision,
    feature_pruning_decision_from_loo,
    should_early_stop_strong_drop,
)


def _day_range(start: date, count: int) -> tuple[date, ...]:
    return tuple(start + timedelta(days=i) for i in range(count))


def test_generate_expanding_folds_three_by_thirty() -> None:
    days = _day_range(date(2026, 1, 1), 180)
    folds = generate_expanding_folds(
        days,
        n_folds=3,
        val_window_days=30,
        min_train_days=90,
    )
    assert len(folds) == 3
    assert folds[0].train_n_days == 90
    assert folds[0].val_n_days == 30
    assert folds[0].val_start == days[90]
    assert folds[2].val_end == days[179]
    assert folds[2].train_end == folds[2].val_start - timedelta(days=1)


def test_generate_expanding_folds_raises_when_insufficient_days() -> None:
    days = _day_range(date(2026, 1, 1), 100)
    with pytest.raises(ValueError, match="need at least"):
        generate_expanding_folds(days, n_folds=3, val_window_days=30, min_train_days=90)


def test_aggregate_arm_decision_keep() -> None:
    cfg = FeatureSelectionTimeCvConfig()
    decision = aggregate_arm_decision((1.5, 1.2, 1.1), arm_id="arm_a", cfg=cfg)
    assert decision.decision == "KEEP"
    assert decision.mean_delta_p1hr_pp == pytest.approx(1.266666, rel=1e-3)


def test_aggregate_arm_decision_strong_drop() -> None:
    cfg = FeatureSelectionTimeCvConfig()
    decision = aggregate_arm_decision((-0.2, -0.5, -0.1), arm_id="arm_b", cfg=cfg)
    assert decision.decision == "STRONG_DROP"


def test_aggregate_arm_decision_marginal() -> None:
    cfg = FeatureSelectionTimeCvConfig()
    decision = aggregate_arm_decision((0.2, 0.5, 0.0), arm_id="arm_c", cfg=cfg)
    assert decision.decision == "MARGINAL"


def test_should_early_stop_strong_drop() -> None:
    assert should_early_stop_strong_drop((-0.1, -0.2, 0.5), early_stop_folds=3) is False
    assert should_early_stop_strong_drop((-0.1, -0.2, -0.3), early_stop_folds=3) is True


def test_feature_pruning_decision_inverts_loo_semantics() -> None:
    cfg = FeatureSelectionTimeCvConfig()
    harmful = aggregate_arm_decision((1.5, 1.2, 1.1), arm_id="loo__bad", cfg=cfg)
    useful = aggregate_arm_decision((-0.2, -0.5, -0.1), arm_id="loo__good", cfg=cfg)
    assert feature_pruning_decision_from_loo(harmful) == "STRONG_DROP_FEATURE"
    assert feature_pruning_decision_from_loo(useful) == "KEEP_FEATURE"


def test_unique_gaming_days_from_series_coerces_and_dedupes() -> None:
    series = pd.Series(
        [
            date(2026, 1, 1),
            datetime(2026, 1, 2, 12, 0),
            "2026-01-03",
            None,
            float("nan"),
            date(2026, 1, 1),
        ],
        name=GAMING_DAY_COLUMN,
    )
    assert unique_gaming_days_from_series(series) == (
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
    )


def test_unique_gaming_days_from_series_empty_returns_empty_tuple() -> None:
    assert unique_gaming_days_from_series(pd.Series([], dtype=object)) == ()


def test_unique_gaming_days_from_series_raises_when_all_unparseable() -> None:
    with pytest.raises(ValueError, match="no parseable dates"):
        unique_gaming_days_from_series(pd.Series([None, "not-a-date"]))
