"""Expanding-window Time-CV for baseline feature selection (operational P@1hr)."""

from trainer_hightier.feature_experiment.time_cv.fold_definitions import (
    GAMING_DAY_COLUMN,
    TimeFold,
    generate_expanding_folds,
    unique_gaming_days_from_parquet,
)
from trainer_hightier.feature_experiment.time_cv.report import (
    TimeCvArmDecision,
    aggregate_arm_decision,
    feature_pruning_decision_from_loo,
)
from trainer_hightier.feature_experiment.time_cv.runner import run_time_cv_ablation

__all__ = [
    "GAMING_DAY_COLUMN",
    "TimeCvArmDecision",
    "TimeFold",
    "aggregate_arm_decision",
    "feature_pruning_decision_from_loo",
    "generate_expanding_folds",
    "run_time_cv_ablation",
    "unique_gaming_days_from_parquet",
]
