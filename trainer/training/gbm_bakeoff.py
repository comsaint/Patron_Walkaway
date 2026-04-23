"""Precision uplift A3 / R3: fair GBDT bakeoff (LightGBM reference + CatBoost + XGBoost).

Same train/valid/test matrices, same DEC-013 ``sample_weight`` row vector, and the same
LightGBM-shaped hyperparameter dict from Optuna (mapped per backend). Each backend picks
its own validation threshold via :func:`pick_threshold_dec026` on its validation scores.

LightGBM metrics are taken from the already-trained primary model (no second LGBM fit).

C3 (stacking / blending): this module only emits a compact JSON-serializable report under
``training_metrics["rated"]["gbm_bakeoff"]`` with ``ensemble_bridge`` metadata so a future
meta-learner can align backends without re-defining splits.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from trainer.core import config as _cfg
from trainer.training.threshold_selection import pick_threshold_dec026

logger = logging.getLogger("trainer")

MIN_VALID_TEST_ROWS: int = int(getattr(_cfg, "MIN_VALID_TEST_ROWS", 50))
THRESHOLD_MIN_RECALL = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)
THRESHOLD_MIN_ALERT_COUNT: int = int(getattr(_cfg, "THRESHOLD_MIN_ALERT_COUNT", 5))
THRESHOLD_FBETA: float = float(getattr(_cfg, "THRESHOLD_FBETA", 0.5))
PRODUCTION_NEG_POS_RATIO = getattr(_cfg, "PRODUCTION_NEG_POS_RATIO", None)

BAKEOFF_BACKENDS: Tuple[str, ...] = ("lightgbm", "catboost", "xgboost")


def _has_strong_validation(X_val: pd.DataFrame, y_val: pd.Series) -> bool:
    return (
        not X_val.empty
        and len(y_val) >= MIN_VALID_TEST_ROWS
        and int(y_val.isna().sum()) == 0
        and int(y_val.sum()) >= 1
        and int((y_val == 0).sum()) >= 1
    )


def _val_block_from_scores(
    y_val: pd.Series,
    val_scores: np.ndarray,
    hp: Mapping[str, Any],
    *,
    label: str,
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
) -> Dict[str, Any]:
    """Mirror ``_train_one_model`` validation metrics (DEC-026 pick)."""
    _has_val = (
        len(y_val) >= MIN_VALID_TEST_ROWS
        and int(y_val.isna().sum()) == 0
        and int(y_val.sum()) >= 1
        and int((y_val == 0).sum()) >= 1
    )
    if _has_val and y_val.sum() > 0:
        prauc = float(average_precision_score(y_val, val_scores))
        _pick = pick_threshold_dec026(
            np.asarray(y_val, dtype=float),
            np.asarray(val_scores, dtype=float),
            recall_floor=THRESHOLD_MIN_RECALL,
            min_alert_count=THRESHOLD_MIN_ALERT_COUNT,
            min_alerts_per_hour=val_dec026_min_alerts_per_hour,
            window_hours=val_dec026_window_hours,
            fbeta_beta=THRESHOLD_FBETA,
        )
        if _pick.is_fallback:
            best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
            best_fbeta = 0.0
        else:
            best_t = _pick.threshold
            best_prec = _pick.precision
            best_rec = _pick.recall
            best_fbeta = _pick.fbeta
            best_f1 = _pick.f1
    else:
        prauc = 0.0
        best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
        best_fbeta = 0.0
    n_val = int(len(y_val))
    n_val_pos = int(y_val.sum())
    val_random_ap = (n_val_pos / n_val) if n_val > 0 else 0.0
    out: Dict[str, Any] = {
        "label": label,
        "val_ap": prauc,
        "val_precision": best_prec,
        "val_recall": best_rec,
        "val_f1": best_f1,
        "val_fbeta_05": best_fbeta,
        "threshold": best_t,
        "val_samples": n_val,
        "val_positives": n_val_pos,
        "val_random_ap": val_random_ap,
        "best_hyperparams": dict(hp),
        "_uncalibrated": not _has_val,
    }
    if val_dec026_window_hours is not None and val_dec026_min_alerts_per_hour is not None:
        out["val_dec026_pick_window_hours"] = float(val_dec026_window_hours)
        out["val_dec026_pick_min_alerts_per_hour"] = float(val_dec026_min_alerts_per_hour)
    return out


def _map_hp_catboost(hp: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "iterations": int(hp.get("n_estimators", 400)),
        "learning_rate": float(hp.get("learning_rate", 0.05)),
        "depth": min(int(hp.get("max_depth", 8)), 16),
        "l2_leaf_reg": max(float(hp.get("reg_lambda", 1.0)), 1e-8),
        "random_seed": 42,
        "verbose": False,
        "early_stopping_rounds": 50,
        "allow_writing_files": False,
        "loss_function": "Logloss",
        "thread_count": -1,
    }


def _map_hp_xgboost(hp: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "n_estimators": int(hp.get("n_estimators", 400)),
        "learning_rate": float(hp.get("learning_rate", 0.05)),
        "max_depth": int(hp.get("max_depth", 8)),
        "reg_lambda": float(hp.get("reg_lambda", 1.0)),
        "reg_alpha": float(hp.get("reg_alpha", 0.0)),
        "subsample": float(hp.get("subsample", 0.8)),
        "colsample_bytree": float(hp.get("colsample_bytree", 0.8)),
        "min_child_weight": max(float(hp.get("min_child_samples", 20)) / 5.0, 1.0),
        "objective": "binary:logistic",
        "tree_method": "hist",
        "random_state": 42,
        "n_jobs": -1,
        "verbosity": 0,
    }


def _train_catboost_backend(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    hp: Mapping[str, Any],
    *,
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
) -> Tuple[Any, Dict[str, Any]]:
    from catboost import CatBoostClassifier

    c_hp = _map_hp_catboost(hp)
    iterations = int(c_hp.pop("iterations"))
    early = int(c_hp.pop("early_stopping_rounds"))
    model = CatBoostClassifier(iterations=iterations, **c_hp)
    _has_val = _has_strong_validation(X_val, y_val)
    if _has_val:
        model.fit(
            X_train,
            y_train.astype(np.int32),
            sample_weight=sw_train,
            eval_set=(X_val, y_val.astype(np.int32)),
            early_stopping_rounds=early,
            verbose=False,
        )
        val_scores = np.asarray(model.predict_proba(X_val)[:, 1], dtype=float)
    else:
        model.fit(X_train, y_train.astype(np.int32), sample_weight=sw_train, verbose=False)
        val_scores = np.zeros(len(y_val), dtype=float)
    metrics = _val_block_from_scores(
        y_val,
        val_scores,
        hp,
        label="rated_catboost",
        val_dec026_window_hours=val_dec026_window_hours,
        val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
    )
    return model, metrics


def _train_xgboost_backend(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    hp: Mapping[str, Any],
    *,
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
) -> Tuple[Any, Dict[str, Any]]:
    import xgboost as xgb

    x_hp = _map_hp_xgboost(hp)
    n_est = int(x_hp.pop("n_estimators"))
    model = xgb.XGBClassifier(n_estimators=n_est, **x_hp)
    _has_val = _has_strong_validation(X_val, y_val)
    if _has_val:
        # XGBoost sklearn API varies by version (callbacks vs no early stopping on fit).
        # Keep a single full fit on train+eval_set for loss logging without early stopping
        # to stay compatible across 2.x / 3.x; n_estimators is capped via mapped hp.
        model.fit(
            X_train,
            y_train,
            sample_weight=sw_train,
            eval_set=[(X_val, y_val)],
            verbose=False,
        )
        val_scores = np.asarray(model.predict_proba(X_val)[:, 1], dtype=float)
    else:
        model.fit(X_train, y_train, sample_weight=sw_train, verbose=False)
        val_scores = np.zeros(len(y_val), dtype=float)
    metrics = _val_block_from_scores(
        y_val,
        val_scores,
        hp,
        label="rated_xgboost",
        val_dec026_window_hours=val_dec026_window_hours,
        val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
    )
    return model, metrics


def _pick_winner(rows: Dict[str, Dict[str, Any]]) -> Tuple[str, str]:
    """Return (winner_backend, selection_rule)."""
    rule = "max_val_ap_then_val_fbeta_05_then_val_precision"
    candidates: List[str] = []
    for b in BAKEOFF_BACKENDS:
        r = rows.get(b) or {}
        if r.get("bakeoff_disposition") == "reject" or r.get("error"):
            continue
        candidates.append(b)
    if not candidates:
        return "lightgbm", rule

    def _key(b: str) -> Tuple[float, float, float]:
        r = rows[b]
        return (
            float(r.get("val_ap") or 0.0),
            float(r.get("val_fbeta_05") or 0.0),
            float(r.get("val_precision") or 0.0),
        )

    candidates.sort(key=_key, reverse=True)
    return candidates[0], rule


def _assign_dispositions(rows: Dict[str, Dict[str, Any]], winner: str) -> None:
    for b in BAKEOFF_BACKENDS:
        r = rows.setdefault(b, {})
        if r.get("error"):
            r["bakeoff_disposition"] = "reject"
        elif b == winner:
            r["bakeoff_disposition"] = "winner"
        else:
            r["bakeoff_disposition"] = "hold"


def run_rated_gbm_bakeoff(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    hp: Mapping[str, Any],
    *,
    lgbm_reference_metrics: Mapping[str, Any],
    X_test: Optional[pd.DataFrame] = None,
    y_test: Optional[pd.Series] = None,
    val_dec026_window_hours: Optional[float] = None,
    val_dec026_min_alerts_per_hour: Optional[float] = None,
) -> Dict[str, Any]:
    """Compare CatBoost and XGBoost against an already-fitted LightGBM metrics row.

    Returns a JSON-serializable dict (no model objects).
    """
    from trainer.training.trainer import _compute_test_metrics

    rows: Dict[str, Dict[str, Any]] = {}

    # LightGBm: reference only (primary path already trained).
    lgbm_row: Dict[str, Any] = {
        "backend": "lightgbm",
        "source": "primary_train",
        "val_ap": float(lgbm_reference_metrics.get("val_ap", 0.0)),
        "val_precision": float(lgbm_reference_metrics.get("val_precision", 0.0)),
        "val_recall": float(lgbm_reference_metrics.get("val_recall", 0.0)),
        "val_f1": float(lgbm_reference_metrics.get("val_f1", 0.0)),
        "val_fbeta_05": float(lgbm_reference_metrics.get("val_fbeta_05", 0.0)),
        "threshold": float(lgbm_reference_metrics.get("threshold", 0.5)),
        "val_samples": lgbm_reference_metrics.get("val_samples"),
        "val_positives": lgbm_reference_metrics.get("val_positives"),
        "val_random_ap": lgbm_reference_metrics.get("val_random_ap"),
        "_uncalibrated": bool(lgbm_reference_metrics.get("_uncalibrated", False)),
    }
    if X_test is not None and y_test is not None and not X_test.empty and not y_test.empty:
        try:
            # Test metrics already in reference — shallow copy keys for side-by-side table.
            lgbm_row["test_ap"] = lgbm_reference_metrics.get("test_ap")
            lgbm_row["test_precision"] = lgbm_reference_metrics.get("test_precision")
            lgbm_row["test_f1"] = lgbm_reference_metrics.get("test_f1")
        except Exception:
            pass
    rows["lightgbm"] = lgbm_row

    for trainer_fn, name in (
        (_train_catboost_backend, "catboost"),
        (_train_xgboost_backend, "xgboost"),
    ):
        try:
            _model, vmetrics = trainer_fn(
                X_train,
                y_train,
                X_val,
                y_val,
                sw_train,
                hp,
                val_dec026_window_hours=val_dec026_window_hours,
                val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
            )
            row = {"backend": name, **{k: vmetrics[k] for k in vmetrics if k != "label"}}
            row["label"] = f"rated_{name}"
            if X_test is not None and y_test is not None and not X_test.empty:
                try:
                    test_m = _compute_test_metrics(
                        _model,
                        float(vmetrics["threshold"]),
                        X_test,
                        y_test,
                        label=f"rated_{name}",
                        _uncalibrated=bool(vmetrics.get("_uncalibrated", False)),
                        log_results=False,
                        production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                    )
                    for k, v in test_m.items():
                        if k.startswith("test_") or k in ("test_neg_pos_ratio", "production_neg_pos_ratio_assumed"):
                            row[k] = v
                except Exception as exc:
                    row["test_error"] = str(exc)
            del _model
        except ImportError as exc:
            row = {
                "backend": name,
                "error": f"import_error:{exc}",
                "bakeoff_disposition": "reject",
            }
            rows[name] = row
            logger.warning("A3 gbm_bakeoff: %s skipped (%s)", name, exc)
            continue
        except Exception as exc:
            row = {
                "backend": name,
                "error": str(exc),
                "bakeoff_disposition": "reject",
            }
            rows[name] = row
            logger.warning("A3 gbm_bakeoff: %s training failed: %s", name, exc)
            continue
        rows[name] = row

    winner, rule = _pick_winner(rows)
    _assign_dispositions(rows, winner)

    report: Dict[str, Any] = {
        "schema_version": "a3_v1",
        "winner_backend": winner,
        "selection_rule": rule,
        "per_backend": rows,
        "ensemble_bridge": {
            "same_splits": True,
            "train_rows": int(len(X_train)),
            "valid_rows": int(len(X_val)),
            "test_rows": int(len(y_test)) if y_test is not None else 0,
            "feature_columns": list(X_train.columns),
            "note": (
                "C3 stacking/blending: OOF exports and meta-learner training are not in A3 scope; "
                "this block records aligned backends and metrics for a future ensemble step."
            ),
        },
    }
    logger.info(
        "A3 gbm_bakeoff winner=%s rule=%s catboost=%s xgboost=%s",
        winner,
        rule,
        rows.get("catboost", {}).get("bakeoff_disposition"),
        rows.get("xgboost", {}).get("bakeoff_disposition"),
    )
    return report
