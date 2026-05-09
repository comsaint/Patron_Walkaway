"""trainer/training/hpo_runtime.py
====================================
GBM backend default hyperparameters and imbalance / CatBoost runtime helpers
split from ``trainer.training.trainer`` (refactor plan Phase C).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd


def _backend_hpo_defaults(backend: str) -> dict[str, Any]:
    backend_n = str(backend or "").strip().lower()
    if backend_n == "lightgbm":
        return {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "max_depth": 8,
            "min_child_samples": 20,
        }
    if backend_n == "catboost":
        return {
            "iterations": 400,
            "learning_rate": 0.05,
            "depth": 8,
            "l2_leaf_reg": 3.0,
            "random_strength": 1.0,
            "rsm": 1.0,
            "random_seed": 42,
            "verbose": False,
            "early_stopping_rounds": 50,
            "allow_writing_files": False,
            "loss_function": "Logloss",
            "thread_count": -1,
        }
    if backend_n == "xgboost":
        return {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "reg_lambda": 1.0,
            "reg_alpha": 0.0,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 4.0,
            "objective": "binary:logistic",
            "tree_method": "hist",
            "random_state": 42,
            "n_jobs": -1,
            "verbosity": 0,
        }
    raise ValueError(f"Unsupported HPO backend: {backend}")


def _catboost_gpu_supports_rsm(loss_function: str) -> bool:
    """Return whether CatBoost GPU supports ``rsm`` for the loss."""
    loss_name = str(loss_function).split(":", 1)[0].strip().lower()
    return "pairwise" in loss_name or loss_name == "querycrossentropy"


def _sanitize_catboost_params_for_runtime(params: Mapping[str, Any]) -> dict[str, Any]:
    """Drop CatBoost options that are invalid for the selected runtime."""
    out = dict(params)
    task_type = str(out.get("task_type", "CPU")).strip().upper()
    loss_function = str(out.get("loss_function", "")).strip()
    if task_type == "GPU" and not _catboost_gpu_supports_rsm(loss_function):
        out.pop("rsm", None)
    return out


def _balanced_binary_class_ratio(y: pd.Series) -> Optional[float]:
    """Return neg/pos ratio for strict binary labels; None when unavailable."""
    if y is None or len(y) == 0:
        return None
    ya = np.asarray(y, dtype=float).reshape(-1)
    if ya.size == 0 or not np.isfinite(ya).all():
        return None
    pos = int(np.sum(ya == 1.0))
    neg = int(np.sum(ya == 0.0))
    if pos <= 0 or neg <= 0:
        return None
    return float(neg / pos)


def _apply_backend_imbalance_params(
    backend: str,
    params: Mapping[str, Any],
    y_train: pd.Series,
) -> dict[str, Any]:
    """Align imbalance handling across GBM backends for fair bakeoff."""
    backend_n = str(backend or "").strip().lower()
    out = dict(params)
    if backend_n == "lightgbm":
        return out

    ratio = _balanced_binary_class_ratio(y_train)
    if ratio is None:
        return out

    if backend_n == "catboost":
        out.setdefault("class_weights", [1.0, float(ratio)])
        return out
    if backend_n == "xgboost":
        out.setdefault("scale_pos_weight", float(ratio))
        return out
    return out
