"""Precision uplift A3 / R3: always-on GBDT family compare.

Contract
--------
- Same feature matrix, same temporal split, same evaluation helper.
- Same DEC-013 / A2 sample_weight vector for all backends.
- Primary winner key = field-test validation objective (DEC-026 operating point,
  prod-adjusted when the contract allows it), not AP.

The caller passes the already-trained LightGBM artifact (which may have been trained
through in-memory / CSV / LibSVM main paths). This module then trains CatBoost and
XGBoost on the same in-memory matrices and returns:

1. A JSON-serialisable report for ``training_metrics["rated"]["gbm_bakeoff"]``.
2. Candidate artifacts for all backends.
3. The selected winner backend/artifact so the caller can persist the actual winner as
   ``model.pkl`` instead of silently keeping LightGBM.

C3 (stacking / blending) is not implemented here; we only emit ``ensemble_bridge`` to
record that all backends were compared on aligned splits.
"""

from __future__ import annotations

import logging
import math
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from trainer.core import config as _cfg
from trainer.core.model_wrappers import EqualWeightSoftVoteModel
from trainer.training.oof_stacking import build_stacked_logistic_candidate
from trainer.training.threshold_selection import pick_threshold_dec026

logger = logging.getLogger("trainer")
_AGENT_DEBUG_LOG_PATH = Path("debug-000243.log")

MIN_VALID_TEST_ROWS: int = int(getattr(_cfg, "MIN_VALID_TEST_ROWS", 50))
THRESHOLD_MIN_RECALL = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)
THRESHOLD_MIN_ALERT_COUNT: int = int(getattr(_cfg, "THRESHOLD_MIN_ALERT_COUNT", 5))
THRESHOLD_FBETA: float = float(getattr(_cfg, "THRESHOLD_FBETA", 0.5))
PRODUCTION_NEG_POS_RATIO = getattr(_cfg, "PRODUCTION_NEG_POS_RATIO", None)

SOFT_VOTE_BACKEND = "soft_vote_equal"
STACKED_LOGISTIC_BACKEND = "stacked_logistic_oof"
BAKEOFF_BACKENDS: Tuple[str, ...] = (
    "lightgbm",
    "catboost",
    "xgboost",
    SOFT_VOTE_BACKEND,
    STACKED_LOGISTIC_BACKEND,
)


def _agent_debug_log(
    *,
    hypothesis_id: str,
    location: str,
    message: str,
    data: Mapping[str, Any],
) -> None:
    """Append one NDJSON debug entry for the current Cursor debug session."""
    payload = {
        "sessionId": "000243",
        "runId": "xgb-bakeoff-debug",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": dict(data),
        "timestamp": int(time.time() * 1000),
    }
    try:
        with _AGENT_DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str, ensure_ascii=True) + "\n")
    except OSError:
        logger.debug("agent debug log write failed", exc_info=True)


def _has_strong_validation(X_val: pd.DataFrame, y_val: pd.Series) -> bool:
    return (
        not X_val.empty
        and len(y_val) >= MIN_VALID_TEST_ROWS
        and int(y_val.isna().sum()) == 0
        and int(y_val.sum()) >= 1
        and int((y_val == 0).sum()) >= 1
    )


def _neg_pos_ratio_from_binary_labels(y: pd.Series) -> Optional[float]:
    """Return neg/pos ratio for strict binary labels; None when invalid / unsupported."""
    if y is None or len(y) == 0:
        return None
    ya = np.asarray(y, dtype=float).reshape(-1)
    if not np.isfinite(ya).all():
        return None
    pos = int(np.sum(ya == 1.0))
    neg = int(np.sum(ya == 0.0))
    if pos <= 0 or neg <= 0:
        return None
    return float(neg / pos)


def _precision_prod_adjusted(
    prec: Optional[float],
    *,
    production_neg_pos_ratio: Optional[float],
    test_neg_pos_ratio: Optional[float],
) -> Optional[float]:
    """Copy of trainer field-test primary-score rescaling (JSON-safe)."""
    if prec is None:
        return None
    p = float(prec)
    if not math.isfinite(p) or p <= 0.0:
        return None
    if p > 1.0 + 1e-9:
        return None
    if p > 1.0:
        p = 1.0
    if production_neg_pos_ratio is None or test_neg_pos_ratio is None:
        return None
    pn = float(production_neg_pos_ratio)
    tn = float(test_neg_pos_ratio)
    if not math.isfinite(pn) or not math.isfinite(tn) or pn <= 0.0 or tn <= 0.0:
        return None
    scaling = pn / tn
    if not math.isfinite(scaling):
        return None
    inv_p = 1.0 / p
    if not math.isfinite(inv_p):
        return None
    term = (inv_p - 1.0) * scaling
    if not math.isfinite(term):
        return None
    denom = 1.0 + term
    if not math.isfinite(denom) or denom <= 0.0:
        return None
    adj = 1.0 / denom
    if not math.isfinite(adj):
        return None
    if adj < -1e-9 or adj > 1.0 + 1e-9:
        return None
    if adj < 0.0:
        return 0.0
    if adj > 1.0:
        return 1.0
    return float(adj)


def _add_field_test_primary_keys(metrics: Dict[str, Any], y_val: pd.Series) -> None:
    """Augment metrics with comparable field-test primary score keys."""
    val_precision = metrics.get("val_precision")
    val_np_ratio = _neg_pos_ratio_from_binary_labels(y_val)
    val_primary_adj = _precision_prod_adjusted(
        float(val_precision) if val_precision is not None else None,
        production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
        test_neg_pos_ratio=val_np_ratio,
    )
    metrics["val_neg_pos_ratio"] = val_np_ratio
    metrics["val_field_test_primary_score"] = (
        float(val_primary_adj) if val_primary_adj is not None else float(val_precision or 0.0)
    )
    metrics["val_field_test_primary_score_mode"] = (
        "precision_prod_adjusted" if val_primary_adj is not None else "precision_raw"
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
    _add_field_test_primary_keys(out, y_val)
    return out


def _to_float32_frame(X: pd.DataFrame) -> pd.DataFrame:
    """Downcast numeric frames for non-LightGBM backends to reduce RAM pressure."""
    if X.empty:
        return X
    return X.astype(np.float32, copy=False)


def _preload_parallel_backend_imports(backends: Tuple[str, ...]) -> None:
    """Import optional backends before worker threads start."""
    for backend in backends:
        # region agent log
        _agent_debug_log(
            hypothesis_id="H1",
            location="trainer/training/gbm_bakeoff.py:_preload_parallel_backend_imports",
            message="preload optional backend before worker threads",
            data={
                "backend": backend,
                "already_in_sys_modules": backend in sys.modules,
            },
        )
        # endregion
        try:
            module = __import__(backend)
            # region agent log
            _agent_debug_log(
                hypothesis_id="H1",
                location="trainer/training/gbm_bakeoff.py:_preload_parallel_backend_imports",
                message="preload optional backend succeeded",
                data={
                    "backend": backend,
                    "module_file": getattr(module, "__file__", None),
                    "module_version": getattr(module, "__version__", None),
                },
            )
            # endregion
        except ImportError as exc:
            # region agent log
            _agent_debug_log(
                hypothesis_id="H1",
                location="trainer/training/gbm_bakeoff.py:_preload_parallel_backend_imports",
                message="preload optional backend failed",
                data={
                    "backend": backend,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            # endregion
            logger.warning("A3 gbm_bakeoff: %s preload failed (%s)", backend, exc)


def _default_backend_hyperparams(backend: str) -> Dict[str, Any]:
    from trainer.training.trainer import _backend_hpo_defaults

    return dict(_backend_hpo_defaults(backend))


def _train_catboost_backend(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    hp: Mapping[str, Any],
    *,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
) -> Tuple[Any, Dict[str, Any]]:
    from catboost import CatBoostClassifier
    from trainer.training.trainer import (
        _apply_backend_imbalance_params,
        _sanitize_catboost_params_for_runtime,
    )

    c_hp = dict(hp)
    if backend_runtime_params:
        c_hp.update(dict(backend_runtime_params))
    iterations = int(c_hp.pop("iterations"))
    early = int(c_hp.pop("early_stopping_rounds"))
    c_hp = _apply_backend_imbalance_params("catboost", c_hp, y_train)
    c_hp = _sanitize_catboost_params_for_runtime(c_hp)
    model = CatBoostClassifier(iterations=iterations, **c_hp)
    X_tr = _to_float32_frame(X_train)
    X_vl = _to_float32_frame(X_val)
    _has_val = _has_strong_validation(X_val, y_val)
    if _has_val:
        model.fit(
            X_tr,
            y_train.astype(np.int32),
            sample_weight=sw_train,
            eval_set=(X_vl, y_val.astype(np.int32)),
            early_stopping_rounds=early,
            verbose=False,
        )
        val_scores = np.asarray(model.predict_proba(X_vl)[:, 1], dtype=float)
    else:
        model.fit(X_tr, y_train.astype(np.int32), sample_weight=sw_train, verbose=False)
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
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
) -> Tuple[Any, Dict[str, Any]]:
    import xgboost as xgb
    from trainer.training.trainer import _apply_backend_imbalance_params

    x_hp = dict(hp)
    if backend_runtime_params:
        x_hp.update(dict(backend_runtime_params))
    n_est = int(x_hp.pop("n_estimators"))
    x_hp = _apply_backend_imbalance_params("xgboost", x_hp, y_train)
    model = xgb.XGBClassifier(n_estimators=n_est, **x_hp)
    X_tr = _to_float32_frame(X_train)
    X_vl = _to_float32_frame(X_val)
    _has_val = _has_strong_validation(X_val, y_val)
    if _has_val:
        model.fit(
            X_tr,
            y_train,
            sample_weight=sw_train,
            eval_set=[(X_vl, y_val)],
            verbose=False,
        )
        val_scores = np.asarray(model.predict_proba(X_vl)[:, 1], dtype=float)
    else:
        model.fit(X_tr, y_train, sample_weight=sw_train, verbose=False)
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
    rule = "max_val_field_test_primary_score_then_val_ap_then_val_fbeta_05"
    candidates: List[str] = []
    for backend in BAKEOFF_BACKENDS:
        row = rows.get(backend) or {}
        if row.get("bakeoff_disposition") == "reject" or row.get("error"):
            continue
        candidates.append(backend)
    if not candidates:
        return "lightgbm", rule

    def _key(backend: str) -> Tuple[float, float, float]:
        row = rows[backend]
        return (
            float(row.get("val_field_test_primary_score") or 0.0),
            float(row.get("val_ap") or 0.0),
            float(row.get("val_fbeta_05") or 0.0),
        )

    candidates.sort(key=_key, reverse=True)
    return candidates[0], rule


def _assign_dispositions(rows: Dict[str, Dict[str, Any]], winner: str) -> None:
    for backend in BAKEOFF_BACKENDS:
        row = rows.setdefault(backend, {})
        if row.get("error"):
            row["bakeoff_disposition"] = "reject"
        elif backend == winner:
            row["bakeoff_disposition"] = "winner"
        else:
            row["bakeoff_disposition"] = "hold"


def _build_soft_vote_candidate(
    candidate_artifacts: Mapping[str, Dict[str, Any]],
    *,
    feature_cols: List[str],
    y_val: pd.Series,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_test: Optional[pd.DataFrame],
    y_test: Optional[pd.Series],
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Create the equal-weight soft-vote candidate from the 3 base models."""
    from trainer.training.trainer import (
        _compute_feature_importance,
        _compute_test_metrics_from_scores,
        _train_metrics_dict_from_y_scores,
    )

    required = ("lightgbm", "catboost", "xgboost")
    missing = [backend for backend in required if backend not in candidate_artifacts]
    if missing:
        raise ValueError(
            "soft_vote_equal requires all 3 base backends; missing %s"
            % ",".join(missing)
        )

    comp_models = [candidate_artifacts[backend]["model"] for backend in required]
    comp_thresholds = [
        float(candidate_artifacts[backend]["metrics"].get("threshold", 0.5))
        for backend in required
    ]
    comp_val_scores = [
        np.asarray(candidate_artifacts[backend]["metrics"]["_val_scores"], dtype=np.float64)
        for backend in required
    ]
    val_scores = np.mean(np.column_stack(comp_val_scores), axis=1, dtype=np.float64)
    metrics = _val_block_from_scores(
        y_val,
        val_scores,
        {},
        label="rated_soft_vote_equal",
        val_dec026_window_hours=val_dec026_window_hours,
        val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
    )
    train_scores = np.mean(
        np.column_stack(
            [
                np.asarray(candidate_artifacts[backend]["metrics"]["_train_scores"], dtype=np.float64)
                for backend in required
            ]
        ),
        axis=1,
        dtype=np.float64,
    )
    metrics.update(
        _train_metrics_dict_from_y_scores(
            y_train,
            train_scores,
            float(metrics["threshold"]),
            label="rated_soft_vote_equal",
            log_results=False,
        )
    )
    if X_test is not None and y_test is not None and not X_test.empty:
        test_scores = np.mean(
            np.column_stack(
                [
                    np.asarray(candidate_artifacts[backend]["metrics"]["_test_scores"], dtype=np.float64)
                    for backend in required
                ]
            ),
            axis=1,
            dtype=np.float64,
        )
        metrics.update(
            _compute_test_metrics_from_scores(
                np.asarray(y_test, dtype=np.float64),
                test_scores,
                float(metrics["threshold"]),
                label="rated_soft_vote_equal",
                _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                log_results=False,
                production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
            )
        )
        metrics["_test_scores"] = test_scores
    metrics["_val_scores"] = val_scores
    metrics["_train_scores"] = train_scores
    metrics["component_backends"] = list(required)
    metrics["component_thresholds"] = {
        backend: thr for backend, thr in zip(required, comp_thresholds)
    }
    model = EqualWeightSoftVoteModel(comp_models, feature_cols, required)
    metrics["feature_importance"] = _compute_feature_importance(model, feature_cols)
    metrics["importance_method"] = "mean_component_gain"
    metrics["model_backend"] = SOFT_VOTE_BACKEND
    metrics["reason_codes_enabled"] = False
    artifact = {
        "model": model,
        "threshold": float(metrics["threshold"]),
        "features": feature_cols,
        "metrics": metrics,
        "model_kind": SOFT_VOTE_BACKEND,
        "reason_codes_enabled": False,
        "component_backends": list(required),
    }
    row = {
        "backend": SOFT_VOTE_BACKEND,
        **{k: metrics[k] for k in metrics if k not in ("_val_scores", "_train_scores", "_test_scores")},
        "label": "rated_soft_vote_equal",
    }
    return row, artifact


def train_and_select_rated_gbm_family(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    hp: Mapping[str, Any],
    *,
    lightgbm_artifact: Mapping[str, Any],
    X_test: Optional[pd.DataFrame] = None,
    y_test: Optional[pd.Series] = None,
    val_dec026_window_hours: Optional[float] = None,
    val_dec026_min_alerts_per_hour: Optional[float] = None,
    run_optuna: bool = True,
    field_test_constrained_optuna_objective_allowed: Optional[bool] = None,
    per_backend_hyperparams: Optional[Mapping[str, Mapping[str, Any]]] = None,
    rated_train_df: Optional[pd.DataFrame] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Train base backends + ensemble candidates on aligned splits and return winner + report."""
    from trainer.training.trainer import (
        _batched_model_positive_class_scores,
        _backend_runtime_manifest,
        _compute_feature_importance,
        _compute_test_metrics,
        _compute_train_metrics,
        resolve_gbm_backend_runtime_plan,
        resolve_backend_optuna_budget,
        run_backend_optuna_search,
    )

    lightgbm_model = lightgbm_artifact["model"]
    feature_cols = list(lightgbm_artifact["features"])
    lightgbm_metrics = dict(lightgbm_artifact["metrics"])
    lightgbm_hp = dict((per_backend_hyperparams or {}).get("lightgbm") or hp)
    lightgbm_metrics["best_hyperparams"] = dict(lightgbm_hp)
    _add_field_test_primary_keys(lightgbm_metrics, y_val)
    lightgbm_metrics["model_backend"] = "lightgbm"
    lightgbm_metrics["_val_scores"] = (
        np.asarray(lightgbm_model.predict_proba(X_val)[:, 1], dtype=np.float64)
        if _has_strong_validation(X_val, y_val)
        else np.zeros(len(y_val), dtype=np.float64)
    )
    lightgbm_metrics["_train_scores"] = _batched_model_positive_class_scores(
        lightgbm_model,
        X_train,
        int(getattr(_cfg, "TRAIN_METRICS_PREDICT_BATCH_ROWS", 500_000)),
    )
    if X_test is not None and y_test is not None and not X_test.empty:
        lightgbm_metrics["_test_scores"] = np.asarray(
            lightgbm_model.predict_proba(X_test)[:, 1],
            dtype=np.float64,
        )
    lightgbm_metrics["feature_importance"] = _compute_feature_importance(
        lightgbm_model,
        feature_cols,
    )
    lightgbm_metrics["importance_method"] = "gain"
    lightgbm_metrics["reason_codes_enabled"] = True

    rows: Dict[str, Dict[str, Any]] = {
        "lightgbm": {
            "backend": "lightgbm",
            "source": "primary_train",
            **{
                k: v
                for k, v in lightgbm_metrics.items()
                if k not in ("_val_scores", "_train_scores", "_test_scores")
            },
        }
    }
    if "optuna_hpo_backend" not in rows["lightgbm"]:
        rows["lightgbm"]["optuna_hpo_backend"] = "lightgbm"
    # Bakeoff does not run Optuna for LightGBM (pre-trained primary artifact + hp).
    # Only CatBoost/XGBoost branches may call run_backend_optuna_search here.
    if "optuna_hpo_enabled" not in rows["lightgbm"]:
        rows["lightgbm"]["optuna_hpo_enabled"] = False
    rows["lightgbm"].update(_backend_runtime_manifest("lightgbm"))
    candidate_artifacts: Dict[str, Dict[str, Any]] = {
        "lightgbm": {
            "model": lightgbm_model,
            "threshold": float(lightgbm_metrics.get("threshold", 0.5)),
            "features": feature_cols,
            "metrics": lightgbm_metrics,
            "model_kind": "lightgbm",
            "reason_codes_enabled": True,
        }
    }

    def _bakeoff_timeout_budget_divisor() -> Optional[int]:
        if not (run_optuna and _has_strong_validation(X_val, y_val)):
            return None
        raw = getattr(_cfg, "OPTUNA_ACTIVE_MODEL_COUNT_FOR_TOTAL_TIMEOUT_SPLIT", 3)
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n > 1 else None

    _timeout_budget_divisor = _bakeoff_timeout_budget_divisor()
    backend_runtime_plan = resolve_gbm_backend_runtime_plan()
    backend_runtime_by_name = dict(backend_runtime_plan.get("backend_runtime_by_name") or {})
    # region agent log
    _agent_debug_log(
        hypothesis_id="H1,H2,H3",
        location="trainer/training/gbm_bakeoff.py:train_and_select_rated_gbm_family",
        message="resolved A3 backend runtime plan",
        data={
            "effective_backend_device_mode": backend_runtime_plan.get("effective_backend_device_mode"),
            "visible_gpu_ids": backend_runtime_plan.get("visible_gpu_ids"),
            "gpu_assignments": backend_runtime_plan.get("gpu_assignments"),
            "parallel_backend_workers": backend_runtime_plan.get("parallel_backend_workers"),
            "parallel_backend_execution": backend_runtime_plan.get("parallel_backend_execution"),
            "xgboost_in_sys_modules": "xgboost" in sys.modules,
            "catboost_in_sys_modules": "catboost" in sys.modules,
        },
    )
    # endregion

    def _run_backend_candidate(
        trainer_fn: Any,
        backend: str,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        try:
            backend_manifest: list[dict[str, Any]] = []
            backend_runtime_params = dict(backend_runtime_by_name.get(backend) or {})
            # region agent log
            _agent_debug_log(
                hypothesis_id="H1,H2,H3",
                location="trainer/training/gbm_bakeoff.py:_run_backend_candidate",
                message="backend candidate started",
                data={
                    "backend": backend,
                    "runtime_params": backend_runtime_params,
                    "run_optuna": bool(run_optuna),
                    "strong_validation": bool(_has_strong_validation(X_val, y_val)),
                    "train_rows": int(len(X_train)),
                    "val_rows": int(len(X_val)),
                    "xgboost_in_sys_modules": "xgboost" in sys.modules,
                    "xgboost_callback_in_sys_modules": "xgboost.callback" in sys.modules,
                },
            )
            # endregion
            backend_runtime_manifest = _backend_runtime_manifest(
                backend,
                backend_runtime_params=backend_runtime_params,
            )
            if backend == "catboost":
                hp_backend_default = _default_backend_hyperparams("catboost")
            else:
                hp_backend_default = _default_backend_hyperparams("xgboost")
            hp_backend = dict((per_backend_hyperparams or {}).get(backend) or hp_backend_default)
            if run_optuna and _has_strong_validation(X_val, y_val):
                budget = resolve_backend_optuna_budget(
                    backend,
                    timeout_budget_divisor=_timeout_budget_divisor,
                )
                hp_backend = run_backend_optuna_search(
                    X_train,
                    y_train,
                    X_val,
                    y_val,
                    sw_train,
                    backend=backend,
                    n_trials=budget.get("n_trials"),
                    label="rated",
                    field_test_constrained_optuna_objective_allowed=field_test_constrained_optuna_objective_allowed,
                    val_window_hours=val_dec026_window_hours,
                    timeout_seconds=budget.get("timeout_seconds"),
                    early_stop_patience=budget.get("early_stop_patience"),
                    hpo_objective_manifest=backend_manifest,
                    backend_runtime_params=backend_runtime_params,
                )
                # region agent log
                _agent_debug_log(
                    hypothesis_id="H2,H3",
                    location="trainer/training/gbm_bakeoff.py:_run_backend_candidate",
                    message="backend optuna completed",
                    data={
                        "backend": backend,
                        "best_param_keys": sorted(hp_backend.keys()),
                        "manifest": backend_manifest,
                    },
                )
                # endregion
            else:
                budget = resolve_backend_optuna_budget(
                    backend,
                    timeout_budget_divisor=_timeout_budget_divisor,
                )
                backend_manifest.append(
                    {
                        "optuna_hpo_backend": backend,
                        "optuna_hpo_enabled": False,
                        "optuna_hpo_n_trials_requested": budget.get("n_trials"),
                        "optuna_hpo_timeout_seconds": budget.get("timeout_seconds"),
                        "optuna_hpo_early_stop_patience": budget.get("early_stop_patience"),
                        "optuna_hpo_objective_mode": "disabled",
                        "optuna_hpo_study_best_trial_value": None,
                    }
                )
            model, metrics = trainer_fn(
                X_train,
                y_train,
                X_val,
                y_val,
                sw_train,
                hp_backend,
                backend_runtime_params=backend_runtime_params,
                val_dec026_window_hours=val_dec026_window_hours,
                val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
            )
            # region agent log
            _agent_debug_log(
                hypothesis_id="H2,H3,H4",
                location="trainer/training/gbm_bakeoff.py:_run_backend_candidate",
                message="backend final train completed",
                data={
                    "backend": backend,
                    "model_type": type(model).__name__,
                    "metric_keys": sorted(metrics.keys()),
                },
            )
            # endregion
            metrics = dict(metrics)
            metrics["best_hyperparams"] = dict(hp_backend)
            metrics.update(backend_runtime_manifest)
            if backend_manifest:
                metrics.update(backend_manifest[0])
            metrics["_val_scores"] = (
                np.asarray(
                    model.predict_proba(_to_float32_frame(X_val))[:, 1],
                    dtype=np.float64,
                )
                if _has_strong_validation(X_val, y_val)
                else np.zeros(len(y_val), dtype=np.float64)
            )
            metrics.update(
                _compute_train_metrics(
                    model,
                    float(metrics["threshold"]),
                    _to_float32_frame(X_train),
                    y_train,
                    label=f"rated_{backend}",
                    log_results=False,
                )
            )
            metrics["_train_scores"] = _batched_model_positive_class_scores(
                model,
                _to_float32_frame(X_train),
                int(getattr(_cfg, "TRAIN_METRICS_PREDICT_BATCH_ROWS", 500_000)),
            )
            if X_test is not None and y_test is not None and not X_test.empty:
                test_metrics = _compute_test_metrics(
                    model,
                    float(metrics["threshold"]),
                    _to_float32_frame(X_test),
                    y_test,
                    label=f"rated_{backend}",
                    _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                    log_results=False,
                    production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                )
                metrics.update(test_metrics)
                metrics["_test_scores"] = np.asarray(
                    model.predict_proba(_to_float32_frame(X_test))[:, 1],
                    dtype=np.float64,
                )
            metrics["feature_importance"] = _compute_feature_importance(model, feature_cols)
            metrics["importance_method"] = "gain"
            metrics["model_backend"] = backend
            metrics["reason_codes_enabled"] = True
            artifact = {
                "model": model,
                "threshold": float(metrics["threshold"]),
                "features": feature_cols,
                "metrics": metrics,
                "model_kind": backend,
                "reason_codes_enabled": True,
            }
            candidate_artifacts[backend] = artifact
            row = {
                "backend": backend,
                **{
                    k: metrics[k]
                    for k in metrics
                    if k not in ("label", "_val_scores", "_train_scores", "_test_scores")
                },
                "label": f"rated_{backend}",
            }
            return backend, artifact, row
        except ImportError as exc:
            # region agent log
            _agent_debug_log(
                hypothesis_id="H1,H4",
                location="trainer/training/gbm_bakeoff.py:_run_backend_candidate",
                message="backend import error",
                data={
                    "backend": backend,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "xgboost_in_sys_modules": "xgboost" in sys.modules,
                    "xgboost_callback_in_sys_modules": "xgboost.callback" in sys.modules,
                    "xgboost_training_in_sys_modules": "xgboost.training" in sys.modules,
                },
            )
            # endregion
            row = {
                "backend": backend,
                "error": f"import_error:{exc}",
                "bakeoff_disposition": "reject",
                **_backend_runtime_manifest(
                    backend,
                    backend_runtime_params=backend_runtime_by_name.get(backend),
                ),
            }
            logger.warning("A3 gbm_bakeoff: %s skipped (%s)", backend, exc)
            return backend, {}, row
        except Exception as exc:
            # region agent log
            _agent_debug_log(
                hypothesis_id="H2,H3,H4",
                location="trainer/training/gbm_bakeoff.py:_run_backend_candidate",
                message="backend non-import error",
                data={
                    "backend": backend,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            # endregion
            row = {
                "backend": backend,
                "error": str(exc),
                "bakeoff_disposition": "reject",
                **_backend_runtime_manifest(
                    backend,
                    backend_runtime_params=backend_runtime_by_name.get(backend),
                ),
            }
            logger.warning("A3 gbm_bakeoff: %s training failed: %s", backend, exc)
            return backend, {}, row

    backend_jobs = (
        (_train_catboost_backend, "catboost"),
        (_train_xgboost_backend, "xgboost"),
    )
    parallel_workers = int(backend_runtime_plan.get("parallel_backend_workers") or 1)
    if parallel_workers > 1:
        _preload_parallel_backend_imports(("catboost", "xgboost"))
        with ThreadPoolExecutor(max_workers=parallel_workers) as pool:
            futures = [
                pool.submit(_run_backend_candidate, trainer_fn, backend)
                for trainer_fn, backend in backend_jobs
            ]
            for fut in as_completed(futures):
                backend, artifact, row = fut.result()
                rows[backend] = row
                if artifact:
                    candidate_artifacts[backend] = artifact
    else:
        for trainer_fn, backend in backend_jobs:
            backend_name, artifact, row = _run_backend_candidate(trainer_fn, backend)
            rows[backend_name] = row
            if artifact:
                candidate_artifacts[backend_name] = artifact

    try:
        soft_row, soft_artifact = _build_soft_vote_candidate(
            candidate_artifacts,
            feature_cols=feature_cols,
            y_val=y_val,
            X_train=_to_float32_frame(X_train),
            y_train=y_train,
            X_test=_to_float32_frame(X_test) if X_test is not None else None,
            y_test=y_test,
            val_dec026_window_hours=val_dec026_window_hours,
            val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
        )
        rows[SOFT_VOTE_BACKEND] = soft_row
        candidate_artifacts[SOFT_VOTE_BACKEND] = soft_artifact
    except Exception as exc:
        rows[SOFT_VOTE_BACKEND] = {
            "backend": SOFT_VOTE_BACKEND,
            "error": str(exc),
            "bakeoff_disposition": "reject",
        }
        logger.warning("A3 gbm_bakeoff: %s build failed: %s", SOFT_VOTE_BACKEND, exc)

    stacking_report: Dict[str, Any] = {
        "status": "skipped",
        "reason": "not_attempted",
    }
    try:
        stacked_row, stacked_artifact, stacking_report = build_stacked_logistic_candidate(
            base_artifacts=candidate_artifacts,
            feature_cols=feature_cols,
            X_train=_to_float32_frame(X_train),
            y_train=y_train,
            rated_train_df=rated_train_df,
            X_val=_to_float32_frame(X_val),
            y_val=y_val,
            X_test=_to_float32_frame(X_test) if X_test is not None else None,
            y_test=y_test,
            val_dec026_window_hours=val_dec026_window_hours,
            val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
        )
        if stacked_row is not None and stacked_artifact is not None:
            rows[STACKED_LOGISTIC_BACKEND] = stacked_row
            candidate_artifacts[STACKED_LOGISTIC_BACKEND] = stacked_artifact
        else:
            rows[STACKED_LOGISTIC_BACKEND] = {
                "backend": STACKED_LOGISTIC_BACKEND,
                "error": str(stacking_report.get("reason") or "stacking_not_built"),
                "bakeoff_disposition": "reject",
            }
    except Exception as exc:
        rows[STACKED_LOGISTIC_BACKEND] = {
            "backend": STACKED_LOGISTIC_BACKEND,
            "error": str(exc),
            "bakeoff_disposition": "reject",
        }
        logger.warning("A3 gbm_bakeoff: %s build failed: %s", STACKED_LOGISTIC_BACKEND, exc)

    winner, rule = _pick_winner(rows)
    _assign_dispositions(rows, winner)
    for backend, row in rows.items():
        if backend in candidate_artifacts:
            candidate_artifacts[backend]["metrics"]["bakeoff_disposition"] = row.get("bakeoff_disposition")
            candidate_artifacts[backend]["metrics"]["model_backend"] = backend

    report: Dict[str, Any] = {
        "schema_version": "a3_v2",
        "winner_backend": winner,
        "selection_rule": rule,
        "selection_mode": "field_test",
        "per_backend": rows,
        "stacking_oof": stacking_report,
        "backend_runtime_plan": {
            "requested_backend_device_mode": backend_runtime_plan.get("requested_backend_device_mode"),
            "effective_backend_device_mode": backend_runtime_plan.get("effective_backend_device_mode"),
            "visible_gpu_ids": list(backend_runtime_plan.get("visible_gpu_ids") or []),
            "gpu_assignments": dict(backend_runtime_plan.get("gpu_assignments") or {}),
            "parallel_backend_workers": int(backend_runtime_plan.get("parallel_backend_workers") or 1),
            "parallel_backend_execution": bool(
                backend_runtime_plan.get("parallel_backend_execution", False)
            ),
        },
        "ensemble_bridge": {
            "same_splits": True,
            "same_time_split": True,
            "same_eval_script": True,
            "same_sample_weight_vector": True,
            "train_rows": int(len(X_train)),
            "valid_rows": int(len(X_val)),
            "test_rows": int(len(y_test)) if y_test is not None else 0,
            "feature_columns": feature_cols,
            "note": (
                "C3 stacking/blending: OOF exports and meta-learner training are not in A3 scope; "
                "this block records aligned backends and metrics for a future ensemble step."
            ),
        },
    }
    for artifact in candidate_artifacts.values():
        metrics_obj = artifact.get("metrics")
        if isinstance(metrics_obj, dict):
            for key in ("_val_scores", "_train_scores", "_test_scores"):
                metrics_obj.pop(key, None)
    logger.info(
        "A3 gbm_bakeoff winner=%s rule=%s catboost=%s xgboost=%s soft_vote=%s stacked=%s",
        winner,
        rule,
        rows.get("catboost", {}).get("bakeoff_disposition"),
        rows.get("xgboost", {}).get("bakeoff_disposition"),
        rows.get(SOFT_VOTE_BACKEND, {}).get("bakeoff_disposition"),
        rows.get(STACKED_LOGISTIC_BACKEND, {}).get("bakeoff_disposition"),
    )
    winner_artifact = candidate_artifacts[winner]
    winner_artifact["metrics"]["gbm_bakeoff_winner_backend"] = winner
    winner_artifact["metrics"]["model_backend"] = winner
    return winner, winner_artifact, report
