"""Unit tests for trainer.training.threshold_selection.pick_threshold_dec026 (DEC-026 / DEC-032)."""

from __future__ import annotations

import numpy as np
import pytest

from trainer.training.threshold_selection import (
    Dec026ThresholdPick,
    dec026_pr_alert_arrays,
    dec026_sanitize_per_hour_params,
    pick_threshold_dec026,
    pick_threshold_dec026_from_pr_arrays,
)


def _legacy_pick(
    y_true: np.ndarray,
    y_score: np.ndarray,
    *,
    recall_floor: float | None,
    min_alert_count: int,
    min_alerts_per_hour: float | None = None,
    window_hours: float | None = None,
    fbeta_beta: float = 0.5,
) -> Dec026ThresholdPick:
    """Compose PR arrays + sanitize + mask (must match pick_threshold_dec026)."""
    prep = dec026_pr_alert_arrays(y_true, y_score)
    if prep is None:
        return Dec026ThresholdPick(0.5, 0.0, 0.0, 0.0, 0.0, True)
    pr_p, pr_r, pr_th, ac, _n = prep
    wh_eff, mah_eff = dec026_sanitize_per_hour_params(window_hours, min_alerts_per_hour)
    return pick_threshold_dec026_from_pr_arrays(
        pr_p,
        pr_r,
        pr_th,
        ac,
        recall_floor=recall_floor,
        min_alert_count=min_alert_count,
        min_alerts_per_hour=mah_eff,
        window_hours=wh_eff,
        fbeta_beta=fbeta_beta,
    )


@pytest.mark.parametrize("seed", [0, 1, 42])
def test_pick_matches_legacy_random_binary(seed: int) -> None:
    rng = np.random.default_rng(seed)
    n = 80
    y = rng.integers(0, 2, size=n).astype(float)
    if y.sum() in (0, n):
        y[0] = 0.0
        y[1] = 1.0
    s = rng.random(n)
    for rf in (0.001, 0.01, 0.1, None):
        for mac in (1, 3, 5):
            for mah, wh in ((None, None), (0.5, 2.0), (10.0, 1.0)):
                a = pick_threshold_dec026(
                    y, s, recall_floor=rf, min_alert_count=mac,
                    min_alerts_per_hour=mah, window_hours=wh, fbeta_beta=0.5,
                )
                b = _legacy_pick(
                    y, s, recall_floor=rf, min_alert_count=mac,
                    min_alerts_per_hour=mah, window_hours=wh, fbeta_beta=0.5,
                )
                assert a == b, (seed, rf, mac, mah, wh)


def test_empty_and_nan_fallback() -> None:
    assert pick_threshold_dec026(
        np.array([]), np.array([]), recall_floor=0.01, min_alert_count=1,
    ).is_fallback
    assert pick_threshold_dec026(
        np.array([0.0, 1.0]), np.array([0.3, float("nan")]),
        recall_floor=0.01, min_alert_count=1,
    ).is_fallback


def test_min_alerts_per_hour_skipped_when_window_hours_none() -> None:
    y = np.array([0, 0, 0, 0, 1, 1], dtype=float)
    s = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.9], dtype=float)
    a = pick_threshold_dec026(
        y, s, recall_floor=0.01, min_alert_count=1,
        min_alerts_per_hour=1e9, window_hours=None,
    )
    b = pick_threshold_dec026(
        y, s, recall_floor=0.01, min_alert_count=1,
        min_alerts_per_hour=None, window_hours=None,
    )
    assert a == b


def test_min_alerts_per_hour_applied_with_positive_window() -> None:
    y = np.array([0, 1, 0, 1, 0, 1], dtype=float)
    s = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.9], dtype=float)
    loose = pick_threshold_dec026(
        y, s, recall_floor=0.001, min_alert_count=1,
        min_alerts_per_hour=0.1, window_hours=1.0,
    )
    strict = pick_threshold_dec026(
        y, s, recall_floor=0.001, min_alert_count=1,
        min_alerts_per_hour=100.0, window_hours=1.0,
    )
    assert not loose.is_fallback
    assert strict.is_fallback
