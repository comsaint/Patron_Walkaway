"""Unit tests for A3 GBM family compare."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest

pytest.importorskip("catboost")
pytest.importorskip("xgboost")

from trainer.training.gbm_bakeoff import BAKEOFF_BACKENDS, train_and_select_rated_gbm_family


def _synth_split(
    *,
    n_train: int = 220,
    n_val: int = 120,
    n_features: int = 5,
    seed: int = 1,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.Series, pd.Series, dict]:
    rng = np.random.default_rng(seed)
    n = n_train + n_val
    X = pd.DataFrame(
        rng.normal(size=(n, n_features)),
        columns=[f"f{i}" for i in range(n_features)],
    )
    y = pd.Series(rng.integers(0, 2, size=n))
    X_tr, X_vl = X.iloc[:n_train].copy(), X.iloc[n_train:].copy()
    y_tr, y_vl = y.iloc[:n_train].copy(), y.iloc[n_train:].copy()
    sw = pd.Series(np.ones(n_train), index=y_tr.index)
    hp = {
        "n_estimators": 48,
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


def _lightgbm_artifact(
    X_tr: pd.DataFrame,
    y_tr: pd.Series,
    X_vl: pd.DataFrame,
    y_vl: pd.Series,
    sw: pd.Series,
    hp: dict,
) -> dict:
    model = lgb.LGBMClassifier(
        objective="binary",
        random_state=42,
        n_estimators=hp["n_estimators"],
        learning_rate=hp["learning_rate"],
        max_depth=hp["max_depth"],
        num_leaves=hp["num_leaves"],
        min_child_samples=hp["min_child_samples"],
        colsample_bytree=hp["colsample_bytree"],
        subsample=hp["subsample"],
        reg_alpha=hp["reg_alpha"],
        reg_lambda=hp["reg_lambda"],
    )
    model.fit(X_tr, y_tr, sample_weight=sw)
    scores = model.predict_proba(X_vl)[:, 1]
    preds = (scores >= 0.5).astype(int)
    tp = int(((preds == 1) & (y_vl.to_numpy() == 1)).sum())
    fp = int(((preds == 1) & (y_vl.to_numpy() == 0)).sum())
    fn = int(((preds == 0) & (y_vl.to_numpy() == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    return {
        "model": model,
        "threshold": 0.5,
        "features": list(X_tr.columns),
        "metrics": {
            "val_ap": 0.4,
            "val_precision": prec,
            "val_recall": rec,
            "val_f1": f1,
            "val_fbeta_05": prec,
            "threshold": 0.5,
            "val_samples": len(y_vl),
            "val_positives": int(y_vl.sum()),
            "val_random_ap": float(y_vl.mean()),
            "_uncalibrated": False,
            "test_ap": 0.41,
        },
    }


def test_train_and_select_rated_gbm_family_returns_schema_and_dispositions() -> None:
    X_tr, y_tr, X_vl, y_vl, sw, hp = _synth_split()
    winner, winner_art, report = train_and_select_rated_gbm_family(
        X_tr,
        y_tr,
        X_vl,
        y_vl,
        sw,
        hp,
        lightgbm_artifact=_lightgbm_artifact(X_tr, y_tr, X_vl, y_vl, sw, hp),
        X_test=X_vl,
        y_test=y_vl,
        val_dec026_window_hours=72.0,
        val_dec026_min_alerts_per_hour=50.0,
    )
    assert report["schema_version"] == "a3_v2"
    assert winner in BAKEOFF_BACKENDS
    assert winner_art["metrics"]["model_backend"] == winner
    assert report["selection_rule"]
    assert report["selection_mode"] == "field_test"
    per = report["per_backend"]
    for backend in BAKEOFF_BACKENDS:
        assert backend in per
        assert "bakeoff_disposition" in per[backend]
        assert per[backend]["bakeoff_disposition"] in ("winner", "hold", "reject")
        if "error" not in per[backend]:
            assert "val_field_test_primary_score" in per[backend]
    assert report["ensemble_bridge"]["same_splits"] is True
    assert report["ensemble_bridge"]["same_eval_script"] is True
    assert report["ensemble_bridge"]["train_rows"] == len(X_tr)
    assert per[winner]["bakeoff_disposition"] == "winner"


def test_train_and_select_rated_gbm_family_small_valid_still_has_winner() -> None:
    X_tr, y_tr, X_vl, y_vl, sw, hp = _synth_split(seed=2)
    winner, _winner_art, report = train_and_select_rated_gbm_family(
        X_tr,
        y_tr,
        X_vl.head(5),
        y_vl.head(5),
        sw,
        hp,
        lightgbm_artifact=_lightgbm_artifact(X_tr, y_tr, X_vl, y_vl, sw, hp),
    )
    assert winner in BAKEOFF_BACKENDS
    assert report["per_backend"][winner]["bakeoff_disposition"] == "winner"
