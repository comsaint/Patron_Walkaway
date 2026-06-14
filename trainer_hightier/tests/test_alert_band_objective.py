"""Unit tests for alert-band precision objective helpers."""

from __future__ import annotations

import pandas as pd
import pytest

from trainer_hightier.evaluation.alert_band_objective import (
    AlertBandEvaluation,
    OperationalCapacityPoint,
    alert_band_meta_dict,
    alert_band_scalar_score,
    evaluate_alert_band_on_candidates,
    target_alert_count,
    threshold_for_target_operational_alerts,
)
from trainer_hightier.evaluation.player_alert_policy import (
    ALERT_TS_COLUMN,
    LABEL_COLUMN,
    PLAYER_ID_COLUMN,
    SCORE_COLUMN,
)


def _candidates(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if "game_id" not in frame.columns:
        frame["game_id"] = 100.0
    return frame


def test_target_alert_count_rounds_up_for_short_window() -> None:
    """Short windows still get at least one alert budget."""

    assert target_alert_count(0.5, 1.0) == 1
    assert target_alert_count(10.0, 2.0) == 20


def test_alert_band_scalar_score_prefers_balanced_band() -> None:
    """Lexicographic rule encoded: min precision dominates mean precision."""

    balanced = alert_band_scalar_score({1.0: 0.5, 2.0: 0.48}, recall_at_primary=0.2)
    skewed = alert_band_scalar_score({1.0: 0.2, 2.0: 0.8}, recall_at_primary=0.2)
    assert balanced > skewed


def test_threshold_for_target_operational_alerts_respects_budget() -> None:
    """Binary search should approximate the requested operational alert count."""

    candidates = _candidates(
        [
            {
                PLAYER_ID_COLUMN: 1,
                SCORE_COLUMN: 0.95,
                LABEL_COLUMN: 1,
                ALERT_TS_COLUMN: pd.Timestamp("2026-06-01 10:00:00"),
            },
            {
                PLAYER_ID_COLUMN: 2,
                SCORE_COLUMN: 0.85,
                LABEL_COLUMN: 0,
                ALERT_TS_COLUMN: pd.Timestamp("2026-06-01 10:05:00"),
            },
            {
                PLAYER_ID_COLUMN: 3,
                SCORE_COLUMN: 0.75,
                LABEL_COLUMN: 1,
                ALERT_TS_COLUMN: pd.Timestamp("2026-06-01 10:10:00"),
            },
        ]
    )
    pt = threshold_for_target_operational_alerts(
        candidates,
        2,
        window_hours=1.0,
        requested_alerts_per_hour=2.0,
        split_prefix="val",
    )
    assert pt.alerts == pytest.approx(2, abs=1)
    assert 0.0 <= pt.precision <= 1.0


def test_evaluate_alert_band_on_candidates_returns_scalar() -> None:
    """Band evaluation produces deployment threshold and scalar score."""

    candidates = _candidates(
        [
            {
                PLAYER_ID_COLUMN: i,
                SCORE_COLUMN: 1.0 - i * 0.1,
                LABEL_COLUMN: 1 if i == 0 else 0,
                ALERT_TS_COLUMN: pd.Timestamp(f"2026-06-01 10:{i:02d}:00"),
            }
            for i in range(5)
        ]
    )
    band = evaluate_alert_band_on_candidates(
        candidates,
        window_hours=2.0,
        target_alerts_per_hour=(1.0, 2.0),
        deployment_target_alerts_per_hour=1.0,
        split_prefix="val",
    )
    assert len(band.points) == 2
    assert band.scalar_score > -1.0
    assert band.deployment_threshold == pytest.approx(band.points[0].threshold)


def test_alert_band_meta_dict_serializes_points() -> None:
    """Refactored metadata helper must round-trip band evaluation for reporting."""
    pt = OperationalCapacityPoint(
        target_alerts_per_hour=1.0,
        target_alert_count=10,
        threshold=0.42,
        precision=0.55,
        recall=0.12,
        alerts=10,
        alerts_per_hour=1.0,
        true_positives=5,
    )
    band = AlertBandEvaluation(
        scalar_score=57.0,
        deployment_target_alerts_per_hour=1.0,
        deployment_threshold=0.42,
        points=(pt,),
        min_precision=0.55,
        mean_precision=0.55,
    )
    got = alert_band_meta_dict(
        band,
        deployment_target_alerts_per_hour=1.0,
        target_alerts_per_hour=(1.0, 2.0),
    )
    assert got["scalar_score"] == pytest.approx(57.0)
    assert got["target_alerts_per_hour"] == [1.0, 2.0]
    assert got["points"][0]["precision"] == pytest.approx(0.55)
    assert got["points"][0]["true_positives"] == 5
