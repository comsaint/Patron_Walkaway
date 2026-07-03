"""Unit tests for Time-CV fold-level operational metric extraction."""

from __future__ import annotations

import pytest

from trainer_hightier.feature_experiment.time_cv.metrics import (
    delta_p1hr_pp,
    fold_metrics_from_report,
    precision_to_pp,
    val_p1hr_precision_from_report,
)


def test_val_p1hr_from_alert_band_points() -> None:
    report = {
        "step5_val_alert_band": {
            "points": [
                {"target_alerts_per_hour": 2.0, "precision": 0.4},
                {"target_alerts_per_hour": 1.0, "precision": 0.62},
            ]
        }
    }
    assert val_p1hr_precision_from_report(report) == pytest.approx(0.62)


def test_val_p1hr_from_flat_key_fallback() -> None:
    report = {"val_op_precision_at_1p0_alerts_per_hour": 0.55}
    assert val_p1hr_precision_from_report(report) == pytest.approx(0.55)


def test_val_p1hr_from_pick_and_deploy_target_fallback() -> None:
    report = {
        "step5_val_precision_at_pick": 0.48,
        "step5_deployment_target_alerts_per_hour": 1.0,
    }
    assert val_p1hr_precision_from_report(report) == pytest.approx(0.48)


def test_val_p1hr_returns_none_when_unavailable() -> None:
    assert val_p1hr_precision_from_report({}) is None
    assert val_p1hr_precision_from_report(
        {"step5_val_precision_at_pick": 0.5, "step5_deployment_target_alerts_per_hour": 2.0}
    ) is None


def test_precision_to_pp_converts_probability() -> None:
    assert precision_to_pp(0.62) == pytest.approx(62.0)
    assert precision_to_pp(None) is None
    assert precision_to_pp(float("nan")) is None


def test_fold_metrics_from_report_populates_delta_fields() -> None:
    report = {
        "step5_val_alert_band": {
            "points": [{"target_alerts_per_hour": 1.0, "precision": 0.5}]
        },
        "val_ap": 0.7,
        "step5_val_recall_at_pick": 0.3,
    }
    got = fold_metrics_from_report(report, fold_idx=1, arm_id="arm_a")
    assert got.fold_idx == 1
    assert got.arm_id == "arm_a"
    assert got.val_p1hr_precision == pytest.approx(0.5)
    assert got.val_p1hr_precision_pp == pytest.approx(50.0)
    assert got.val_ap == pytest.approx(0.7)
    assert got.val_recall_at_pick == pytest.approx(0.3)


def test_delta_p1hr_pp_baseline_minus_arm() -> None:
    baseline = {"val_op_precision_at_1p0_alerts_per_hour": 0.5}
    arm = {"val_op_precision_at_1p0_alerts_per_hour": 0.55}
    assert delta_p1hr_pp(baseline, arm) == pytest.approx(5.0)
