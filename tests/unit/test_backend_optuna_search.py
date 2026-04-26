"""Unit tests for backend Optuna search result handling."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trainer.training import trainer as trainer_mod


class _NoSuccessfulTrialStudy:
    """Optuna Study stub with no completed successful trials."""

    trials: list[Any] = []

    @property
    def best_params(self) -> dict[str, Any]:
        """Raise like Optuna when no successful trial exists."""
        raise ValueError("No trials are completed yet.")

    @property
    def best_value(self) -> float:
        """Raise like Optuna when no successful trial exists."""
        raise ValueError("No trials are completed yet.")

    def optimize(self, *args: Any, **kwargs: Any) -> None:
        """Skip real optimization to keep the test fast and deterministic."""
        return None


class _SuccessfulCatBoostGpuStudy:
    """Optuna Study stub with CatBoost params selected by one successful trial."""

    trials: list[Any] = [object()]
    _best_params = {
        "iterations": 150,
        "learning_rate": 0.07,
        "rsm": 0.73,
    }

    @property
    def best_params(self) -> dict[str, Any]:
        """Return the selected Optuna trial parameters."""
        return dict(self._best_params)

    @property
    def best_value(self) -> float:
        """Return a valid objective value."""
        return 0.42

    def optimize(self, *args: Any, **kwargs: Any) -> None:
        """Skip real optimization to keep the test fast and deterministic."""
        return None


def _minimal_split() -> tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.Series, pd.Series]:
    """Return a tiny valid split; fake Optuna prevents model fitting."""
    X_train = pd.DataFrame({"f0": np.array([0.0, 1.0, 2.0, 3.0])})
    y_train = pd.Series([0, 1, 0, 1])
    X_val = pd.DataFrame({"f0": np.array([0.5, 1.5])})
    y_val = pd.Series([0, 1])
    sw_train = pd.Series(np.ones(len(y_train)))
    return X_train, y_train, X_val, y_val, sw_train


def test_run_backend_optuna_search_returns_defaults_when_no_trial_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No completed trial should not crash when reading best_params."""
    monkeypatch.setattr(
        trainer_mod.optuna,
        "create_study",
        lambda **kwargs: _NoSuccessfulTrialStudy(),
    )
    X_train, y_train, X_val, y_val, sw_train = _minimal_split()

    best = trainer_mod.run_backend_optuna_search(
        X_train,
        y_train,
        X_val,
        y_val,
        sw_train,
        backend="lightgbm",
        n_trials=1,
        label="unit",
    )

    assert best == trainer_mod._backend_hpo_defaults("lightgbm")


def test_run_backend_optuna_search_preserves_catboost_gpu_best_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returned best params should reflect Optuna selection, not runtime sanitization."""
    monkeypatch.setattr(
        trainer_mod.optuna,
        "create_study",
        lambda **kwargs: _SuccessfulCatBoostGpuStudy(),
    )
    X_train, y_train, X_val, y_val, sw_train = _minimal_split()

    best = trainer_mod.run_backend_optuna_search(
        X_train,
        y_train,
        X_val,
        y_val,
        sw_train,
        backend="catboost",
        n_trials=1,
        label="unit",
        backend_runtime_params={"task_type": "GPU", "devices": "0"},
    )

    assert best["rsm"] == pytest.approx(0.73)
    assert "task_type" not in best
    assert "devices" not in best
