"""trainer/training/model_eval_runtime.py
============================================
Train/valid/test metric helpers and batched predict paths split from
``trainer/training/trainer.py`` (trainer refactor plan Phase C surface).

Depends on ``trainer.core.config`` and ``trainer.training.metrics_eval`` only
(no import of ``trainer.training.trainer``).
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, List, Optional, Union

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve

from trainer.core import config as _cfg
from trainer.training.metrics_eval import (
    _precision_prod_adjusted,
    _warn_if_invalid_production_neg_pos_ratio,
)

logger = logging.getLogger("trainer")


def _is_lightgbm_booster(booster: Any) -> bool:
    """Return True only for LightGBM core Booster (ndarray batch ``predict``).

    Do not use substring heuristics on ``__module__``: A3 disk path may expose an
    ``xgboost.core.Booster`` via ``model.booster_`` (e.g. ``XGBoostBoosterDiskClassifier``),
    which must use ``predict_proba`` / DMatrix instead of ndarray ``predict``.
    """
    return isinstance(booster, lgb.Booster)


def _is_xgboost_booster(booster: Any) -> bool:
    """Return True for ``xgboost`` core Booster without importing ``xgboost`` at import time."""
    if booster is None:
        return False
    mod = str(getattr(type(booster), "__module__", "") or "")
    return mod.startswith("xgboost.") and type(booster).__name__ == "Booster"


def _xgboost_booster_from_model(model: Any) -> Any:
    """Resolve ``xgboost.core.Booster`` from ``booster_`` or ``get_booster()``."""
    b = getattr(model, "booster_", None)
    if _is_xgboost_booster(b):
        return b
    getter = getattr(model, "get_booster", None)
    if callable(getter):
        try:
            out = getter()
        except Exception:
            return None
        return out if _is_xgboost_booster(out) else None
    return None


def _xgboost_dmatrix_feature_names(model: Any, booster: Any) -> Optional[List[str]]:
    """Feature name list for ``DMatrix``; ``None`` lets XGBoost infer from data only."""
    raw = getattr(model, "_feature_names", None)
    if isinstance(raw, (list, tuple)) and raw:
        return [str(x) for x in raw]
    fin = getattr(model, "feature_names_in_", None)
    if fin is not None:
        return [str(x) for x in list(fin)]
    bfn = getattr(booster, "feature_names", None)
    if isinstance(bfn, (list, tuple)) and bfn:
        return [str(x) for x in bfn]
    return None


def _lgb_booster_feature_name_list(booster: Any) -> List[str]:
    """Return LightGBM Booster feature names across versions.

    Newer wheels expose ``feature_names()``; older builds used ``feature_name()``.
    """
    fn = getattr(booster, "feature_names", None)
    if callable(fn):
        return list(fn())
    fn_legacy = getattr(booster, "feature_name", None)
    if callable(fn_legacy):
        return list(fn_legacy())
    return []


MIN_VALID_TEST_ROWS: int = int(getattr(_cfg, "MIN_VALID_TEST_ROWS", 50))
THRESHOLD_FBETA: float = float(getattr(_cfg, "THRESHOLD_FBETA", 0.5))
TRAIN_METRICS_PREDICT_BATCH_ROWS: int = int(
    getattr(_cfg, "TRAIN_METRICS_PREDICT_BATCH_ROWS", 500_000)
)


def _field_test_hpo_min_alerts_per_hour_for_reports() -> float:
    """Floor used for field-test alert-density reporting (aligns with Optuna DEC-026 guard)."""
    raw = getattr(_cfg, "FIELD_TEST_HPO_MIN_ALERTS_PER_HOUR", 50.0)
    try:
        mf = float(raw)
    except (TypeError, ValueError):
        mf = 50.0
    if not math.isfinite(mf) or mf <= 0.0:
        mf = 50.0
    return mf

def _split_alert_density_prefixed_dict(
    split: str,
    *,
    scores: Optional[np.ndarray],
    threshold: float,
    window_hours: Optional[float],
    objective_min: float,
) -> dict[str, Any]:
    """Flat keys ``{split}_window_hours``, ``{split}_alerts``, … for ``training_metrics`` v2 datasets."""
    pfx = str(split or "").strip().lower()
    if not pfx:
        return {}
    obj_out: Optional[float] = None
    if math.isfinite(objective_min) and objective_min > 0.0:
        obj_out = float(objective_min)
    wh_out: Optional[float] = None
    if window_hours is not None and math.isfinite(float(window_hours)) and float(window_hours) > 0.0:
        wh_out = float(window_hours)
    alerts: Optional[int] = None
    aph: Optional[float] = None
    meets: Optional[bool] = None
    if scores is not None:
        if isinstance(scores, np.memmap):
            thr = float(threshold)
            br = 500_000
            n_tot = int(len(scores))
            c_alerts = 0
            for st in range(0, n_tot, br):
                sc = np.asarray(scores[st : st + br], dtype=np.float64)
                c_alerts += int(np.sum(np.isfinite(sc) & (sc >= thr)))
            alerts = int(c_alerts)
        else:
            s = np.asarray(scores, dtype=np.float64).reshape(-1)
            alerts = int(np.sum(np.isfinite(s) & (s >= float(threshold))))
        if wh_out is not None:
            aph = float(alerts) / float(wh_out)
            if obj_out is not None:
                meets = bool(aph >= float(obj_out))
    return {
        f"{pfx}_window_hours": wh_out,
        f"{pfx}_alerts": alerts,
        f"{pfx}_alerts_per_hour": aph,
        f"{pfx}_min_alerts_per_hour_objective": obj_out,
        f"{pfx}_alerts_per_hour_meets_objective": meets,
    }

def _compute_test_metrics(
    model: Any,
    threshold: float,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    label: str = "",
    _uncalibrated: bool = False,
    log_results: bool = True,
    production_neg_pos_ratio: Optional[float] = None,
    *,
    test_window_hours: Optional[float] = None,
) -> dict:
    """Evaluate a trained model on the held-out test set at the val-derived threshold.

    Uses the same MIN_VALID_TEST_ROWS guard as _train_one_model so an under-sized
    test split returns zeroed metrics rather than crashing.  test_ap is computed
    without any threshold so it is comparable to val_ap.

    R1100: requires at least one negative label so average precision is meaningful.
    R1101: _uncalibrated=True is propagated into test_threshold_uncalibrated key.
    R1105: y_test.values is used for positional comparisons to avoid index misalign.

    Additional reporting:
    - test_precision_at_recall_{r}: highest precision achievable at recall >= r,
      computed from the PR curve (threshold-free). Reported for r in (0.001, 0.01, 0.1, 0.5) (DEC-026).
    - threshold_at_recall_{r}, n_alerts_at_recall_{r}: operating point at that precision; alerts_per_minute_at_recall_{r} is None (trainer has no test window length).
    - test_precision_prod_adjusted: test_precision rescaled to the assumed production
      neg/pos ratio (production_neg_pos_ratio). Only computed when
      production_neg_pos_ratio is not None and > 0.
    - test_precision_at_recall_{r}_prod_adjusted: same closed-form rescaling applied to each
      test_precision_at_recall_{r} (approximation at that PR operating point; None when not JSON-safe).
    """
    _TARGET_RECALLS = (0.001, 0.01, 0.1, 0.5)  # DEC-026
    _zeroed_recall_keys: dict = {
        f"test_precision_at_recall_{r}": None for r in _TARGET_RECALLS
    }
    for r in _TARGET_RECALLS:
        _zeroed_recall_keys[f"threshold_at_recall_{r}"] = None
        _zeroed_recall_keys[f"n_alerts_at_recall_{r}"] = None
        _zeroed_recall_keys[f"alerts_per_minute_at_recall_{r}"] = None
        _zeroed_recall_keys[f"test_precision_at_recall_{r}_prod_adjusted"] = None
    _obj_ft = _field_test_hpo_min_alerts_per_hour_for_reports()
    _dens_none = _split_alert_density_prefixed_dict(
        "test",
        scores=None,
        threshold=float(threshold),
        window_hours=test_window_hours,
        objective_min=_obj_ft,
    )

    # R1100: guard against all-positive labels (average_precision_score = 1.0 trivially)
    _has_test = (
        not X_test.empty
        and len(y_test) >= MIN_VALID_TEST_ROWS
        and int(y_test.isna().sum()) == 0
        and int(y_test.sum()) >= 1
        and int((y_test == 0).sum()) >= 1
    )
    if not _has_test:
        logger.warning(
            "%s: test set too small or unbalanced (%d rows, %d positives, %d negatives)"
            " — test metrics will be zero.",
            label or "model",
            len(y_test),
            int(y_test.sum()) if not y_test.empty else 0,
            int((y_test == 0).sum()) if not y_test.empty else 0,
        )
        n_te = int(len(y_test))
        n_te_pos = int(y_test.sum()) if not y_test.empty else 0
        return {
            "test_ap": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            # R1101: propagate uncalibrated flag
            "test_threshold_uncalibrated": _uncalibrated,
            **_zeroed_recall_keys,
            "test_precision_prod_adjusted": None,
            "test_neg_pos_ratio": None,
            "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
            **_dens_none,
        }

    test_scores = model.predict_proba(X_test)[:, 1]
    if not np.isfinite(test_scores).all():
        logger.warning(
            "%s: test predict_proba scores contain non-finite values — test metrics will be zero.",
            label or "model",
        )
        n_te = int(len(y_test))
        n_te_pos = int(y_test.sum()) if not y_test.empty else 0
        return {
            "test_ap": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            "test_threshold_uncalibrated": _uncalibrated,
            **_zeroed_recall_keys,
            "test_precision_prod_adjusted": None,
            "test_neg_pos_ratio": None,
            "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
            **_dens_none,
        }

    prauc = float(average_precision_score(y_test, test_scores))
    preds = (test_scores >= threshold).astype(int)
    # R1105: use .values to prevent pandas index misalignment with numpy preds array
    y_arr = y_test.values
    tp = int(((preds == 1) & (y_arr == 1)).sum())
    fp = int(((preds == 1) & (y_arr == 0)).sum())
    fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    n_te = int(len(y_test))
    n_te_pos = int(y_test.sum())
    n_te_neg = int((y_test == 0).sum())
    test_random_ap = (n_te_pos / n_te) if n_te > 0 else 0.0
    test_neg_pos_ratio: Optional[float] = (n_te_neg / n_te_pos) if n_te_pos > 0 else None

    # --- Precision at fixed recall levels (threshold-free, from PR curve) ---
    # For each target recall R, find the maximum precision among all PR-curve
    # points where recall >= R; also record threshold and n_alerts at that point (DEC-026).
    pr_prec_arr, pr_rec_arr, pr_thresholds = precision_recall_curve(y_test, test_scores)
    pr_prec = pr_prec_arr[:-1]
    pr_rec = pr_rec_arr[:-1]
    precision_at_recall: dict = {}
    for r in _TARGET_RECALLS:
        mask = pr_rec >= r
        if mask.any():
            valid_idx = np.where(mask)[0]
            best_local = int(np.argmax(pr_prec[valid_idx]))
            best_idx = int(valid_idx[best_local])
            thr_r = float(pr_thresholds[best_idx])
            n_alerts_r = int((test_scores >= thr_r).sum())
            precision_at_recall[f"test_precision_at_recall_{r}"] = float(pr_prec[best_idx])
            precision_at_recall[f"threshold_at_recall_{r}"] = thr_r
            precision_at_recall[f"n_alerts_at_recall_{r}"] = n_alerts_r
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None  # trainer has no test window
        else:
            precision_at_recall[f"test_precision_at_recall_{r}"] = None
            precision_at_recall[f"threshold_at_recall_{r}"] = None
            precision_at_recall[f"n_alerts_at_recall_{r}"] = None
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None

    for r in _TARGET_RECALLS:
        _raw_par = precision_at_recall.get(f"test_precision_at_recall_{r}")
        precision_at_recall[f"test_precision_at_recall_{r}_prod_adjusted"] = _precision_prod_adjusted(
            float(_raw_par) if _raw_par is not None else None,
            production_neg_pos_ratio=production_neg_pos_ratio,
            test_neg_pos_ratio=test_neg_pos_ratio,
        )

    # --- Production-prior adjusted precision ---
    # Rescales test precision to the expected production neg/pos ratio using the
    # Bayes-consistent approximation: 1/P - 1 scales linearly with neg/pos ratio.
    # Only meaningful when negatives were downsampled (neg_sample_frac < 1.0) and
    # production_neg_pos_ratio is provided.
    test_precision_prod_adjusted = _precision_prod_adjusted(
        prec,
        production_neg_pos_ratio=production_neg_pos_ratio,
        test_neg_pos_ratio=test_neg_pos_ratio,
    )
    _warn_if_invalid_production_neg_pos_ratio(production_neg_pos_ratio)

    if log_results:
        _adj_str = (
            f"  prec_prod_adj={test_precision_prod_adjusted:.4f}"
            if test_precision_prod_adjusted is not None
            else ""
        )
        _par_str = "  ".join(
            f"prec@rec{r}={precision_at_recall[f'test_precision_at_recall_{r}']:.4f}"
            if precision_at_recall[f"test_precision_at_recall_{r}"] is not None
            else f"prec@rec{r}=N/A"
            for r in _TARGET_RECALLS
        )
        _thr_apm_str = "  ".join(
            f"thr@rec{r}={precision_at_recall[f'threshold_at_recall_{r}']:.4f} n={precision_at_recall[f'n_alerts_at_recall_{r}']}"
            if precision_at_recall[f"threshold_at_recall_{r}"] is not None
            else f"thr@rec{r}=N/A"
            for r in _TARGET_RECALLS
        )
        logger.info(
            "%s test: AP=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f%s",
            label, prauc, f1, prec, rec, threshold, _adj_str,
        )
        logger.info("%s test PR-curve: %s", label, _par_str)
        logger.info("%s test thr/n_alerts@rec: %s", label, _thr_apm_str)
    return {
        "test_ap": prauc,
        "test_precision": prec,
        "test_recall": rec,
        "test_f1": f1,
        "test_samples": n_te,
        "test_positives": n_te_pos,
        "test_random_ap": test_random_ap,
        # R1101: propagate uncalibrated flag so downstream can distrust P/R/F1
        "test_threshold_uncalibrated": _uncalibrated,
        **precision_at_recall,
        "test_precision_prod_adjusted": test_precision_prod_adjusted,
        "test_neg_pos_ratio": test_neg_pos_ratio,
        "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
        **_split_alert_density_prefixed_dict(
            "test",
            scores=np.asarray(test_scores, dtype=np.float64).reshape(-1),
            threshold=float(threshold),
            window_hours=test_window_hours,
            objective_min=_obj_ft,
        ),
    }


def _compute_test_metrics_from_scores(
    y_test: np.ndarray,
    test_scores: np.ndarray,
    threshold: float,
    label: str = "",
    _uncalibrated: bool = False,
    log_results: bool = True,
    production_neg_pos_ratio: Optional[float] = None,
    *,
    test_window_hours: Optional[float] = None,
) -> dict:
    """Compute test-set metrics from precomputed scores (PLAN B+ 階段 6 第 3 步: test from file).

    Same keys as _compute_test_metrics; used when test labels and predictions come from
    LibSVM file (no X_test in memory). y_test and test_scores must be 1d arrays of same length.
    """
    _TARGET_RECALLS = (0.001, 0.01, 0.1, 0.5)  # DEC-026
    _zeroed_recall_keys = {f"test_precision_at_recall_{r}": None for r in _TARGET_RECALLS}
    for r in _TARGET_RECALLS:
        _zeroed_recall_keys[f"threshold_at_recall_{r}"] = None
        _zeroed_recall_keys[f"n_alerts_at_recall_{r}"] = None
        _zeroed_recall_keys[f"alerts_per_minute_at_recall_{r}"] = None
        _zeroed_recall_keys[f"test_precision_at_recall_{r}_prod_adjusted"] = None
    _obj_ft_fs = _field_test_hpo_min_alerts_per_hour_for_reports()
    _dens_none_fs = _split_alert_density_prefixed_dict(
        "test",
        scores=None,
        threshold=float(threshold),
        window_hours=test_window_hours,
        objective_min=_obj_ft_fs,
    )
    y_arr = np.asarray(y_test).reshape(-1)
    scores_arr = np.asarray(test_scores).reshape(-1)
    if len(y_arr) != len(scores_arr):
        n = min(len(y_arr), len(scores_arr))
        y_arr = y_arr[:n]
        scores_arr = scores_arr[:n]
    n_te = int(len(y_arr))
    n_te_pos = int(np.nansum(y_arr))
    n_te_neg = int(np.sum(np.asarray(y_arr == 0, dtype=float)))
    test_neg_pos_ratio: Optional[float] = (n_te_neg / n_te_pos) if n_te_pos > 0 else None
    _has_test = (
        n_te >= MIN_VALID_TEST_ROWS
        and int(np.isnan(y_arr).sum()) == 0
        and n_te_pos >= 1
        and n_te_neg >= 1
    )
    if not _has_test:
        logger.warning(
            "%s: test from file too small or unbalanced (%d rows, %d pos, %d neg) — test metrics zero.",
            label or "model", n_te, n_te_pos, n_te_neg,
        )
        return {
            "test_ap": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            "test_threshold_uncalibrated": _uncalibrated,
            **_zeroed_recall_keys,
            "test_precision_prod_adjusted": None,
            "test_neg_pos_ratio": None,
            "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
            **_dens_none_fs,
        }
    if not np.isfinite(scores_arr).all():
        logger.warning(
            "%s: test scores (from file) contain non-finite values — test metrics will be zero.",
            label or "model",
        )
        return {
            "test_ap": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            "test_threshold_uncalibrated": _uncalibrated,
            **_zeroed_recall_keys,
            "test_precision_prod_adjusted": None,
            "test_neg_pos_ratio": None,
            "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
            **_dens_none_fs,
        }
    prauc = float(average_precision_score(y_arr, scores_arr))
    preds = (scores_arr >= threshold).astype(int)
    tp = int(((preds == 1) & (y_arr == 1)).sum())
    fp = int(((preds == 1) & (y_arr == 0)).sum())
    fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    test_random_ap = (n_te_pos / n_te) if n_te > 0 else 0.0
    pr_prec_arr, pr_rec_arr, pr_thresholds = precision_recall_curve(y_arr, scores_arr)
    pr_prec = pr_prec_arr[:-1]
    pr_rec = pr_rec_arr[:-1]
    precision_at_recall: dict[str, Optional[float]] = {}
    for r in _TARGET_RECALLS:
        mask = pr_rec >= r
        if mask.any():
            valid_idx = np.where(mask)[0]
            best_local = int(np.argmax(pr_prec[valid_idx]))
            best_idx = int(valid_idx[best_local])
            thr_r = float(pr_thresholds[best_idx])
            n_alerts_r = int((scores_arr >= thr_r).sum())
            precision_at_recall[f"test_precision_at_recall_{r}"] = float(pr_prec[best_idx])
            precision_at_recall[f"threshold_at_recall_{r}"] = thr_r
            precision_at_recall[f"n_alerts_at_recall_{r}"] = n_alerts_r
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None
        else:
            precision_at_recall[f"test_precision_at_recall_{r}"] = None
            precision_at_recall[f"threshold_at_recall_{r}"] = None
            precision_at_recall[f"n_alerts_at_recall_{r}"] = None
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None
    for r in _TARGET_RECALLS:
        _raw_par = precision_at_recall.get(f"test_precision_at_recall_{r}")
        precision_at_recall[f"test_precision_at_recall_{r}_prod_adjusted"] = _precision_prod_adjusted(
            float(_raw_par) if _raw_par is not None else None,
            production_neg_pos_ratio=production_neg_pos_ratio,
            test_neg_pos_ratio=test_neg_pos_ratio,
        )
    test_precision_prod_adjusted = _precision_prod_adjusted(
        prec,
        production_neg_pos_ratio=production_neg_pos_ratio,
        test_neg_pos_ratio=test_neg_pos_ratio,
    )
    _warn_if_invalid_production_neg_pos_ratio(production_neg_pos_ratio)
    if log_results:
        _adj_str = f"  prec_prod_adj={test_precision_prod_adjusted:.4f}" if test_precision_prod_adjusted is not None else ""
        logger.info(
            "%s test (from file): AP=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f%s",
            label, prauc, f1, prec, rec, threshold, _adj_str,
        )
    return {
        "test_ap": prauc,
        "test_precision": prec,
        "test_recall": rec,
        "test_f1": f1,
        "test_samples": n_te,
        "test_positives": n_te_pos,
        "test_random_ap": test_random_ap,
        "test_threshold_uncalibrated": _uncalibrated,
        **precision_at_recall,
        "test_precision_prod_adjusted": test_precision_prod_adjusted,
        "test_neg_pos_ratio": test_neg_pos_ratio,
        "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
        **_split_alert_density_prefixed_dict(
            "test",
            scores=np.asarray(scores_arr, dtype=np.float64).reshape(-1),
            threshold=float(threshold),
            window_hours=test_window_hours,
            objective_min=_obj_ft_fs,
        ),
    }


def _compute_valid_metrics_from_scores(
    y_valid: Union[np.ndarray, pd.Series],
    valid_scores: np.ndarray,
    threshold: float,
) -> dict[str, Any]:
    """Compute lightweight validation metrics from precomputed scores."""
    y_arr = np.asarray(y_valid, dtype=float).reshape(-1)
    s_arr = np.asarray(valid_scores, dtype=float).reshape(-1)
    if len(y_arr) != len(s_arr):
        n = min(len(y_arr), len(s_arr))
        y_arr = y_arr[:n]
        s_arr = s_arr[:n]
    n_val = int(len(y_arr))
    n_val_pos = int(np.sum(y_arr == 1))
    n_val_neg = int(np.sum(y_arr == 0))
    if n_val == 0:
        return {
            "val_ap": 0.0,
            "val_precision": 0.0,
            "val_recall": 0.0,
            "val_f1": 0.0,
            "val_fbeta_05": 0.0,
            "val_samples": 0,
            "val_positives": 0,
            "val_random_ap": 0.0,
        }
    has_both = n_val_pos >= 1 and n_val_neg >= 1 and np.isfinite(s_arr).all()
    val_ap = float(average_precision_score(y_arr == 1, s_arr)) if has_both else 0.0
    preds = (s_arr >= float(threshold)).astype(int)
    tp = int(((preds == 1) & (y_arr == 1)).sum())
    fp = int(((preds == 1) & (y_arr == 0)).sum())
    fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    beta = float(THRESHOLD_FBETA)
    b2 = beta * beta
    fbeta = ((1.0 + b2) * prec * rec / (b2 * prec + rec)) if (b2 * prec + rec) > 0 else 0.0
    return {
        "val_ap": val_ap,
        "val_precision": prec,
        "val_recall": rec,
        "val_f1": f1,
        "val_fbeta_05": fbeta,
        "val_samples": n_val,
        "val_positives": n_val_pos,
        "val_random_ap": (n_val_pos / n_val) if n_val > 0 else 0.0,
    }


def _dataframe_for_lgb_predict(
    model: Any,
    df: pd.DataFrame,
    avail_cols: List[str],
) -> pd.DataFrame:
    """Return a DataFrame with columns matching the booster's feature names for predict (e.g. f0..f49 when trained from LibSVM without feature_name)."""
    X = df[avail_cols]
    booster = getattr(model, "booster_", None)
    if booster is None or not avail_cols:
        return X
    fnames = _lgb_booster_feature_name_list(booster)
    if not fnames or fnames[0] != "f0" or len(fnames) != len(avail_cols):
        return X
    X = X.copy()
    X.columns = fnames
    return X


def _batched_booster_predict_scores(
    booster: lgb.Booster,
    X_train: pd.DataFrame,
    batch_rows: int,
) -> np.ndarray:
    """Chunked ``booster.predict`` on in-memory features (DEC-031 / T-DEC031).

    Avoids sklearn ``predict_proba`` allocating one huge dense probability matrix for
    large training sets when only the positive-class score is needed.
    """
    n = int(len(X_train))
    if n == 0:
        return np.asarray([], dtype=np.float64)
    br = max(1, int(batch_rows))
    parts: list[np.ndarray] = []
    for start in range(0, n, br):
        chunk = X_train.iloc[start : start + br]
        arr = np.ascontiguousarray(chunk.to_numpy(dtype=np.float32, copy=True))
        raw = booster.predict(arr)
        pa = np.asarray(raw).reshape(-1)
        parts.append(pa.astype(np.float64, copy=False))
    return np.concatenate(parts, axis=0)


def _batched_xgboost_booster_predict_scores(
    booster: Any,
    X: pd.DataFrame,
    batch_rows: int,
    feature_names: Optional[List[str]],
) -> np.ndarray:
    """Chunked ``Booster.predict`` via ``DMatrix`` (positive class, binary logistic)."""
    import xgboost as xgb

    n = int(len(X))
    if n == 0:
        return np.asarray([], dtype=np.float64)
    n_feat = int(X.shape[1])
    if feature_names is not None and len(feature_names) != n_feat:
        raise ValueError(
            "XGBoost DMatrix batch: feature_names length mismatch "
            f"(got {len(feature_names)} names vs {n_feat} columns)."
        )
    br = max(1, int(batch_rows))
    fn_arg: Optional[List[str]] = feature_names if feature_names else None
    parts: list[np.ndarray] = []
    for start in range(0, n, br):
        chunk = X.iloc[start : start + br]
        arr = np.ascontiguousarray(chunk.to_numpy(dtype=np.float32, copy=True))
        dm = xgb.DMatrix(arr, feature_names=fn_arg)
        raw = booster.predict(dm)
        pa = np.asarray(raw, dtype=np.float64).reshape(-1)
        parts.append(pa)
    return np.concatenate(parts, axis=0)


def _batched_model_positive_class_scores(
    model: Any,
    X: pd.DataFrame,
    batch_rows: int,
) -> np.ndarray:
    """Chunked positive-class scores for any sklearn-like classifier.

    LightGBM ``booster_``: ndarray batch ``predict``. XGBoost core ``Booster``:
    chunked ``DMatrix`` ``predict`` (avoids sklearn allocating one huge proba matrix).
    Other backends: chunked ``predict_proba``.
    """
    booster = getattr(model, "booster_", None)
    if booster is not None and _is_lightgbm_booster(booster):
        return _batched_booster_predict_scores(booster, X, batch_rows)
    xgb_booster = booster if _is_xgboost_booster(booster) else _xgboost_booster_from_model(model)
    if _is_xgboost_booster(xgb_booster):
        names = _xgboost_dmatrix_feature_names(model, xgb_booster)
        try:
            return _batched_xgboost_booster_predict_scores(
                xgb_booster, X, batch_rows, names
            )
        except Exception as exc:
            logger.warning(
                "XGBoost batched DMatrix predict failed; falling back to predict_proba: %s",
                exc,
            )
    n = int(len(X))
    if n == 0:
        return np.asarray([], dtype=np.float64)
    br = max(1, int(batch_rows))
    parts: list[np.ndarray] = []
    for start in range(0, n, br):
        chunk = X.iloc[start : start + br]
        raw = model.predict_proba(chunk)[:, 1]
        parts.append(np.asarray(raw, dtype=np.float64).reshape(-1))
    return np.concatenate(parts, axis=0)


def _histogram_average_precision_streaming(
    y_arr: np.ndarray,
    scores: Union[np.ndarray, np.memmap],
    *,
    n_bins: int = 256,
    chunk_rows: int = 500_000,
) -> float:
    """Binned AP estimate for large / memmap-backed scores (Phase E ``approx_histogram``)."""
    n = int(len(y_arr))
    pos_total = int(np.sum(y_arr == 1.0))
    neg_total = int(np.sum(y_arr == 0.0))
    if pos_total < 1 or neg_total < 1:
        return 0.0
    smin = math.inf
    smax = -math.inf
    for st in range(0, n, chunk_rows):
        sc = np.asarray(scores[st : st + chunk_rows], dtype=np.float64)
        if sc.size == 0:
            continue
        smin = min(smin, float(np.min(sc)))
        smax = max(smax, float(np.max(sc)))
    if not math.isfinite(smin) or not math.isfinite(smax) or smax <= smin:
        smax = smin + 1e-9
    edges = np.linspace(smin, smax, num=n_bins + 1, dtype=np.float64)
    pos_hist = np.zeros(n_bins, dtype=np.float64)
    neg_hist = np.zeros(n_bins, dtype=np.float64)
    for st in range(0, n, chunk_rows):
        yc = np.asarray(y_arr[st : st + chunk_rows], dtype=np.float64)
        sc = np.asarray(scores[st : st + chunk_rows], dtype=np.float64)
        m1 = yc == 1.0
        m0 = yc == 0.0
        if bool(np.any(m1)):
            pos_hist += np.histogram(sc[m1], bins=edges)[0].astype(np.float64)
        if bool(np.any(m0)):
            neg_hist += np.histogram(sc[m0], bins=edges)[0].astype(np.float64)
    ap = 0.0
    r_prev = 0.0
    cum_pos = 0.0
    cum_neg = 0.0
    for b in range(n_bins - 1, -1, -1):
        cum_pos += float(pos_hist[b])
        cum_neg += float(neg_hist[b])
        den = cum_pos + cum_neg
        prec = float(cum_pos / den) if den > 0.0 else 0.0
        rec = float(cum_pos / pos_total) if pos_total > 0 else 0.0
        ap += max(0.0, rec - r_prev) * prec
        r_prev = rec
    return float(ap)


def _tp_fp_fn_at_threshold_streaming(
    y_arr: np.ndarray,
    scores: Union[np.ndarray, np.memmap],
    threshold: float,
    *,
    chunk_rows: int = 500_000,
) -> tuple[int, int, int]:
    """Threshold confusion counts without full dense score materialisation."""
    n = int(len(y_arr))
    tp = fp = fn = 0
    thr = float(threshold)
    for st in range(0, n, chunk_rows):
        yc = np.asarray(y_arr[st : st + chunk_rows], dtype=float)
        sc = np.asarray(scores[st : st + chunk_rows], dtype=np.float64)
        pr = sc >= thr
        tp += int(np.sum(pr & (yc == 1.0)))
        fp += int(np.sum(pr & (yc == 0.0)))
        fn += int(np.sum((~pr) & (yc == 1.0)))
    return tp, fp, fn


def _train_metrics_dict_from_y_scores(
    y_train: Union[np.ndarray, pd.Series],
    train_scores: Union[np.ndarray, np.memmap],
    threshold: float,
    label: str = "",
    log_results: bool = True,
    *,
    train_window_hours: Optional[float] = None,
    ap_mode: Optional[str] = None,
) -> dict:
    """Build train_* metrics from parallel label/score arrays (same rules as legacy train metrics)."""
    _obj_ft = _field_test_hpo_min_alerts_per_hour_for_reports()
    y_arr = np.asarray(y_train, dtype=float).reshape(-1)
    scores_arr = train_scores  # may be memmap float32 (Phase E)
    if len(y_arr) != len(scores_arr):
        n_fix = min(len(y_arr), len(scores_arr))
        y_arr = y_arr[:n_fix]
        scores_arr = scores_arr[:n_fix]
    n_tr = int(len(y_arr))
    mode_raw = ap_mode if ap_mode is not None else (os.getenv("GBM_BAKEOFF_AP_MODE") or "legacy")
    ap_mode_eff = str(mode_raw).strip().lower()
    if ap_mode_eff not in ("legacy", "approx_histogram", "exact_external_sort"):
        ap_mode_eff = "legacy"
    if n_tr == 0:
        return {
            "train_ap": 0.0,
            "train_precision": 0.0,
            "train_recall": 0.0,
            "train_f1": 0.0,
            "train_samples": 0,
            "train_positives": 0,
            "train_random_ap": 0.0,
            "a3_ap_mode": ap_mode_eff,
            **_split_alert_density_prefixed_dict(
                "train",
                scores=None,
                threshold=float(threshold),
                window_hours=train_window_hours,
                objective_min=_obj_ft,
            ),
        }
    n_tr_pos = int(np.sum(y_arr == 1))
    train_random_ap = (n_tr_pos / n_tr) if n_tr > 0 else 0.0
    _fin_ok = True
    _chk = 500_000
    if isinstance(scores_arr, np.memmap) or n_tr > _chk:
        for st in range(0, n_tr, _chk):
            sc = np.asarray(scores_arr[st : st + _chk], dtype=np.float64)
            if sc.size and not np.isfinite(sc).all():
                _fin_ok = False
                break
    else:
        _fin_ok = bool(np.isfinite(np.asarray(scores_arr, dtype=np.float64).reshape(-1)).all())
    if not _fin_ok:
        logger.warning(
            "%s train: scores contain non-finite values — train metrics set to zero.",
            label or "model",
        )
        return {
            "train_ap": 0.0,
            "train_precision": 0.0,
            "train_recall": 0.0,
            "train_f1": 0.0,
            "train_samples": n_tr,
            "train_positives": n_tr_pos,
            "train_random_ap": train_random_ap,
            "a3_ap_mode": ap_mode_eff,
            **_split_alert_density_prefixed_dict(
                "train",
                scores=None,
                threshold=float(threshold),
                window_hours=train_window_hours,
                objective_min=_obj_ft,
            ),
        }
    has_both = n_tr_pos >= 1 and (n_tr - n_tr_pos) >= 1
    # sklearn average_precision_score requires binary {0,1} y; use strict-positive mask only for AP.
    y_ap = np.asarray(y_arr == 1, dtype=np.float64).reshape(-1)
    if has_both:
        if ap_mode_eff == "approx_histogram":
            train_prauc = float(
                _histogram_average_precision_streaming(y_arr, scores_arr, n_bins=256, chunk_rows=500_000)
            )
        else:
            train_prauc = float(
                average_precision_score(y_ap, np.asarray(scores_arr, dtype=np.float64).reshape(-1))
            )
    else:
        train_prauc = 0.0
    if isinstance(scores_arr, np.memmap) or n_tr > 1_000_000:
        tp, fp, fn = _tp_fp_fn_at_threshold_streaming(y_arr, scores_arr, float(threshold))
    else:
        scores_dense = np.asarray(scores_arr, dtype=np.float64).reshape(-1)
        preds = (scores_dense >= float(threshold)).astype(int)
        tp = int(((preds == 1) & (y_arr == 1)).sum())
        fp = int(((preds == 1) & (y_arr == 0)).sum())
        fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    if log_results:
        logger.info(
            "%s train: AP=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  random_ap=%.4f",
            label, train_prauc, f1, prec, rec, train_random_ap,
        )
    return {
        "train_ap": train_prauc,
        "train_precision": prec,
        "train_recall": rec,
        "train_f1": f1,
        "train_samples": n_tr,
        "train_positives": n_tr_pos,
        "train_random_ap": train_random_ap,
        "a3_ap_mode": ap_mode_eff,
        **_split_alert_density_prefixed_dict(
            "train",
            scores=scores_arr,
            threshold=float(threshold),
            window_hours=train_window_hours,
            objective_min=_obj_ft,
        ),
    }


def _compute_train_metrics(
    model: Any,
    threshold: float,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    label: str = "",
    log_results: bool = True,
    *,
    train_window_hours: Optional[float] = None,
) -> dict:
    """Evaluate a trained model on the training set (for reporting overfit / fit quality).

    Reports train_ap, P/R/F1 at the validation-derived threshold, train_samples,
    train_positives, and train_random_ap (positives/samples = theoretical AP for random guess).
    """
    if X_train.empty or y_train.empty:
        return {
            "train_ap": 0.0,
            "train_precision": 0.0,
            "train_recall": 0.0,
            "train_f1": 0.0,
            "train_samples": 0,
            "train_positives": 0,
            "train_random_ap": 0.0,
            **_split_alert_density_prefixed_dict(
                "train",
                scores=None,
                threshold=float(threshold),
                window_hours=train_window_hours,
                objective_min=_field_test_hpo_min_alerts_per_hour_for_reports(),
            ),
        }
    try:
        batched = _batched_model_positive_class_scores(
            model, X_train, TRAIN_METRICS_PREDICT_BATCH_ROWS
        )
        return _train_metrics_dict_from_y_scores(
            y_train,
            batched,
            threshold,
            label=label,
            log_results=log_results,
            train_window_hours=train_window_hours,
        )
    except Exception as exc:
        logger.warning(
            "Train metrics: batched positive-class predict failed (%s); falling back to predict_proba.",
            exc,
        )
    train_scores = model.predict_proba(X_train)[:, 1]
    return _train_metrics_dict_from_y_scores(
        y_train,
        np.asarray(train_scores, dtype=np.float64).reshape(-1),
        threshold,
        label=label,
        log_results=log_results,
        train_window_hours=train_window_hours,
    )


def _compute_feature_importance(
    model: Any,
    feature_cols: List[str],
) -> list:
    """Return features ranked by LightGBM 'gain' importance (descending).

    Each entry has importance_gain_pct: share of total gain as a percentage (0–100).
    Uses the booster's native feature_importance(importance_type='gain'); falls back
    to sklearn-style .feature_importances_ when the booster attribute is absent
    (AttributeError), e.g. in unit tests with mock estimators.

    R1102: raises ValueError if importance vector length != feature_cols length.
    R1103: only AttributeError triggers fallback; other exceptions propagate.
    """
    try:
        booster = model.booster_
        names: List[str] = _lgb_booster_feature_name_list(booster)
        gains = booster.feature_importance(importance_type="gain").tolist()
    except AttributeError:
        # Fallback for mock / non-LightGBM models (no booster_ attribute).
        names = list(feature_cols)
        # sklearn uses ndarray .tolist(); XGBoostBoosterDiskClassifier returns a plain list.
        raw = model.feature_importances_  # type: ignore[union-attr]
        gains = np.asarray(raw, dtype=np.float64).reshape(-1).tolist()
        # R1102: guard against silent truncation by zip when lengths differ
        if len(gains) != len(names):
            raise ValueError(
                f"_compute_feature_importance: feature_importances_ length ({len(gains)}) "
                f"!= feature_cols length ({len(names)}). "
                "Ensure the model was trained with the same feature list."
            )

    total_gain = sum(gains)
    ranked = sorted(zip(names, gains), key=lambda x: x[1], reverse=True)
    return [
        {
            "rank": i + 1,
            "feature": name,
            "importance_gain_pct": round(100.0 * float(gain) / total_gain, 2) if total_gain > 0 else 0.0,
        }
        for i, (name, gain) in enumerate(ranked)
    ]
