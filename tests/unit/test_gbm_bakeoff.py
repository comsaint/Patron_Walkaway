"""Unit tests for A3 GBM family compare."""

from __future__ import annotations

import inspect
import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

pytest.importorskip("catboost")
pytest.importorskip("xgboost")

from trainer.training.gbm_bakeoff import BAKEOFF_BACKENDS, train_and_select_rated_gbm_family
from trainer.training import trainer as trainer_mod


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


def test_train_single_rated_model_releases_a3_bakeoff_temp_matrices() -> None:
    src = inspect.getsource(trainer_mod.train_single_rated_model)
    anchor = "Peak-RAM cleanup: A3 may materialize rated valid/test splits from parquet"
    i_anchor = src.find(anchor)
    assert i_anchor > 0, "A3 bakeoff cleanup anchor must exist in train_single_rated_model"
    window = src[i_anchor : i_anchor + 700]
    assert "_compare_valid = None" in window
    assert "_compare_test = None" in window
    assert "_x_vl_cmp = None" in window
    assert "_x_te_cmp = None" in window
    assert "gc.collect()" in window
def test_train_and_select_rated_gbm_family_runs_per_backend_optuna_and_emits_metadata() -> None:
    X_tr, y_tr, X_vl, y_vl, sw, hp = _synth_split(seed=3)
    seen_backends: list[str] = []

    def _fake_hpo(
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_val: pd.DataFrame,
        y_val: pd.Series,
        sw_train: pd.Series,
        *,
        backend: str = "lightgbm",
        n_trials: int | None = None,
        label: str = "",
        field_test_constrained_optuna_objective_allowed: bool | None = None,
        val_window_hours: float | None = None,
        timeout_seconds: int | None = None,
        early_stop_patience: int | None = None,
        hpo_sample_rows: int | None = None,
        hpo_objective_manifest: list[dict[str, object]] | None = None,
    ) -> dict:
        seen_backends.append(backend)
        payload = {
            "catboost": {
                "iterations": 120,
                "learning_rate": 0.06,
                "depth": 6,
                "l2_leaf_reg": 2.5,
                "random_seed": 42,
                "verbose": False,
                "early_stopping_rounds": 50,
                "allow_writing_files": False,
                "loss_function": "Logloss",
                "thread_count": -1,
            },
            "xgboost": {
                "n_estimators": 150,
                "learning_rate": 0.07,
                "max_depth": 5,
                "reg_lambda": 0.3,
                "reg_alpha": 0.1,
                "subsample": 0.8,
                "colsample_bytree": 0.85,
                "min_child_weight": 2.0,
                "objective": "binary:logistic",
                "tree_method": "hist",
                "random_state": 42,
                "n_jobs": -1,
                "verbosity": 0,
            },
        }[backend]
        if hpo_objective_manifest is not None:
            hpo_objective_manifest.clear()
            hpo_objective_manifest.append(
                {
                    "optuna_hpo_backend": backend,
                    "optuna_hpo_enabled": True,
                    "optuna_hpo_n_trials_requested": n_trials,
                    "optuna_hpo_timeout_seconds": timeout_seconds,
                    "optuna_hpo_early_stop_patience": early_stop_patience,
                    "optuna_hpo_objective_mode": "validation_ap",
                    "optuna_hpo_study_best_trial_value": 0.321,
                    "optuna_hpo_study_trials_completed": 3,
                    "optuna_hpo_study_stopped_early": False,
                }
            )
        return payload

    with patch("trainer.training.trainer.run_backend_optuna_search", side_effect=_fake_hpo):
        winner, winner_art, report = train_and_select_rated_gbm_family(
            X_tr,
            y_tr,
            X_vl,
            y_vl,
            sw,
            hp,
            lightgbm_artifact=_lightgbm_artifact(X_tr, y_tr, X_vl, y_vl, sw, hp),
            run_optuna=True,
            X_test=X_vl,
            y_test=y_vl,
            val_dec026_window_hours=72.0,
            val_dec026_min_alerts_per_hour=50.0,
        )

    assert sorted(seen_backends) == ["catboost", "xgboost"]
    assert winner in BAKEOFF_BACKENDS
    assert winner_art["metrics"]["model_backend"] == winner
    per = report["per_backend"]
    assert per["catboost"]["optuna_hpo_backend"] == "catboost"
    assert per["catboost"]["optuna_hpo_enabled"] is True
    assert per["catboost"]["optuna_hpo_objective_mode"] == "validation_ap"
    assert per["catboost"]["best_hyperparams"]["iterations"] == 120
    assert per["xgboost"]["optuna_hpo_backend"] == "xgboost"
    assert per["xgboost"]["optuna_hpo_enabled"] is True
    assert per["xgboost"]["best_hyperparams"]["n_estimators"] == 150


def test_train_and_select_rated_gbm_family_skips_backend_optuna_when_disabled() -> None:
    X_tr, y_tr, X_vl, y_vl, sw, hp = _synth_split(seed=4)

    with patch("trainer.training.trainer.run_backend_optuna_search") as mocked_hpo:
        winner, _winner_art, report = train_and_select_rated_gbm_family(
            X_tr,
            y_tr,
            X_vl,
            y_vl,
            sw,
            hp,
            lightgbm_artifact=_lightgbm_artifact(X_tr, y_tr, X_vl, y_vl, sw, hp),
            run_optuna=False,
            X_test=X_vl,
            y_test=y_vl,
        )

    assert winner in BAKEOFF_BACKENDS
    mocked_hpo.assert_not_called()
    per = report["per_backend"]
    assert per["lightgbm"]["optuna_hpo_enabled"] is False
    assert per["catboost"]["optuna_hpo_enabled"] is False
    assert per["catboost"]["optuna_hpo_objective_mode"] == "disabled"
    assert per["xgboost"]["optuna_hpo_enabled"] is False
    assert per["xgboost"]["optuna_hpo_objective_mode"] == "disabled"
