"""Unit tests for backtester single-window PAT@1% bridge (precision-uplift orchestrator T10)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from trainer.training.backtester import (
    _apply_pat_at_recall_bridges_for_json_sections,
    _attach_single_window_pat_at_recall_bridge,
    _build_pat_recall_1pct_series_from_gaming_day,
    _flat_section_to_mlflow_metrics,
)


def test_bridge_adds_aligned_series_when_scalar_pat_finite() -> None:
    md: dict = {"test_precision_at_recall_0.01": 0.42}
    _attach_single_window_pat_at_recall_bridge(
        md,
        window_start_iso="2024-01-01T00:00:00+08:00",
        window_end_iso="2024-01-01T06:00:00+08:00",
    )
    assert md["test_precision_at_recall_0.01_by_window"] == [0.42]
    assert md["test_precision_at_recall_0.01_window_ids"] == [
        "2024-01-01T00:00:00+08:00->2024-01-01T06:00:00+08:00"
    ]


def test_bridge_skips_when_by_window_already_set() -> None:
    md: dict = {
        "test_precision_at_recall_0.01": 0.1,
        "test_precision_at_recall_0.01_by_window": [0.2, 0.3],
        "test_precision_at_recall_0.01_window_ids": ["a", "b"],
    }
    _attach_single_window_pat_at_recall_bridge(
        md,
        window_start_iso="s",
        window_end_iso="e",
    )
    assert md["test_precision_at_recall_0.01_by_window"] == [0.2, 0.3]
    assert md["test_precision_at_recall_0.01_window_ids"] == ["a", "b"]


def test_bridge_skips_when_by_window_is_empty_list() -> None:
    md: dict = {
        "test_precision_at_recall_0.01": 0.5,
        "test_precision_at_recall_0.01_by_window": [],
    }
    _attach_single_window_pat_at_recall_bridge(md, window_start_iso="s", window_end_iso="e")
    assert md["test_precision_at_recall_0.01_by_window"] == []
    assert "test_precision_at_recall_0.01_window_ids" not in md


def test_bridge_omits_when_pat_is_none() -> None:
    md: dict = {"test_precision_at_recall_0.01": None}
    _attach_single_window_pat_at_recall_bridge(md, window_start_iso="s", window_end_iso="e")
    assert "test_precision_at_recall_0.01_by_window" not in md


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), float("-inf")],
)
def test_bridge_omits_when_pat_non_finite(bad: float) -> None:
    assert not math.isfinite(bad)
    md: dict = {"test_precision_at_recall_0.01": bad}
    _attach_single_window_pat_at_recall_bridge(md, window_start_iso="s", window_end_iso="e")
    assert "test_precision_at_recall_0.01_by_window" not in md
    assert "test_precision_at_recall_0.01_window_ids" not in md


def test_bridge_coerces_numeric_string() -> None:
    md: dict = {"test_precision_at_recall_0.01": "0.25"}
    _attach_single_window_pat_at_recall_bridge(md, window_start_iso="a", window_end_iso="b")
    assert md["test_precision_at_recall_0.01_by_window"] == [0.25]
    assert md["test_precision_at_recall_0.01_window_ids"] == ["a->b"]


def test_apply_bridges_covers_model_default_and_optuna() -> None:
    results: dict = {
        "window_start": "A",
        "window_end": "B",
        "model_default": {"test_precision_at_recall_0.01": 0.5},
        "optuna": {"test_precision_at_recall_0.01": 0.6},
    }
    _apply_pat_at_recall_bridges_for_json_sections(results)
    assert results["model_default"]["test_precision_at_recall_0.01_by_window"] == [0.5]
    assert results["model_default"]["test_precision_at_recall_0.01_window_ids"] == ["A->B"]
    assert results["optuna"]["test_precision_at_recall_0.01_by_window"] == [0.6]
    assert results["optuna"]["test_precision_at_recall_0.01_window_ids"] == ["A->B"]


def test_apply_bridges_skips_non_dict_section_values() -> None:
    results: dict = {
        "window_start": "s",
        "window_end": "e",
        "model_default": None,
        "optuna": "not-a-dict",
    }
    _apply_pat_at_recall_bridges_for_json_sections(results)
    assert results["model_default"] is None
    assert results["optuna"] == "not-a-dict"


def test_apply_bridges_only_model_default_when_optuna_absent() -> None:
    results: dict = {
        "window_start": "x",
        "window_end": "y",
        "model_default": {"test_precision_at_recall_0.01": 0.11},
    }
    _apply_pat_at_recall_bridges_for_json_sections(results)
    assert results["model_default"]["test_precision_at_recall_0.01_by_window"] == [0.11]
    assert "optuna" not in results


def test_flat_section_to_mlflow_metrics_default_prefix_contract() -> None:
    flat = {
        "test_ap": 0.25,
        "threshold": 0.5,
        "rated_threshold": 0.45,
        "alerts": 12,
        "alerts_per_hour": 3.0,
    }
    out = _flat_section_to_mlflow_metrics(flat)
    assert out["backtest_ap"] == 0.25
    assert out["backtest_threshold"] == 0.5
    assert out["backtest_rated_threshold"] == 0.45
    assert out["backtest_alerts"] == 12
    assert out["backtest_alerts_per_hour"] == 3.0


def test_flat_section_to_mlflow_metrics_supports_optuna_prefix() -> None:
    flat = {"test_ap": 0.99, "threshold": 0.4, "alerts": 9}
    out = _flat_section_to_mlflow_metrics(flat, metric_prefix="backtest_optuna_")
    assert out["backtest_optuna_ap"] == 0.99
    assert out["backtest_optuna_threshold"] == 0.4
    assert out["backtest_optuna_alerts"] == 9
    assert "backtest_ap" not in out


def test_true_multi_window_series_from_gaming_day_returns_aligned_series() -> None:
    rated_sub = pd.DataFrame({
        "gaming_day": ["2026-01-01"] * 10 + ["2026-01-02"] * 10,
        "label": [1] * 5 + [0] * 5 + [1] * 5 + [0] * 5,
        "score": [0.95, 0.9, 0.85, 0.8, 0.75, 0.4, 0.35, 0.3, 0.25, 0.2] * 2,
        "is_rated": [True] * 20,
    })
    out = _build_pat_recall_1pct_series_from_gaming_day(rated_sub)
    assert out is not None
    series, window_ids = out
    assert window_ids == ["2026-01-01", "2026-01-02"]
    assert len(series) == len(window_ids) == 2
    assert all(isinstance(v, float) for v in series)


def test_true_multi_window_series_from_gaming_day_skips_when_missing_column() -> None:
    rated_sub = pd.DataFrame(
        {
            "label": [1, 0],
            "score": [0.7, 0.3],
            "is_rated": [True, True],
        }
    )
    assert _build_pat_recall_1pct_series_from_gaming_day(rated_sub) is None
