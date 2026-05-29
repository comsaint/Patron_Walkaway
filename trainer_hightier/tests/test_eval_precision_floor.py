"""Tests for high-tier precision-floor scoring."""

from __future__ import annotations

import numpy as np

from trainer_hightier.eval import report_alert_rate_at_precision_floor


def test_precision_floor_returns_highest_feasible_threshold() -> None:
    """Among score levels meeting precision ≥ floor, picks maximum threshold (minimal alerts)."""
    y_true = np.array([0, 1, 0, 1, 1], dtype=np.int8)
    y_score = np.array([0.1, 0.95, 0.2, 0.96, 0.97])

    rep = report_alert_rate_at_precision_floor(y_true, y_score, min_precision=0.8)
    assert rep.feasible
    assert rep.alert_count <= len(y_true)
    assert np.isfinite(rep.threshold)
    mask = y_score >= rep.threshold
    prec = float(np.sum(y_true[mask])) / float(max(1, int(np.sum(mask))))
    assert prec >= 0.8 - 1e-9


def test_precision_floor_not_feasible() -> None:
    """No positives → every non-empty alert set has precision 0."""
    y_true = np.zeros(4, dtype=np.int8)
    y_score = np.array([0.9, 0.8, 0.7, 0.6])
    rep = report_alert_rate_at_precision_floor(y_true, y_score, min_precision=0.5)
    assert not rep.feasible
