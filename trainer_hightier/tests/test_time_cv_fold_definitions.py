"""Unit tests for Time-CV fold generation and decision framework."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from trainer_hightier.config import FeatureSelectionTimeCvConfig
from trainer_hightier.feature_experiment.time_cv.fold_definitions import generate_expanding_folds
from trainer_hightier.feature_experiment.time_cv.metrics import (
    delta_p1hr_pp,
    fold_metrics_from_report,
    precision_to_pp,
    val_p1hr_precision_from_report,
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


def test_val_p1hr_precision_from_alert_band_points() -> None:
    report = {
        "step5_val_alert_band": {
            "points": [
                {"target_alerts_per_hour": 2.0, "precision": 0.5},
                {"target_alerts_per_hour": 1.0, "precision": 0.31},
            ]
        }
    }
    assert val_p1hr_precision_from_report(report) == pytest.approx(0.31)


def test_val_p1hr_precision_from_flat_key_fallback() -> None:
    report = {"val_op_precision_at_1p0_alerts_per_hour": 0.27}
    assert val_p1hr_precision_from_report(report) == pytest.approx(0.27)


def test_val_p1hr_precision_from_pick_and_deploy_fallback() -> None:
    report = {
        "step5_val_precision_at_pick": 0.33,
        "step5_deployment_target_alerts_per_hour": 1.0,
    }
    assert val_p1hr_precision_from_report(report) == pytest.approx(0.33)


def test_val_p1hr_precision_returns_none_when_unavailable() -> None:
    assert val_p1hr_precision_from_report({}) is None
    assert val_p1hr_precision_from_report(
        {"step5_val_precision_at_pick": 0.2, "step5_deployment_target_alerts_per_hour": 2.0}
    ) is None


def test_fold_metrics_and_delta_p1hr_pp() -> None:
    baseline = {"val_op_precision_at_1p0_alerts_per_hour": 0.20}
    arm = {"val_op_precision_at_1p0_alerts_per_hour": 0.25, "val_ap": 0.4}
    metrics = fold_metrics_from_report(arm, fold_idx=2, arm_id="arm_x")
    assert metrics.fold_idx == 2
    assert metrics.arm_id == "arm_x"
    assert metrics.val_p1hr_precision == pytest.approx(0.25)
    assert metrics.val_p1hr_precision_pp == pytest.approx(25.0)
    assert metrics.val_ap == pytest.approx(0.4)
    assert delta_p1hr_pp(baseline, arm) == pytest.approx(5.0)
    assert precision_to_pp(None) is None
