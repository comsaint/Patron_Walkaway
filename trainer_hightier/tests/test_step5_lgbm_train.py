"""Unit tests for Step 5 LightGBM training helpers."""

from __future__ import annotations

import importlib

import numpy as np
import pytest

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")
pick_threshold_precision_floor = _b5.pick_threshold_precision_floor


def test_pick_threshold_feasible_prefers_max_recall() -> None:
    """Among precision >= floor, choose operating point with highest recall."""

    y = np.array([1, 0, 1, 1, 0], dtype=np.int8)
    scores = np.array([0.9, 0.8, 0.7, 0.6, 0.5], dtype=np.float64)
    rep = pick_threshold_precision_floor(y, scores, min_precision=0.5)
    assert rep.feasible
    assert rep.recall == pytest.approx(1.0)
    assert rep.alert_count == 4


def test_pick_threshold_infeasible_best_precision() -> None:
    """When floor unreachable, maximize precision then recall."""

    y = np.array([0, 1, 0, 0], dtype=np.int8)
    scores = np.array([0.99, 0.5, 0.4, 0.3], dtype=np.float64)
    rep = pick_threshold_precision_floor(y, scores, min_precision=0.95)
    assert not rep.feasible
    assert rep.precision == pytest.approx(0.5)
    assert rep.recall == pytest.approx(1.0)
    assert rep.alert_count == 2


def test_split_metrics_block_true_labels_per_hour() -> None:
    """``true_labels_per_hour`` = positive count / window_hours (baseline label density)."""

    y = np.array([1, 0, 1, 0, 0], dtype=np.int8)
    scores = np.array([0.9, 0.1, 0.8, 0.2, 0.3], dtype=np.float64)
    block = _b5._split_metrics_block("val", y, scores, threshold=0.5, window_hours=10.0)
    assert block["val_positives"] == 2
    assert block["val_true_labels_per_hour"] == pytest.approx(0.2)
    assert block["val_alerts_per_hour"] == pytest.approx(0.2)


def test_split_metrics_block_omits_per_hour_when_window_invalid() -> None:
    """Invalid ``window_hours`` leaves density fields as ``None``."""

    y = np.array([1, 0], dtype=np.int8)
    scores = np.array([0.9, 0.1], dtype=np.float64)
    block = _b5._split_metrics_block("train", y, scores, threshold=0.5, window_hours=None)
    assert block["train_alerts_per_hour"] is None
    assert block["train_true_labels_per_hour"] is None


def test_pick_threshold_all_negative_early_exit() -> None:
    """No positives: degenerate result without scanning prefixes."""

    y = np.zeros(5, dtype=np.int8)
    scores = np.linspace(0.2, 1.0, 5).astype(np.float64)
    rep = pick_threshold_precision_floor(y, scores, min_precision=0.8)
    assert not rep.feasible
    assert rep.alert_count == 0

