"""Tests for feature_experiment val slice helpers."""

from __future__ import annotations

import numpy as np

from trainer_hightier.feature_experiment.val_slices import per_slice_recall_at_threshold


def test_per_slice_recall_at_threshold_basic() -> None:
    """Recall within mask matches hand-count at fixed threshold."""

    y = np.array([1, 0, 1, 1, 0], dtype=np.int8)
    s = np.array([0.9, 0.1, 0.4, 0.8, 0.2])
    m0 = np.array([True, True, True, False, False])
    m1 = np.array([False, False, False, True, True])
    thr = 0.5
    out = per_slice_recall_at_threshold(y, s, thr, [m0, m1])
    # slice0: positives at idx 0,2 → only idx 0 has score>=0.5 → recall 0.5
    assert out[0] == 0.5
    # slice1: one positive idx 3 → score 0.8 >= 0.5 → recall 1.0
    assert out[1] == 1.0


def test_per_slice_recall_nan_without_positives() -> None:
    """Slice with no positives yields NaN recall."""

    y = np.array([0, 0, 1], dtype=np.int8)
    s = np.array([0.9, 0.8, 0.7])
    m = np.array([True, True, False])
    out = per_slice_recall_at_threshold(y, s, 0.5, [m])
    assert len(out) == 1
    assert np.isnan(out[0])
