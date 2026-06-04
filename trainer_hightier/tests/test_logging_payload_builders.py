"""Tests for high-tier ``run_summary`` / ``metrics_detailed`` / ``pipeline_debug`` builders."""

from __future__ import annotations

import importlib

import pytest

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    OptunaFloatParamRange,
    OptunaIntParamRange,
    Step5TrainConfig,
)
from trainer_hightier.trainer import (
    HighTierTrainArgs,
    build_metrics_detailed,
    build_pipeline_debug,
    build_run_summary,
)

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")


def test_step5_optuna_search_defaults_are_regularized() -> None:
    """Default search bounds cap tree depth and enforce non-trivial L1/L2."""

    search = Step5TrainConfig().optuna_search
    assert search.learning_rate == OptunaFloatParamRange(0.02, 0.15, log=True)
    assert search.num_leaves == OptunaIntParamRange(16, 31)
    assert search.max_depth == OptunaIntParamRange(4, 10)
    assert search.min_child_samples == OptunaIntParamRange(50, 250)
    assert search.subsample == OptunaFloatParamRange(0.6, 0.9)
    assert search.colsample_bytree == OptunaFloatParamRange(0.6, 0.9)
    assert search.reg_alpha == OptunaFloatParamRange(0.0, 1.0)
    assert search.reg_lambda == OptunaFloatParamRange(0.1, 5.0, log=True)


def test_step5_baseline_params_inside_optuna_search() -> None:
    """Baseline (--skip-optuna) defaults sit inside the Optuna search box."""

    cfg = Step5TrainConfig()
    search = cfg.optuna_search
    assert search.learning_rate.low <= cfg.baseline_learning_rate <= search.learning_rate.high
    assert search.num_leaves.low <= cfg.baseline_num_leaves <= search.num_leaves.high
    assert search.max_depth.low <= cfg.baseline_max_depth <= search.max_depth.high
    assert search.min_child_samples.low <= cfg.baseline_min_child_samples <= search.min_child_samples.high
    assert search.subsample.low <= cfg.baseline_subsample <= search.subsample.high
    assert search.colsample_bytree.low <= cfg.baseline_colsample_bytree <= search.colsample_bytree.high
    assert search.reg_alpha.low <= cfg.baseline_reg_alpha <= search.reg_alpha.high
    assert search.reg_lambda.low <= cfg.baseline_reg_lambda <= search.reg_lambda.high


def test_baseline_lgb_params_comes_from_config_only() -> None:
    """Step 5 baseline kwargs are assembled from config SSOT (no script literals)."""

    cfg = Step5TrainConfig()
    params = _b5._baseline_lgb_params(cfg, seed=7)
    assert params["objective"] == cfg.lgb_fixed.objective
    assert params["metric"] == cfg.lgb_fixed.metric
    assert params["verbosity"] == cfg.lgb_fixed.verbosity
    assert params["n_jobs"] == cfg.lgb_fixed.n_jobs
    assert params["n_estimators"] == cfg.lgb_n_estimators_cap
    assert params["learning_rate"] == cfg.baseline_learning_rate
    assert params["max_depth"] == cfg.baseline_max_depth
    assert params["reg_alpha"] == cfg.baseline_reg_alpha
    assert params["reg_lambda"] == cfg.baseline_reg_lambda
    assert params["random_state"] == 7


def test_step5_optuna_search_rejects_invalid_range() -> None:
    """Invalid low/high pairs fail fast at config construction."""

    with pytest.raises(ValueError, match="OptunaIntParamRange"):
        OptunaIntParamRange(96, 16)
    with pytest.raises(ValueError, match="OptunaFloatParamRange"):
        OptunaFloatParamRange(1.0, 0.5)


def test_optuna_stopping_reason_enums() -> None:
    """Minimal stopping-reason classification for logging."""

    assert (
        _b5._optuna_stopping_reason(wall_sec=3600.0, timeout_sec=3600.0, n_trials_total=10, n_completed=3)
        == "time_budget_exhausted"
    )
    assert (
        _b5._optuna_stopping_reason(wall_sec=10.0, timeout_sec=3600.0, n_trials_total=8, n_completed=0)
        == "no_completed_trials"
    )
    assert (
        _b5._optuna_stopping_reason(wall_sec=100.0, timeout_sec=3600.0, n_trials_total=5, n_completed=5)
        == "completed"
    )
    assert (
        _b5._optuna_stopping_reason(wall_sec=100.0, timeout_sec=3600.0, n_trials_total=8, n_completed=4)
        == "unknown"
    )


def test_build_run_summary_derives_patron_ratio_from_adt_quantile(tmp_path) -> None:
    """When ADT filtering is on, approximate segment fraction is ``1 - theo_train_quantile``."""

    args = HighTierTrainArgs(
        output_dir=tmp_path,
        duckdb_runtime=DuckDbRuntimeConfig(),
        filter_bets_by_adt_quantile=True,
        objective=HighTierObjectiveConfig(theo_train_quantile=0.99, min_precision=0.6),
        step5=Step5TrainConfig(run_step5=False),
    )
    metrics = {
        "model_version": "mv-test",
        "start_epoch_ms": 1_700_000_000_000,
        "finish_epoch_ms": 1_700_000_060_000,
        "run_training_total_seconds": 60.0,
        "step5_threshold": 0.5,
        "step5_optuna_skipped": True,
        "step5_feature_columns": ["a", "b"],
        "optuna_max_time_sec_configured": 3600.0,
        "optuna_max_trials_configured": None,
        "optuna_wall_time_sec_actual": None,
        "optuna_trials_completed": 0,
        "optuna_trials_total": 0,
        "optuna_stopping_reason": "optuna_skipped",
        "optuna_best_value": None,
        "val_ap": 0.5,
        "val_precision": 0.6,
        "val_recall": 0.1,
        "val_f1": 0.17,
        "val_samples": 100,
        "val_positives": 30,
        "val_alerts": 10,
        "val_alerts_per_hour": 1.0,
        "test_ap": 0.49,
        "test_precision": 0.59,
        "test_recall": 0.09,
        "test_f1": 0.16,
        "test_samples": 80,
        "test_positives": 24,
        "test_alerts": 9,
        "test_alerts_per_hour": 0.9,
    }
    rs = build_run_summary(metrics, args)
    assert rs["data_scope"]["patron_sampling_ratio"] == pytest.approx(0.01)
    assert rs["data_scope"]["patron_sampling_ratio_source"] == "adt_quantile_derived"
    assert rs["optimization"]["enabled"] is False


def test_build_run_summary_includes_split_periods(tmp_path) -> None:
    """``step4_split_periods`` from metrics appears in run_summary and metrics_detailed."""

    args = HighTierTrainArgs(
        output_dir=tmp_path,
        duckdb_runtime=DuckDbRuntimeConfig(),
        step5=Step5TrainConfig(run_step5=False),
    )
    periods = {
        "basis": "gaming_day_event",
        "train_day_fraction": 0.7,
        "val_day_fraction": 0.15,
        "distinct_gaming_days": 100,
        "by_split": {
            "train": {"min_gaming_day": "2024-01-01", "max_gaming_day": "2024-08-01", "row_count": 1000},
            "val": {"min_gaming_day": "2024-08-02", "max_gaming_day": "2024-09-01", "row_count": 200},
            "test": {"min_gaming_day": "2024-09-02", "max_gaming_day": "2024-10-01", "row_count": 150},
        },
    }
    metrics = {
        "model_version": "mv-sp",
        "step4_split_periods": periods,
        "step5_optuna_skipped": True,
        "step5_threshold": 0.5,
        "optuna_max_time_sec_configured": 1.0,
        "optuna_max_trials_configured": None,
        "optuna_wall_time_sec_actual": None,
        "optuna_trials_completed": 0,
        "optuna_trials_total": 0,
        "optuna_stopping_reason": "optuna_skipped",
        "optuna_best_value": None,
        "val_ap": 0.5,
        "val_precision": 0.6,
        "test_ap": 0.49,
        "test_precision": 0.59,
    }
    rs = build_run_summary(metrics, args)
    assert rs["split_periods"] == periods
    md = build_metrics_detailed(metrics)
    assert md["split_periods"] == periods


def test_build_metrics_detailed_and_pipeline_debug_smoke() -> None:
    """Smoke nested payloads from flat Step 5 metrics."""

    metrics = {
        "model_version": "mv2",
        "train_ap": 0.55,
        "train_precision": 0.7,
        "train_recall": 0.2,
        "train_f1": 0.3,
        "step5_min_precision": 0.6,
        "step5_threshold": 0.42,
        "step5_val_pick_feasible": True,
        "step5_feature_columns": ["x"],
        "session_clean_cache_hit": True,
        "prepare_training_frame_seconds": 1.0,
        "step5_seconds": 10.0,
        "run_training_total_seconds": 100.0,
        "model_path": "out/models_high_tier_mvp/run/model.pkl",
        "step4_split_periods": {"basis": "gaming_day_event", "by_split": {"train": {"min_gaming_day": "2024-01-01"}}},
    }
    md = build_metrics_detailed(metrics)
    assert md["split_metrics"]["train"]["ap"] == 0.55
    assert md["feature_columns"] == ["x"]
    assert md["split_periods"]["basis"] == "gaming_day_event"
    dbg = build_pipeline_debug(metrics)
    assert dbg["cache"]["session_clean_cache_hit"] is True
    assert dbg["split_periods"]["basis"] == "gaming_day_event"
    assert dbg["timings_sec"]["prepare_training_frame"] == 1.0
