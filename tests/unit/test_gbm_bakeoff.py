"""Unit tests for A3 GBM bakeoff (CatBoost / XGBoost vs LightGBM reference)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("catboost")
pytest.importorskip("xgboost")

from trainer.training.gbm_bakeoff import BAKEOFF_BACKENDS, run_rated_gbm_bakeoff


def _synth_split(*, n_train: int = 220, n_val: int = 120, n_features: int = 5, seed: int = 1):
    rng = np.random.default_rng(seed)
    n = n_train + n_val
    X = pd.DataFrame(rng.normal(size=(n, n_features)), columns=[f"f{i}" for i in range(n_features)])
    y = pd.Series(rng.integers(0, 2, size=n))
    X_tr, X_vl = X.iloc[:n_train], X.iloc[n_train:]
    y_tr, y_vl = y.iloc[:n_train], y.iloc[n_train:]
    sw = pd.Series(np.ones(n_train), index=y_tr.index)
    hp = {
        "n_estimators": 64,
        "learning_rate": 0.1,
        "max_depth": 4,
        "num_leaves": 31,
        "min_child_samples": 10,
        "colsample_bytree": 0.9,
        "subsample": 0.9,
        "reg_alpha": 0.01,
        "reg_lambda": 0.1,
    }
    return X_tr, y_tr, X_vl, y_vl, sw, hp


def test_run_rated_gbm_bakeoff_returns_schema_and_dispositions() -> None:
    X_tr, y_tr, X_vl, y_vl, sw, hp = _synth_split()
    ref = {
        "val_ap": 0.4,
        "val_precision": 0.35,
        "val_recall": 0.3,
        "val_f1": 0.32,
        "val_fbeta_05": 0.34,
        "threshold": 0.5,
        "val_samples": len(y_vl),
        "val_positives": int(y_vl.sum()),
        "val_random_ap": float(y_vl.mean()),
        "_uncalibrated": False,
        "test_ap": 0.41,
    }
    rep = run_rated_gbm_bakeoff(
        X_tr,
        y_tr,
        X_vl,
        y_vl,
        sw,
        hp,
        lgbm_reference_metrics=ref,
        X_test=X_vl,
        y_test=y_vl,
        val_dec026_window_hours=72.0,
        val_dec026_min_alerts_per_hour=50.0,
    )
    assert rep["schema_version"] == "a3_v1"
    assert rep["winner_backend"] in BAKEOFF_BACKENDS
    assert rep["selection_rule"]
    per = rep["per_backend"]
    for b in BAKEOFF_BACKENDS:
        assert b in per
        assert "bakeoff_disposition" in per[b]
        assert per[b]["bakeoff_disposition"] in ("winner", "hold", "reject")
    assert rep["ensemble_bridge"]["same_splits"] is True
    assert rep["ensemble_bridge"]["train_rows"] == len(X_tr)
    winner = rep["winner_backend"]
    assert per[winner]["bakeoff_disposition"] == "winner"


def test_run_rated_gbm_bakeoff_one_reject_still_has_winner() -> None:
    X_tr, y_tr, X_vl, y_vl, sw, hp = _synth_split(seed=2)
    ref = {k: 0.0 for k in ("val_ap", "val_precision", "val_recall", "val_f1", "val_fbeta_05")}
    ref.update(
        {
            "threshold": 0.5,
            "val_samples": len(y_vl),
            "val_positives": int(y_vl.sum()),
            "val_random_ap": float(y_vl.mean()),
            "_uncalibrated": True,
        }
    )
    rep = run_rated_gbm_bakeoff(
        X_tr,
        y_tr,
        X_vl.head(5),
        y_vl.head(5),
        sw,
        hp,
        lgbm_reference_metrics=ref,
    )
    assert rep["winner_backend"] in BAKEOFF_BACKENDS
