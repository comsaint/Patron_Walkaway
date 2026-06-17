"""Unit tests for alert-band precision objective helpers."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from trainer_hightier.evaluation.alert_band_objective import (
    alert_band_meta_dict,
    alert_band_metrics_block,
    alert_band_scalar_score,
    evaluate_alert_band_on_candidates,
    operational_threshold_picks_for_targets,
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


def test_alert_band_meta_dict_matches_evaluate_output() -> None:
    """Step 5 persists ``alert_band_meta_dict``; fields must mirror band evaluation."""

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
    meta = alert_band_meta_dict(
        band,
        deployment_target_alerts_per_hour=1.0,
        target_alerts_per_hour=(1.0, 2.0),
    )
    assert meta["scalar_score"] == pytest.approx(band.scalar_score)
    assert meta["min_precision"] == pytest.approx(band.min_precision)
    assert meta["mean_precision"] == pytest.approx(band.mean_precision)
    assert meta["deployment_target_alerts_per_hour"] == 1.0
    assert meta["target_alerts_per_hour"] == [1.0, 2.0]
    assert len(meta["points"]) == len(band.points)
    for got_pt, src_pt in zip(meta["points"], band.points, strict=True):
        assert got_pt["target_alerts_per_hour"] == pytest.approx(src_pt.target_alerts_per_hour)
        assert got_pt["threshold"] == pytest.approx(src_pt.threshold)
        assert got_pt["precision"] == pytest.approx(src_pt.precision)
        assert got_pt["alerts"] == src_pt.alerts


def test_alert_band_metrics_block_emits_flat_slug_keys() -> None:
    """Legacy flat report keys remain stable for downstream dashboards."""

    candidates = _candidates(
        [
            {
                PLAYER_ID_COLUMN: 1,
                SCORE_COLUMN: 0.9,
                LABEL_COLUMN: 1,
                ALERT_TS_COLUMN: pd.Timestamp("2026-06-01 10:00:00"),
            },
            {
                PLAYER_ID_COLUMN: 2,
                SCORE_COLUMN: 0.4,
                LABEL_COLUMN: 0,
                ALERT_TS_COLUMN: pd.Timestamp("2026-06-01 10:30:00"),
            },
        ]
    )
    block = alert_band_metrics_block(
        "val",
        candidates,
        window_hours=1.0,
        target_alerts_per_hour=(1.0, 2.0),
    )
    assert "val_alert_band_scalar_score" in block
    assert "val_op_precision_at_1_alerts_per_hour" in block
    assert "val_op_precision_at_2_alerts_per_hour" in block
    assert 0.0 <= block["val_op_precision_at_1_alerts_per_hour"] <= 1.0


def test_threshold_for_target_operational_alerts_handles_empty_inputs() -> None:
    """Empty or non-scorable candidates must not raise during threshold search."""

    empty = threshold_for_target_operational_alerts(
        _candidates([]),
        1,
        window_hours=1.0,
        requested_alerts_per_hour=1.0,
        split_prefix="val",
    )
    assert math.isnan(empty.threshold)
    assert empty.alerts == 0

    non_finite = _candidates(
        [
            {
                PLAYER_ID_COLUMN: 1,
                SCORE_COLUMN: float("nan"),
                LABEL_COLUMN: 0,
                ALERT_TS_COLUMN: pd.Timestamp("2026-06-01 10:00:00"),
            },
        ]
    )
    bad = threshold_for_target_operational_alerts(
        non_finite,
        1,
        window_hours=1.0,
        requested_alerts_per_hour=1.0,
        split_prefix="val",
    )
    assert math.isnan(bad.threshold)
    assert bad.alerts == 0


def test_operational_threshold_picks_for_targets() -> None:
    """Frontier studies reuse shared capacity threshold picks."""

    candidates = _candidates(
        [
            {
                PLAYER_ID_COLUMN: i,
                SCORE_COLUMN: 1.0 - i * 0.2,
                LABEL_COLUMN: 1 if i == 0 else 0,
                ALERT_TS_COLUMN: pd.Timestamp(f"2026-06-01 10:{i:02d}:00"),
            }
            for i in range(4)
        ]
    )
    picks = operational_threshold_picks_for_targets(
        candidates,
        window_hours=1.0,
        target_alerts_per_hour=(1.0,),
        split_prefix="val",
        name_prefix="band",
    )
    assert len(picks) == 1
    name, thr = picks[0]
    assert name == "band_1hr"
    assert math.isfinite(thr)
