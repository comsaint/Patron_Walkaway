"""Model wrappers used by artifact bundles.

These wrappers keep the external bundle contract simple: ``model.pkl`` always exposes a
single object with a ``predict_proba(X)`` method, even when the underlying implementation
is a small ensemble.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd


def _positive_class_scores(model: Any, X: pd.DataFrame) -> np.ndarray:
    """Return 1d positive-class scores from a sklearn-like binary classifier."""
    raw = model.predict_proba(X)
    arr = np.asarray(raw, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 2:
        raise ValueError(
            "_positive_class_scores requires predict_proba(X) with shape (n, >=2); "
            f"got {arr.shape!r}."
        )
    return arr[:, 1].reshape(-1)


def _feature_importance_vector(model: Any, feature_count: int) -> np.ndarray:
    """Best-effort feature importance vector aligned to the bundle feature list."""
    booster = getattr(model, "booster_", None)
    if booster is not None:
        try:
            gains = np.asarray(
                booster.feature_importance(importance_type="gain"),
                dtype=np.float64,
            ).reshape(-1)
            if len(gains) == feature_count:
                return gains
        except Exception:
            pass
    raw = getattr(model, "feature_importances_", None)
    if raw is None:
        return np.zeros(feature_count, dtype=np.float64)
    arr = np.asarray(raw, dtype=np.float64).reshape(-1)
    if len(arr) != feature_count:
        return np.zeros(feature_count, dtype=np.float64)
    return arr


class EqualWeightSoftVoteModel:
    """Equal-weight average of multiple binary classifiers.

    The wrapper is intentionally small and pickle-friendly so it can be stored directly
    inside ``model.pkl`` and loaded by serving / backtesting without special file layouts.
    """

    model_kind: str = "soft_vote_equal"
    supports_shap_reason_codes: bool = False
    reason_codes_disabled_reason: str = "ensemble_soft_vote_reason_codes_disabled"

    def __init__(
        self,
        models: Sequence[Any],
        feature_names: Sequence[str],
        component_backends: Sequence[str],
    ) -> None:
        if len(models) < 2:
            raise ValueError("EqualWeightSoftVoteModel requires at least 2 component models.")
        if len(models) != len(component_backends):
            raise ValueError("models and component_backends must have the same length.")
        self.models = tuple(models)
        self.feature_names = tuple(str(x) for x in feature_names)
        self.component_backends = tuple(str(x) for x in component_backends)
        self.reason_codes_enabled = False

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Return binary-class probabilities with equal-weight averaged class-1 score."""
        parts = [_positive_class_scores(model, X) for model in self.models]
        avg = np.mean(np.column_stack(parts), axis=1, dtype=np.float64)
        avg = np.clip(avg, 0.0, 1.0)
        return np.column_stack([1.0 - avg, avg])

    @property
    def feature_importances_(self) -> np.ndarray:
        """Mean component feature importance, aligned to ``feature_names``."""
        n = len(self.feature_names)
        if n <= 0:
            return np.asarray([], dtype=np.float64)
        mats = [
            _feature_importance_vector(model, n)
            for model in self.models
        ]
        return np.mean(np.vstack(mats), axis=0, dtype=np.float64)
