"""OOF stacking helpers for rated-model bakeoff.

This module implements PIT-safe expanding-monthly OOF generation and a
LogisticRegression meta-learner over base-model probabilities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score

from trainer.core import config as _cfg
from trainer.core.model_wrappers import LogisticStackedModel
from trainer.training.threshold_selection import pick_threshold_dec026

logger = logging.getLogger("trainer")

_BASE_BACKENDS: Tuple[str, ...] = ("lightgbm", "catboost", "xgboost")


def _cfg_oof_enabled() -> bool:
    return bool(getattr(_cfg, "OOF_STACKING_ENABLED", True))


def _cfg_oof_min_folds() -> int:
    return int(getattr(_cfg, "OOF_STACKING_MIN_FOLDS", 2))


def _cfg_oof_holdout_months() -> int:
    return int(getattr(_cfg, "OOF_STACKING_HOLDOUT_MONTHS", 1))


def _cfg_oof_min_valid_positives() -> int:
    return int(getattr(_cfg, "OOF_STACKING_MIN_VALID_POSITIVES", 1))


def _cfg_oof_max_months() -> Optional[int]:
    return getattr(_cfg, "OOF_STACKING_MAX_MONTHS", None)


@dataclass(frozen=True)
class OOFFold:
    train_idx: np.ndarray
    valid_idx: np.ndarray
    train_end: pd.Timestamp
    valid_start: pd.Timestamp
    valid_end: pd.Timestamp
    valid_month: pd.Timestamp


def _ensure_payout_ts(df: pd.DataFrame) -> pd.Series:
    if "payout_complete_dtm" not in df.columns:
        raise ValueError("OOF stacking requires 'payout_complete_dtm' in rated rows.")
    ts = pd.to_datetime(df["payout_complete_dtm"], errors="coerce")
    if ts.isna().any():
        raise ValueError("OOF stacking found invalid payout_complete_dtm values.")
    return ts


def build_expanding_monthly_folds(
    rated_df: pd.DataFrame,
    *,
    holdout_months: Optional[int] = None,
    min_valid_positives: Optional[int] = None,
    max_months: Optional[int] = None,
) -> Tuple[List[OOFFold], Dict[str, Any]]:
    """Build PIT-safe expanding monthly folds on a time-ordered rated frame."""
    if holdout_months is None:
        holdout_months = _cfg_oof_holdout_months()
    if min_valid_positives is None:
        min_valid_positives = _cfg_oof_min_valid_positives()
    if max_months is None:
        max_months = _cfg_oof_max_months()
    if rated_df.empty:
        return [], {"reason": "empty_rated_df"}
    if holdout_months < 1:
        holdout_months = 1
    ts = _ensure_payout_ts(rated_df)
    months = ts.dt.to_period("M").dt.to_timestamp()
    unique_months = sorted(months.unique().tolist())
    if max_months is not None and isinstance(max_months, int) and max_months > 0:
        unique_months = unique_months[-max_months:]
    if len(unique_months) <= (holdout_months + 1):
        return [], {
            "reason": "insufficient_months",
            "n_months": int(len(unique_months)),
            "holdout_months": int(holdout_months),
        }
    usable_months = unique_months[:-holdout_months]
    month_to_idx = {
        m: np.flatnonzero((months == m).to_numpy())  # month-local index sets
        for m in usable_months
    }
    folds: List[OOFFold] = []
    skipped: List[Dict[str, Any]] = []
    for i in range(1, len(usable_months)):
        valid_month = usable_months[i]
        train_months = usable_months[:i]
        tr_parts = [month_to_idx[m] for m in train_months]
        if not tr_parts:
            skipped.append({"valid_month": str(valid_month), "reason": "empty_train_months"})
            continue
        train_idx = np.concatenate(tr_parts)
        valid_idx = month_to_idx[valid_month]
        if len(valid_idx) == 0:
            skipped.append({"valid_month": str(valid_month), "reason": "empty_valid_month"})
            continue
        y_valid = pd.to_numeric(rated_df.iloc[valid_idx]["label"], errors="coerce").fillna(0)
        if int((y_valid == 1).sum()) < int(max(1, min_valid_positives)):
            skipped.append(
                {
                    "valid_month": str(valid_month),
                    "reason": "insufficient_valid_positives",
                    "valid_rows": int(len(valid_idx)),
                    "valid_positives": int((y_valid == 1).sum()),
                }
            )
            continue
        valid_ts = ts.iloc[valid_idx]
        train_ts = ts.iloc[train_idx]
        fold = OOFFold(
            train_idx=train_idx,
            valid_idx=valid_idx,
            train_end=pd.Timestamp(train_ts.max()),
            valid_start=pd.Timestamp(valid_ts.min()),
            valid_end=pd.Timestamp(valid_ts.max()),
            valid_month=pd.Timestamp(valid_month),
        )
        # PIT hard assertion: no temporal overlap.
        if not (fold.train_end < fold.valid_start):
            skipped.append(
                {
                    "valid_month": str(valid_month),
                    "reason": "pit_monotonicity_failed",
                    "train_end": str(fold.train_end),
                    "valid_start": str(fold.valid_start),
                }
            )
            continue
        folds.append(fold)
    meta = {
        "scheme": "expanding_monthly",
        "n_months_total": int(len(unique_months)),
        "n_months_usable": int(len(usable_months)),
        "holdout_months": int(holdout_months),
        "n_folds_built": int(len(folds)),
        "skipped_folds": skipped,
    }
    return folds, meta


def _predict_scores(model: Any, X: pd.DataFrame) -> np.ndarray:
    raw = model.predict_proba(X)
    arr = np.asarray(raw, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 2:
        raise ValueError(f"predict_proba output shape invalid: {arr.shape!r}")
    return arr[:, 1].reshape(-1)


def _add_field_test_primary_keys(metrics: Dict[str, Any], y_val: pd.Series) -> None:
    val_precision = float(metrics.get("val_precision") or 0.0)
    ya = np.asarray(y_val, dtype=float).reshape(-1)
    pos = int(np.sum(ya == 1.0))
    neg = int(np.sum(ya == 0.0))
    val_np_ratio = float(neg / pos) if pos > 0 and neg > 0 else None
    prod_ratio = getattr(_cfg, "PRODUCTION_NEG_POS_RATIO", None)
    val_primary_adj: Optional[float] = None
    if (
        prod_ratio is not None
        and val_np_ratio is not None
        and val_precision > 0.0
        and np.isfinite(val_precision)
        and np.isfinite(float(prod_ratio))
        and float(prod_ratio) > 0.0
    ):
        p = min(1.0, val_precision)
        scaling = float(prod_ratio) / float(val_np_ratio)
        denom = 1.0 + ((1.0 / p) - 1.0) * scaling
        if np.isfinite(denom) and denom > 0:
            val_primary_adj = float(np.clip(1.0 / denom, 0.0, 1.0))
    metrics["val_neg_pos_ratio"] = val_np_ratio
    metrics["val_field_test_primary_score"] = (
        float(val_primary_adj) if val_primary_adj is not None else float(val_precision)
    )
    metrics["val_field_test_primary_score_mode"] = (
        "precision_prod_adjusted" if val_primary_adj is not None else "precision_raw"
    )


def _val_block_from_scores(
    y_val: pd.Series,
    val_scores: np.ndarray,
    *,
    label: str,
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
) -> Dict[str, Any]:
    min_rows = int(getattr(_cfg, "MIN_VALID_TEST_ROWS", 50))
    _has_val = (
        len(y_val) >= min_rows
        and int(y_val.isna().sum()) == 0
        and int(y_val.sum()) >= 1
        and int((y_val == 0).sum()) >= 1
    )
    if _has_val and y_val.sum() > 0:
        prauc = float(average_precision_score(y_val, val_scores))
        _pick = pick_threshold_dec026(
            np.asarray(y_val, dtype=float),
            np.asarray(val_scores, dtype=float),
            recall_floor=getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01),
            min_alert_count=int(getattr(_cfg, "THRESHOLD_MIN_ALERT_COUNT", 5)),
            min_alerts_per_hour=val_dec026_min_alerts_per_hour,
            window_hours=val_dec026_window_hours,
            fbeta_beta=float(getattr(_cfg, "THRESHOLD_FBETA", 0.5)),
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
        "val_random_ap": (n_val_pos / n_val) if n_val > 0 else 0.0,
        "_uncalibrated": not _has_val,
    }
    if val_dec026_window_hours is not None and val_dec026_min_alerts_per_hour is not None:
        out["val_dec026_pick_window_hours"] = float(val_dec026_window_hours)
        out["val_dec026_pick_min_alerts_per_hour"] = float(val_dec026_min_alerts_per_hour)
    _add_field_test_primary_keys(out, y_val)
    return out


def _stacking_candidate_from_scores(
    *,
    base_artifacts: Mapping[str, Dict[str, Any]],
    feature_cols: Sequence[str],
    y_val: pd.Series,
    val_scores: np.ndarray,
    y_train: pd.Series,
    train_scores: np.ndarray,
    X_test: Optional[pd.DataFrame],
    y_test: Optional[pd.Series],
    test_scores: Optional[np.ndarray],
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
    oof_report: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    from trainer.training.trainer import (
        _compute_feature_importance,
        _compute_test_metrics_from_scores,
        _train_metrics_dict_from_y_scores,
    )

    base_models = [base_artifacts[k]["model"] for k in _BASE_BACKENDS]
    meta_model = oof_report["meta_model"]
    wrapper = LogisticStackedModel(
        base_models=base_models,
        feature_names=list(feature_cols),
        component_backends=list(_BASE_BACKENDS),
        meta_model=meta_model,
    )
    metrics = _val_block_from_scores(
        y_val,
        val_scores,
        label="rated_stacked_logistic_oof",
        val_dec026_window_hours=val_dec026_window_hours,
        val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
    )
    metrics.update(
        _train_metrics_dict_from_y_scores(
            y_train,
            train_scores,
            float(metrics["threshold"]),
            label="rated_stacked_logistic_oof",
            log_results=False,
        )
    )
    if X_test is not None and y_test is not None and test_scores is not None and len(test_scores) > 0:
        metrics.update(
            _compute_test_metrics_from_scores(
                np.asarray(y_test, dtype=np.float64),
                test_scores,
                float(metrics["threshold"]),
                label="rated_stacked_logistic_oof",
                _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                log_results=False,
                production_neg_pos_ratio=getattr(_cfg, "PRODUCTION_NEG_POS_RATIO", None),
            )
        )
    metrics["feature_importance"] = _compute_feature_importance(wrapper, list(feature_cols))
    metrics["importance_method"] = "meta_weighted_component_gain"
    metrics["model_backend"] = "stacked_logistic_oof"
    metrics["reason_codes_enabled"] = False
    metrics["stacking"] = {
        "meta_learner": "logistic_regression",
        "component_backends": list(_BASE_BACKENDS),
        "oof_fold_report": {k: v for k, v in oof_report.items() if k != "meta_model"},
    }
    artifact = {
        "model": wrapper,
        "threshold": float(metrics["threshold"]),
        "features": list(feature_cols),
        "metrics": metrics,
        "model_kind": "stacked_logistic_oof",
        "reason_codes_enabled": False,
        "component_backends": list(_BASE_BACKENDS),
    }
    row = {
        "backend": "stacked_logistic_oof",
        **{
            k: v
            for k, v in metrics.items()
            if k not in ("_val_scores", "_train_scores", "_test_scores")
        },
        "label": "rated_stacked_logistic_oof",
    }
    return row, artifact


def build_stacked_logistic_candidate(
    *,
    base_artifacts: Mapping[str, Dict[str, Any]],
    feature_cols: Sequence[str],
    X_train: pd.DataFrame,
    y_train: pd.Series,
    rated_train_df: Optional[pd.DataFrame],
    X_val: pd.DataFrame,
    y_val: pd.Series,
    X_test: Optional[pd.DataFrame],
    y_test: Optional[pd.Series],
    val_dec026_window_hours: Optional[float],
    val_dec026_min_alerts_per_hour: Optional[float],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    """Create stacked_logistic_oof candidate using expanding-monthly OOF."""
    report: Dict[str, Any] = {
        "stacking_enabled": bool(_cfg_oof_enabled()),
        "scheme": "expanding_monthly",
        "status": "skipped",
        "reason": None,
        "pit_contract": {
            "fold_time_monotonicity_required": True,
            "train_end_strictly_before_valid_start": True,
            "fold_cutoff_scope": "rated_train_df.payout_complete_dtm",
            "identity_mapping_scope": "global_train_end_phase1_limitation",
        },
    }
    if not _cfg_oof_enabled():
        report["reason"] = "disabled_by_config"
        return None, None, report
    missing = [k for k in _BASE_BACKENDS if k not in base_artifacts]
    if missing:
        report["reason"] = f"missing_base_backends:{','.join(missing)}"
        return None, None, report
    if rated_train_df is None or rated_train_df.empty:
        report["reason"] = "rated_train_df_missing"
        return None, None, report
    if len(rated_train_df) != len(X_train) or len(y_train) != len(X_train):
        report["reason"] = "train_view_length_mismatch"
        report["rated_train_rows"] = int(len(rated_train_df))
        report["x_train_rows"] = int(len(X_train))
        report["y_train_rows"] = int(len(y_train))
        return None, None, report
    for col in ("label", "payout_complete_dtm"):
        if col not in rated_train_df.columns:
            report["reason"] = f"rated_train_df_missing_col:{col}"
            return None, None, report
    folds, fold_meta = build_expanding_monthly_folds(rated_train_df)
    report["fold_builder"] = fold_meta
    if len(folds) < int(max(1, _cfg_oof_min_folds())):
        report["reason"] = "insufficient_effective_folds"
        report["required_min_folds"] = int(max(1, _cfg_oof_min_folds()))
        report["effective_folds"] = int(len(folds))
        return None, None, report
    n = len(rated_train_df)
    z_oof = np.full((n, len(_BASE_BACKENDS)), np.nan, dtype=np.float64)
    y_oof = np.full(n, np.nan, dtype=np.float64)
    fold_summaries: List[Dict[str, Any]] = []
    for fold_id, fold in enumerate(folds, start=1):
        x_tr = X_train.iloc[fold.train_idx]
        x_vl = X_train.iloc[fold.valid_idx]
        y_vl = pd.to_numeric(y_train.iloc[fold.valid_idx], errors="coerce").fillna(0).to_numpy(dtype=np.float64)
        y_oof[fold.valid_idx] = y_vl
        for b_idx, backend in enumerate(_BASE_BACKENDS):
            mdl = base_artifacts[backend]["model"]
            z_oof[fold.valid_idx, b_idx] = _predict_scores(mdl, x_vl)
        fold_summaries.append(
            {
                "fold_id": int(fold_id),
                "valid_month": str(fold.valid_month.date()),
                "train_rows": int(len(fold.train_idx)),
                "valid_rows": int(len(fold.valid_idx)),
                "valid_positives": int(np.sum(y_vl == 1.0)),
                "train_end": str(fold.train_end),
                "valid_start": str(fold.valid_start),
            }
        )
    valid_mask = np.isfinite(y_oof) & np.isfinite(z_oof).all(axis=1)
    if int(np.sum(valid_mask)) < int(getattr(_cfg, "MIN_VALID_TEST_ROWS", 50)):
        report["reason"] = "insufficient_oof_rows"
        report["oof_rows"] = int(np.sum(valid_mask))
        return None, None, report
    z_fit = z_oof[valid_mask]
    y_fit = y_oof[valid_mask]
    if int(np.sum(y_fit == 1.0)) < 1 or int(np.sum(y_fit == 0.0)) < 1:
        report["reason"] = "single_class_oof"
        return None, None, report
    meta = LogisticRegression(
        random_state=42,
        solver="lbfgs",
        max_iter=200,
    )
    meta.fit(z_fit, y_fit.astype(int))
    # Final stacked scores across standard evaluated splits.
    z_train_all = np.column_stack([_predict_scores(base_artifacts[b]["model"], X_train) for b in _BASE_BACKENDS])
    train_scores = np.asarray(meta.predict_proba(z_train_all)[:, 1], dtype=np.float64)
    z_val = np.column_stack([_predict_scores(base_artifacts[b]["model"], X_val) for b in _BASE_BACKENDS])
    val_scores = np.asarray(meta.predict_proba(z_val)[:, 1], dtype=np.float64)
    test_scores: Optional[np.ndarray] = None
    if X_test is not None and y_test is not None and not X_test.empty:
        z_test = np.column_stack([_predict_scores(base_artifacts[b]["model"], X_test) for b in _BASE_BACKENDS])
        test_scores = np.asarray(meta.predict_proba(z_test)[:, 1], dtype=np.float64)
    report.update(
        {
            "status": "built",
            "reason": None,
            "effective_folds": int(len(folds)),
            "oof_rows": int(np.sum(valid_mask)),
            "oof_positive_rows": int(np.sum(y_fit == 1.0)),
            "oof_neg_rows": int(np.sum(y_fit == 0.0)),
            "folds": fold_summaries,
            "meta_coef": np.asarray(meta.coef_, dtype=np.float64).reshape(-1).tolist(),
            "meta_intercept": np.asarray(meta.intercept_, dtype=np.float64).reshape(-1).tolist(),
        }
    )
    oof_report_for_metrics = dict(report)
    oof_report_for_metrics["meta_model"] = meta
    row, artifact = _stacking_candidate_from_scores(
        base_artifacts=base_artifacts,
        feature_cols=feature_cols,
        y_val=y_val,
        val_scores=val_scores,
        y_train=y_train,
        train_scores=train_scores,
        X_test=X_test,
        y_test=y_test,
        test_scores=test_scores,
        val_dec026_window_hours=val_dec026_window_hours,
        val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
        oof_report=oof_report_for_metrics,
    )
    return row, artifact, report
