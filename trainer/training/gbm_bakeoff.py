"""Precision uplift A3 / R3: always-on GBDT family compare.

Contract
--------
- Same feature matrix, same temporal split, same evaluation helper.
- Same DEC-013 / A2 sample_weight vector for all backends.
- Primary winner key = field-test validation objective (DEC-026 operating point,
  prod-adjusted when the contract allows it), not AP.

The caller passes the already-trained LightGBM artifact (which may have been trained
through in-memory / CSV / LibSVM main paths). This module then optionally trains
CatBoost and/or XGBoost on the same in-memory matrices (see
``GBM_BAKEOFF_ENABLE_CATBOOST`` / ``GBM_BAKEOFF_ENABLE_XGBOOST``; CatBoost defaults from
``trainer.core._config_training_domain`` when the env var is unset; XGBoost is off when
unset) and returns:

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
import os
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
from trainer.training.gbm_bakeoff_disk import GbmBakeoffLibSvmBundle
from trainer.training.phase_e_process_snapshot import log_phase_e_memory
from trainer.training.oof_stacking import build_stacked_logistic_candidate
from trainer.training.threshold_selection import pick_threshold_dec026

logger = logging.getLogger("trainer")

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


def _symmetric_bakeoff_hpo_enabled() -> bool:
    """When true, optional-backend Optuna uses full per-backend timeout (no global divisor)."""
    raw = (os.getenv("GBM_BAKEOFF_SYMMETRIC_HPO") or "").strip().lower()
    return bool(raw) and raw in ("1", "true", "t", "yes", "y")


def _optional_bakeoff_catboost_xgboost_enabled() -> tuple[bool, bool]:
    """Read per-run env toggles for optional A3 backends.

    CatBoost: when ``GBM_BAKEOFF_ENABLE_CATBOOST`` is unset, use
    ``config.GBM_BAKEOFF_ENABLE_CATBOOST`` (domain default). XGBoost: when unset, use
    ``config.GBM_BAKEOFF_ENABLE_XGBOOST`` (domain default on).

    Truthy env values: 1, true, t, yes, y (case-insensitive).
    """
    cat_raw = os.getenv("GBM_BAKEOFF_ENABLE_CATBOOST")
    if cat_raw is None:
        enable_cat = bool(getattr(_cfg, "GBM_BAKEOFF_ENABLE_CATBOOST", False))
    else:
        tok_c = str(cat_raw).strip().lower()
        enable_cat = bool(tok_c) and tok_c in ("1", "true", "t", "yes", "y")
    xgb_raw = os.getenv("GBM_BAKEOFF_ENABLE_XGBOOST")
    if xgb_raw is None:
        enable_xgb = bool(getattr(_cfg, "GBM_BAKEOFF_ENABLE_XGBOOST", True))
    else:
        tok_x = str(xgb_raw).strip().lower()
        enable_xgb = bool(tok_x) and tok_x in ("1", "true", "t", "yes", "y")
    return enable_cat, enable_xgb


def _has_strong_validation(X_val: pd.DataFrame, y_val: pd.Series) -> bool:
    return (
        not X_val.empty
        and len(y_val) >= MIN_VALID_TEST_ROWS
        and int(y_val.isna().sum()) == 0
        and int(y_val.sum()) >= 1
        and int((y_val == 0).sum()) >= 1
    )


def _phase_e_dense_positive_scores(
    model: Any,
    X: pd.DataFrame,
    batch_rows: int,
    *,
    backend: str,
    role: str,
) -> tuple[np.ndarray, str]:
    """Chunked positive-class ``predict_proba`` scores with paired begin/end + batch heartbeats.

    Uses :func:`trainer.training.trainer._batched_model_positive_class_scores` for the
    LightGBM booster fast path; otherwise chunks ``predict_proba`` to reduce peak RAM
    during Step 9 Phase E on laptop-scale runs.
    """
    from trainer.training.model_eval_runtime import _batched_model_positive_class_scores

    t0 = time.perf_counter()
    n = int(len(X))
    br = max(1, int(batch_rows))
    # XGBoost sklearn inference may allocate large transient buffers per call
    # (DMatrix + prediction workspace). Keep Phase E validation/test chunks
    # conservative to reduce silent OOM-kill risk on Windows.
    if str(backend).strip().lower() == "xgboost":
        br = min(br, 100_000)
    ncols = int(X.shape[1]) if getattr(X, "ndim", 2) >= 2 else 0
    batched = n > br
    mode = "in_memory_dense_batched" if batched else "in_memory_dense_single"
    logger.info(
        "A3 PhaseE predict_begin role=%s backend=%s rows=%d cols=%d mode=%s batch_rows=%d",
        role,
        backend,
        n,
        ncols,
        mode,
        br,
    )
    log_phase_e_memory(logger, "predict_begin_after_log", cfg=_cfg, extra={"role": role})
    if n == 0:
        elapsed = time.perf_counter() - t0
        logger.info(
            "A3 PhaseE predict_end role=%s backend=%s len=0 mode=empty elapsed_s=%.3f",
            role,
            backend,
            elapsed,
        )
        return np.asarray([], dtype=np.float64), "in_memory_dense"
    hb_every = max(
        0,
        int(getattr(_cfg, "A3_PHASE_E_PREDICT_HEARTBEAT_EVERY_N_BATCHES", 10)),
    )
    # XGBoost: default heartbeat interval is too sparse for few-batch val runs; log every batch.
    if str(backend).strip().lower() == "xgboost" and hb_every > 0:
        hb_every = 1
    booster = getattr(model, "booster_", None)
    if booster is not None:
        logger.info(
            "A3 PhaseE booster_predict_begin role=%s backend=%s rows=%d batch_rows=%d",
            role,
            backend,
            n,
            br,
        )
        log_phase_e_memory(logger, "booster_predict_begin", cfg=_cfg, extra={"role": role})
        _t_booster = time.perf_counter()
        out = np.asarray(
            _batched_model_positive_class_scores(model, X, br),
            dtype=np.float64,
        ).reshape(-1)
        elapsed = time.perf_counter() - t0
        ds = "in_memory_dense_batched" if batched else "in_memory_dense"
        logger.info(
            "A3 PhaseE predict_end role=%s backend=%s len=%d data_source=%s "
            "engine=lightgbm_booster inner_wall_s=%.3f elapsed_s=%.3f",
            role,
            backend,
            int(len(out)),
            ds,
            time.perf_counter() - _t_booster,
            elapsed,
        )
        log_phase_e_memory(logger, "booster_predict_end", cfg=_cfg, extra={"role": role})
        return out, ds
    parts: list[np.ndarray] = []
    batch_count = 0
    for start in range(0, n, br):
        batch_count += 1
        end_excl = min(start + br, n)
        rows_in_batch = end_excl - start
        _t_batch = time.perf_counter()
        logger.info(
            "A3 PhaseE batch_begin role=%s backend=%s batch_idx=%d start_row=%d end_row=%d "
            "rows_in_batch=%d total_rows=%d",
            role,
            backend,
            batch_count,
            start,
            end_excl,
            rows_in_batch,
            n,
        )
        log_phase_e_memory(
            logger,
            "batch_begin",
            cfg=_cfg,
            extra={"role": role, "batch_idx": batch_count},
        )
        chunk = X.iloc[start:end_excl]
        if bool(getattr(_cfg, "A3_PHASE_E_DIAG_MEMORY_SNAPSHOT", False)) and batch_count == 1:
            _cn = int(chunk.shape[1]) if getattr(chunk, "ndim", 2) >= 2 else 0
            _flat = np.asarray(chunk.to_numpy(dtype=np.float64, copy=False)).ravel()
            _nf = int(np.size(_flat) - int(np.count_nonzero(np.isfinite(_flat))))
            logger.info(
                "A3 PhaseE_diag tag=chunk_numeric_batch1 role=%s cols=%d nonfinite_count=%d",
                role,
                _cn,
                _nf,
            )
        # Avoid up-front full-frame casting; coerce per chunk to keep peak RAM bounded.
        chunk_for_predict = chunk.astype(np.float32, copy=False)
        log_phase_e_memory(
            logger,
            "before_predict_proba",
            cfg=_cfg,
            extra={"role": role, "batch_idx": batch_count},
        )
        raw = model.predict_proba(chunk_for_predict)[:, 1]
        log_phase_e_memory(
            logger,
            "after_predict_proba",
            cfg=_cfg,
            extra={"role": role, "batch_idx": batch_count},
        )
        parts.append(np.asarray(raw, dtype=np.float64).reshape(-1))
        _batch_wall = time.perf_counter() - _t_batch
        logger.info(
            "A3 PhaseE batch_end role=%s backend=%s batch_idx=%d rows_in_batch=%d "
            "batch_wall_s=%.3f cumulative_elapsed_s=%.3f",
            role,
            backend,
            batch_count,
            rows_in_batch,
            _batch_wall,
            time.perf_counter() - t0,
        )
        log_phase_e_memory(
            logger,
            "batch_end",
            cfg=_cfg,
            extra={"role": role, "batch_idx": batch_count},
        )
        processed = end_excl
        if hb_every > 0 and batch_count % hb_every == 0:
            logger.info(
                "A3 PhaseE predict_heartbeat role=%s backend=%s batch_idx=%d "
                "processed_rows=%d/%d elapsed_s=%.3f",
                role,
                backend,
                batch_count,
                processed,
                n,
                time.perf_counter() - t0,
            )
    out = np.concatenate(parts, axis=0)
    elapsed = time.perf_counter() - t0
    ds = "in_memory_dense_batched" if batched else "in_memory_dense"
    logger.info(
        "A3 PhaseE predict_end role=%s backend=%s len=%d data_source=%s "
        "engine=sklearn_chunks batches=%d elapsed_s=%.3f",
        role,
        backend,
        int(len(out)),
        ds,
        batch_count,
        elapsed,
    )
    log_phase_e_memory(logger, "predict_end", cfg=_cfg, extra={"role": role})
    return out, ds


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
        try:
            __import__(backend)
        except ImportError as exc:
            logger.warning("A3 gbm_bakeoff: %s preload failed (%s)", backend, exc)


def _default_backend_hyperparams(backend: str) -> Dict[str, Any]:
    from trainer.training.hpo_runtime import _backend_hpo_defaults

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
    libsvm_bundle: Optional[GbmBakeoffLibSvmBundle] = None,
) -> Tuple[Any, Dict[str, Any]]:
    from catboost import CatBoostClassifier
    from trainer.training.hpo_runtime import (
        _apply_backend_imbalance_params,
        _sanitize_catboost_params_for_runtime,
    )

    if libsvm_bundle is not None and bool(getattr(_cfg, "GBM_BAKEOFF_FROM_FILE", True)):
        try:
            from trainer.training.gbm_bakeoff_disk import (
                catboost_disk_strict_refit_on_train_union_valid,
                train_catboost_from_libsvm_disk,
            )
            from trainer.training.split_file_bundle import trainer_file_backed_strict_enabled

            model_cb, metrics_cb = train_catboost_from_libsvm_disk(
                libsvm_bundle,
                hp,
                y_val=y_val,
                backend_runtime_params=backend_runtime_params,
                val_dec026_window_hours=val_dec026_window_hours,
                val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
                quantize_first=bool(getattr(_cfg, "GBM_BAKEOFF_CATBOOST_QUANTIZE", False)),
            )
            if trainer_file_backed_strict_enabled() and _has_strong_validation(X_val, y_val):
                model_cb = catboost_disk_strict_refit_on_train_union_valid(
                    model_cb,
                    libsvm_bundle,
                    hp,
                    backend_runtime_params=backend_runtime_params,
                    quantize_first=bool(getattr(_cfg, "GBM_BAKEOFF_CATBOOST_QUANTIZE", False)),
                )
                metrics_cb["final_refit_train_valid"] = True
                metrics_cb["final_refit_data_source"] = "libsvm_disk_train_union_valid"
                metrics_cb["final_refit_backend"] = "catboost"
            else:
                metrics_cb["final_refit_train_valid"] = False
                metrics_cb["final_refit_data_source"] = "skipped_non_strict_or_weak_val"
                metrics_cb["final_refit_backend"] = "catboost"
            return model_cb, metrics_cb
        except Exception as exc:
            from trainer.training.split_file_bundle import trainer_file_backed_strict_enabled

            if trainer_file_backed_strict_enabled():
                raise
            logger.warning(
                "A3 CatBoost LibSVM-disk train failed; falling back to in-memory fit: %s",
                exc,
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
    metrics["final_refit_train_valid"] = False
    metrics["final_refit_data_source"] = "in_memory_dense"
    metrics["final_refit_backend"] = "catboost"
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
    libsvm_bundle: Optional[GbmBakeoffLibSvmBundle] = None,
) -> Tuple[Any, Dict[str, Any]]:
    import xgboost as xgb
    from trainer.training.hpo_runtime import _apply_backend_imbalance_params

    if libsvm_bundle is not None and bool(getattr(_cfg, "GBM_BAKEOFF_FROM_FILE", True)):
        try:
            from trainer.training.gbm_bakeoff_disk import (
                train_xgboost_from_libsvm_disk,
                xgboost_disk_strict_refit_on_train_union_valid,
            )
            from trainer.training.split_file_bundle import trainer_file_backed_strict_enabled

            _x_use_ext = bool(getattr(_cfg, "GBM_BAKEOFF_XGBOOST_EXTERNAL_MEMORY", False))
            model_x, metrics_x = train_xgboost_from_libsvm_disk(
                libsvm_bundle,
                hp,
                y_val=y_val,
                backend_runtime_params=backend_runtime_params,
                val_dec026_window_hours=val_dec026_window_hours,
                val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
                use_external_memory=_x_use_ext,
            )
            if trainer_file_backed_strict_enabled() and _has_strong_validation(X_val, y_val):
                model_x = xgboost_disk_strict_refit_on_train_union_valid(
                    model_x,
                    libsvm_bundle,
                    hp,
                    backend_runtime_params=backend_runtime_params,
                    use_external_memory=_x_use_ext,
                )
                metrics_x["final_refit_train_valid"] = True
                metrics_x["final_refit_data_source"] = "libsvm_disk_train_union_valid"
                metrics_x["final_refit_backend"] = "xgboost"
            else:
                metrics_x["final_refit_train_valid"] = False
                metrics_x["final_refit_data_source"] = "skipped_non_strict_or_weak_val"
                metrics_x["final_refit_backend"] = "xgboost"
            return model_x, metrics_x
        except Exception as exc:
            from trainer.training.split_file_bundle import trainer_file_backed_strict_enabled

            if trainer_file_backed_strict_enabled():
                raise
            logger.warning(
                "A3 XGBoost LibSVM-disk train failed; falling back to in-memory fit: %s",
                exc,
            )

    x_hp = dict(hp)
    if backend_runtime_params:
        x_hp.update(dict(backend_runtime_params))
    n_est = int(x_hp.pop("n_estimators"))
    x_hp = _apply_backend_imbalance_params("xgboost", x_hp, y_train)
    model = xgb.XGBClassifier(n_estimators=n_est, **x_hp)
    X_tr = _to_float32_frame(X_train)
    X_vl = _to_float32_frame(X_val)
    _has_val = _has_strong_validation(X_val, y_val)
    logger.info(
        "A3 investigate: xgboost in-memory path n_estimators=%d train_rows=%d "
        "eval_set=%s (starting model.fit).",
        int(n_est),
        int(len(X_tr)),
        _has_val,
    )
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
    logger.info(
        "A3 investigate: xgboost model.fit finished (eval_set was %s); computing val metrics block.",
        _has_val,
    )
    metrics = _val_block_from_scores(
        y_val,
        val_scores,
        hp,
        label="rated_xgboost",
        val_dec026_window_hours=val_dec026_window_hours,
        val_dec026_min_alerts_per_hour=val_dec026_min_alerts_per_hour,
    )
    metrics["final_refit_train_valid"] = False
    metrics["final_refit_data_source"] = "in_memory_dense"
    metrics["final_refit_backend"] = "xgboost"
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


def _soft_vote_component_backends(
    candidate_artifacts: Mapping[str, Dict[str, Any]],
) -> Tuple[str, ...]:
    """Ordered lightgbm → catboost → xgboost subset present in *candidate_artifacts*."""
    order = ("lightgbm", "catboost", "xgboost")
    out: list[str] = []
    for backend in order:
        art = candidate_artifacts.get(backend)
        if isinstance(art, dict) and art.get("model") is not None:
            out.append(backend)
    return tuple(out)


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
    """Create the equal-weight soft-vote from every trained base GBM in the bakeoff."""
    from trainer.training.model_eval_runtime import (
        _compute_feature_importance,
        _compute_test_metrics_from_scores,
        _train_metrics_dict_from_y_scores,
    )

    required = _soft_vote_component_backends(candidate_artifacts)
    if len(required) < 2:
        raise ValueError(
            "soft_vote_equal requires at least 2 trained base backends among "
            "lightgbm/catboost/xgboost; have %s"
            % (",".join(required) if required else "(none)")
        )

    comp_models = [candidate_artifacts[backend]["model"] for backend in required]
    comp_thresholds = [
        float(candidate_artifacts[b]["metrics"].get("threshold", 0.5)) for b in required
    ]
    comp_val_scores = [
        np.asarray(candidate_artifacts[b]["metrics"]["_val_scores"], dtype=np.float64)
        for b in required
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
                np.asarray(candidate_artifacts[b]["metrics"]["_train_scores"], dtype=np.float64)
                for b in required
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
                    np.asarray(candidate_artifacts[b]["metrics"]["_test_scores"], dtype=np.float64)
                    for b in required
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
    libsvm_bundle: Optional[GbmBakeoffLibSvmBundle] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Train base backends + ensemble candidates on aligned splits and return winner + report."""
    from trainer.training.model_eval_runtime import (
        _batched_model_positive_class_scores,
        _compute_feature_importance,
        _compute_test_metrics,
        _compute_test_metrics_from_scores,
        _compute_train_metrics,
        _train_metrics_dict_from_y_scores,
    )
    from trainer.training.trainer import (
        _backend_runtime_manifest,
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
    if "optuna_hpo_data_source" in lightgbm_metrics:
        rows["lightgbm"]["a3_primary_optuna_hpo_data_source"] = lightgbm_metrics["optuna_hpo_data_source"]
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
        if _symmetric_bakeoff_hpo_enabled():
            return None
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
    enable_cat, enable_xgb = _optional_bakeoff_catboost_xgboost_enabled()
    if enable_cat or enable_xgb:
        logger.info(
            "A3 gbm_bakeoff optional backends enabled: catboost=%s xgboost=%s",
            enable_cat,
            enable_xgb,
        )
    else:
        logger.info(
            "A3 gbm_bakeoff: CatBoost and XGBoost disabled for this run (enable via "
            "--gbm-bakeoff-catboost / --gbm-bakeoff-xgboost or GBM_BAKEOFF_ENABLE_*=1)."
        )

    def _run_backend_candidate(
        trainer_fn: Any,
        backend: str,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        try:
            backend_manifest: list[dict[str, Any]] = []
            backend_runtime_params = dict(backend_runtime_by_name.get(backend) or {})
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
                _disk_hpo: Optional[Tuple[Path, Path, int, Tuple[str, ...]]] = None
                if libsvm_bundle is not None:
                    _disk_hpo = (
                        Path(libsvm_bundle.train_libsvm),
                        Path(libsvm_bundle.valid_libsvm),
                        int(libsvm_bundle.train_row_count),
                        tuple(str(x) for x in libsvm_bundle.feature_names),
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
                    libsvm_disk_hpo=_disk_hpo,
                )
                logger.info(
                    "A3 investigate: backend=%s Optuna returned; starting trainer_fn final fit "
                    "(train_rows=%d val_rows=%d).",
                    backend,
                    int(len(X_train)),
                    int(len(X_val)),
                )
                if backend_manifest:
                    backend_manifest[0]["a3_bakeoff_symmetric_hpo"] = bool(
                        _symmetric_bakeoff_hpo_enabled()
                    )
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
                        "optuna_hpo_data_source": "n_a_hpo_disabled",
                        "a3_bakeoff_symmetric_hpo": bool(_symmetric_bakeoff_hpo_enabled()),
                    }
                )
                logger.info(
                    "A3 investigate: backend=%s HPO disabled or weak val; starting trainer_fn final fit "
                    "(train_rows=%d val_rows=%d).",
                    backend,
                    int(len(X_train)),
                    int(len(X_val)),
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
                libsvm_bundle=libsvm_bundle,
            )
            logger.info(
                "A3 investigate: backend=%s trainer_fn returned; attaching metrics / Phase E.",
                backend,
            )
            metrics = dict(metrics)
            metrics["best_hyperparams"] = dict(hp_backend)
            metrics.update(backend_runtime_manifest)
            if backend_manifest:
                metrics.update(backend_manifest[0])
            from trainer.training.split_file_bundle import (
                forbid_file_backed_strict_dense_predict,
                trainer_file_backed_strict_enabled,
            )
            from trainer.training.gbm_bakeoff_disk import (
                phase_e_ap_mode,
                phase_e_predict_batch_rows,
                phase_e_predict_streaming_enabled,
                phase_e_score_memmap_enabled,
                predict_positive_scores_phase_e_libsvm,
            )

            _pe_stream_gate = phase_e_predict_streaming_enabled() or (
                libsvm_bundle is not None and trainer_file_backed_strict_enabled()
            )
            logger.info(
                "A3 investigate: backend=%s Phase E gate libsvm_bundle=%s "
                "phase_e_predict_streaming_enabled=%s strict_stream_gate=%s",
                backend,
                libsvm_bundle is not None,
                phase_e_predict_streaming_enabled(),
                _pe_stream_gate,
            )
            _pe_val_ok = False
            if libsvm_bundle is not None and _pe_stream_gate:
                _t_val_stream = time.perf_counter()
                logger.info(
                    "A3 PhaseE predict_begin role=val backend=%s mode=libsvm_streaming "
                    "batch_rows=%d",
                    backend,
                    int(phase_e_predict_batch_rows()),
                )
                try:
                    _sv, _mv = predict_positive_scores_phase_e_libsvm(
                        backend=backend,
                        model=model,
                        libsvm_path=Path(libsvm_bundle.valid_libsvm),
                        feature_names=feature_cols,
                        cache_dir=libsvm_bundle.cache_dir,
                        batch_rows=phase_e_predict_batch_rows(),
                        use_memmap=phase_e_score_memmap_enabled(),
                        role="valid",
                    )
                    metrics["_val_scores"] = np.asarray(_sv, dtype=np.float64).reshape(-1)
                    metrics["a3_val_scores_data_source"] = "libsvm_streaming"
                    metrics.update({k: v for k, v in _mv.items() if str(k).startswith("a3_")})
                    _pe_val_ok = True
                    logger.info(
                        "A3 PhaseE predict_end role=val backend=%s len=%d mode=libsvm_streaming "
                        "elapsed_s=%.3f",
                        backend,
                        int(len(metrics["_val_scores"])),
                        time.perf_counter() - _t_val_stream,
                    )
                except Exception as _pev_exc:
                    if trainer_file_backed_strict_enabled():
                        raise RuntimeError(
                            "TRAINER_FILE_BACKED_STRICT: Phase E validation LibSVM streaming "
                            f"predict failed ({_pev_exc})"
                        ) from _pev_exc
                    logger.warning("Phase E: validation streaming scores skipped: %s", _pev_exc)
                    logger.info(
                        "A3 PhaseE predict_end role=val backend=%s mode=libsvm_streaming "
                        "status=skipped elapsed_s=%.3f",
                        backend,
                        time.perf_counter() - _t_val_stream,
                    )
            if not _pe_val_ok:
                if hasattr(model, "predict_val_scores_from_libsvm"):
                    _t_vx = time.perf_counter()
                    logger.info(
                        "A3 PhaseE predict_begin role=val backend=%s mode=xgboost_libsvm_uri",
                        backend,
                    )
                    metrics["_val_scores"] = model.predict_val_scores_from_libsvm()
                    metrics["a3_val_scores_data_source"] = "xgboost_libsvm_uri"
                    logger.info(
                        "A3 PhaseE predict_end role=val backend=%s len=%d mode=xgboost_libsvm_uri "
                        "elapsed_s=%.3f",
                        backend,
                        int(len(metrics["_val_scores"])),
                        time.perf_counter() - _t_vx,
                    )
                elif getattr(model, "_gbm_bakeoff_valid_libsvm_uri", None):
                    from catboost import Pool

                    _t_vc = time.perf_counter()
                    logger.info(
                        "A3 PhaseE predict_begin role=val backend=%s mode=catboost_libsvm_pool_uri",
                        backend,
                    )
                    _vuri = str(getattr(model, "_gbm_bakeoff_valid_libsvm_uri"))
                    metrics["_val_scores"] = np.asarray(
                        model.predict_proba(Pool(_vuri))[:, 1],
                        dtype=np.float64,
                    )
                    metrics["a3_val_scores_data_source"] = "catboost_libsvm_pool_uri"
                    logger.info(
                        "A3 PhaseE predict_end role=val backend=%s len=%d mode=catboost_libsvm_pool_uri "
                        "elapsed_s=%.3f",
                        backend,
                        int(len(metrics["_val_scores"])),
                        time.perf_counter() - _t_vc,
                    )
                else:
                    if (
                        libsvm_bundle is not None
                        and trainer_file_backed_strict_enabled()
                        and _has_strong_validation(X_val, y_val)
                    ):
                        forbid_file_backed_strict_dense_predict(
                            role="validation",
                            detail=f"backend={backend}",
                        )
                    if _has_strong_validation(X_val, y_val):
                        _val_batch = int(getattr(_cfg, "TRAIN_METRICS_PREDICT_BATCH_ROWS", 500_000))
                        _vs_arr, _vs_src = _phase_e_dense_positive_scores(
                            model,
                            X_val,
                            _val_batch,
                            backend=backend,
                            role="val",
                        )
                        metrics["_val_scores"] = _vs_arr
                        metrics["a3_val_scores_data_source"] = _vs_src
                    else:
                        _t_vz = time.perf_counter()
                        logger.info(
                            "A3 PhaseE predict_begin role=val backend=%s rows=%d "
                            "mode=zeros_weak_validation",
                            backend,
                            int(len(y_val)),
                        )
                        metrics["_val_scores"] = np.zeros(len(y_val), dtype=np.float64)
                        metrics["a3_val_scores_data_source"] = "in_memory_dense"
                        logger.info(
                            "A3 PhaseE predict_end role=val backend=%s len=%d mode=zeros_weak_validation "
                            "elapsed_s=%.3f",
                            backend,
                            int(len(y_val)),
                            time.perf_counter() - _t_vz,
                        )
            _vs_arr = metrics.get("_val_scores")
            _vs_len = int(len(_vs_arr)) if _vs_arr is not None else -1
            logger.info(
                "A3 investigate: backend=%s val_scores_done a3_val_scores_data_source=%s n=%d",
                backend,
                metrics.get("a3_val_scores_data_source"),
                _vs_len,
            )
            train_thr = float(metrics["threshold"])
            train_scores_disk: Optional[np.ndarray] = None
            _pe_tr_ok = False
            if libsvm_bundle is not None and _pe_stream_gate:
                _t_tr_stream = time.perf_counter()
                logger.info(
                    "A3 PhaseE predict_begin role=train backend=%s mode=libsvm_streaming "
                    "batch_rows=%d",
                    backend,
                    int(phase_e_predict_batch_rows()),
                )
                try:
                    train_scores_disk, _mt = predict_positive_scores_phase_e_libsvm(
                        backend=backend,
                        model=model,
                        libsvm_path=Path(libsvm_bundle.train_libsvm),
                        feature_names=feature_cols,
                        cache_dir=libsvm_bundle.cache_dir,
                        batch_rows=phase_e_predict_batch_rows(),
                        use_memmap=phase_e_score_memmap_enabled(),
                        role="train",
                    )
                    _pe_tr_ok = True
                    logger.info(
                        "A3 PhaseE predict_end role=train backend=%s len=%d mode=libsvm_streaming "
                        "elapsed_s=%.3f",
                        backend,
                        int(len(train_scores_disk)),
                        time.perf_counter() - _t_tr_stream,
                    )
                except Exception as _pet_exc:
                    if trainer_file_backed_strict_enabled():
                        raise RuntimeError(
                            "TRAINER_FILE_BACKED_STRICT: Phase E train LibSVM streaming predict "
                            f"failed ({_pet_exc})"
                        ) from _pet_exc
                    logger.warning("Phase E: train streaming scores skipped: %s", _pet_exc)
                    train_scores_disk = None
                    logger.info(
                        "A3 PhaseE predict_end role=train backend=%s mode=libsvm_streaming "
                        "status=skipped elapsed_s=%.3f",
                        backend,
                        time.perf_counter() - _t_tr_stream,
                    )
            if not _pe_tr_ok:
                if hasattr(model, "predict_train_scores_from_libsvm"):
                    train_scores_disk = np.asarray(
                        model.predict_train_scores_from_libsvm(), dtype=np.float64
                    ).reshape(-1)
                elif getattr(model, "_gbm_bakeoff_train_libsvm_uri", None):
                    from catboost import Pool

                    _tr_uri = str(getattr(model, "_gbm_bakeoff_train_libsvm_uri"))
                    train_scores_disk = np.asarray(
                        model.predict_proba(Pool(_tr_uri))[:, 1],
                        dtype=np.float64,
                    ).reshape(-1)
            if (
                train_scores_disk is None
                and libsvm_bundle is not None
                and trainer_file_backed_strict_enabled()
                and len(X_train) > 0
            ):
                forbid_file_backed_strict_dense_predict(
                    role="train",
                    detail=f"backend={backend}",
                )
            if train_scores_disk is not None:
                metrics.update(
                    _train_metrics_dict_from_y_scores(
                        y_train,
                        train_scores_disk,
                        train_thr,
                        label=f"rated_{backend}",
                        log_results=False,
                        ap_mode=phase_e_ap_mode(),
                    )
                )
                if _pe_tr_ok:
                    metrics["a3_train_metrics_data_source"] = "libsvm_streaming"
                else:
                    metrics["a3_train_metrics_data_source"] = (
                        "xgboost_libsvm_uri"
                        if hasattr(model, "predict_train_scores_from_libsvm")
                        else "catboost_libsvm_pool_uri"
                    )
                metrics["_train_scores"] = np.asarray(train_scores_disk, dtype=np.float32)
            else:
                _tr_batch = int(getattr(_cfg, "TRAIN_METRICS_PREDICT_BATCH_ROWS", 500_000))
                logger.info(
                    "A3 investigate: backend=%s train_scores in_memory_dense_batched "
                    "train_rows=%d predict_batch_rows=%d (before _compute_train_metrics).",
                    backend,
                    int(len(X_train)),
                    _tr_batch,
                )
                metrics.update(
                    _compute_train_metrics(
                        model,
                        train_thr,
                        _to_float32_frame(X_train),
                        y_train,
                        label=f"rated_{backend}",
                        log_results=False,
                    )
                )
                logger.info(
                    "A3 investigate: backend=%s _compute_train_metrics done; "
                    "starting _batched_model_positive_class_scores on train.",
                    backend,
                )
                metrics["_train_scores"] = _batched_model_positive_class_scores(
                    model,
                    _to_float32_frame(X_train),
                    _tr_batch,
                )
                metrics["a3_train_metrics_data_source"] = "in_memory_dense_batched"
                logger.info(
                    "A3 investigate: backend=%s batched train scores done len=%d",
                    backend,
                    int(len(metrics["_train_scores"])),
                )
            if X_test is not None and y_test is not None and not X_test.empty:
                _ts_disk: Optional[np.ndarray] = None
                _pe_te_ok = False
                if (
                    libsvm_bundle is not None
                    and libsvm_bundle.test_libsvm is not None
                    and _pe_stream_gate
                ):
                    _t_te_stream = time.perf_counter()
                    logger.info(
                        "A3 PhaseE predict_begin role=test backend=%s mode=libsvm_streaming "
                        "batch_rows=%d",
                        backend,
                        int(phase_e_predict_batch_rows()),
                    )
                    try:
                        _ts_raw, _mte = predict_positive_scores_phase_e_libsvm(
                            backend=backend,
                            model=model,
                            libsvm_path=Path(libsvm_bundle.test_libsvm),
                            feature_names=feature_cols,
                            cache_dir=libsvm_bundle.cache_dir,
                            batch_rows=phase_e_predict_batch_rows(),
                            use_memmap=phase_e_score_memmap_enabled(),
                            role="test",
                        )
                        _ts_disk = np.asarray(_ts_raw, dtype=np.float64).reshape(-1)
                        metrics.update({k: v for k, v in _mte.items() if str(k).startswith("a3_")})
                        metrics["a3_test_scores_data_source"] = "libsvm_streaming"
                        _pe_te_ok = True
                        logger.info(
                            "A3 PhaseE predict_end role=test backend=%s len=%d mode=libsvm_streaming "
                            "elapsed_s=%.3f",
                            backend,
                            int(len(_ts_disk)),
                            time.perf_counter() - _t_te_stream,
                        )
                    except Exception as _pee_exc:
                        if trainer_file_backed_strict_enabled():
                            raise RuntimeError(
                                "TRAINER_FILE_BACKED_STRICT: Phase E test LibSVM streaming predict "
                                f"failed ({_pee_exc})"
                            ) from _pee_exc
                        logger.warning("Phase E: test streaming scores skipped: %s", _pee_exc)
                        logger.info(
                            "A3 PhaseE predict_end role=test backend=%s mode=libsvm_streaming "
                            "status=skipped elapsed_s=%.3f",
                            backend,
                            time.perf_counter() - _t_te_stream,
                        )
                if not _pe_te_ok and hasattr(model, "predict_test_scores_from_libsvm"):
                    _ts_disk = model.predict_test_scores_from_libsvm()
                    if _ts_disk is not None:
                        metrics["a3_test_scores_data_source"] = "xgboost_libsvm_uri"
                elif not _pe_te_ok and getattr(model, "_gbm_bakeoff_test_libsvm_uri", None):
                    from catboost import Pool

                    _turi = str(getattr(model, "_gbm_bakeoff_test_libsvm_uri"))
                    _ts_disk = np.asarray(
                        model.predict_proba(Pool(_turi))[:, 1],
                        dtype=np.float64,
                    )
                    metrics["a3_test_scores_data_source"] = "catboost_libsvm_pool_uri"
                if _ts_disk is not None:
                    test_metrics = _compute_test_metrics_from_scores(
                        np.asarray(y_test, dtype=float).reshape(-1),
                        np.asarray(_ts_disk, dtype=np.float64).reshape(-1),
                        float(metrics["threshold"]),
                        label=f"rated_{backend}",
                        _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                        log_results=False,
                        production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                    )
                    metrics.update(test_metrics)
                    metrics["_test_scores"] = np.asarray(_ts_disk, dtype=np.float64).reshape(-1)
                elif (
                    libsvm_bundle is not None
                    and libsvm_bundle.test_libsvm is not None
                    and trainer_file_backed_strict_enabled()
                ):
                    forbid_file_backed_strict_dense_predict(
                        role="test",
                        detail=f"backend={backend}",
                    )
                else:
                    _te_batch = int(getattr(_cfg, "TRAIN_METRICS_PREDICT_BATCH_ROWS", 500_000))
                    _test_scores_arr, _test_src = _phase_e_dense_positive_scores(
                        model,
                        X_test,
                        _te_batch,
                        backend=backend,
                        role="test",
                    )
                    test_metrics = _compute_test_metrics_from_scores(
                        np.asarray(y_test, dtype=float).reshape(-1),
                        np.asarray(_test_scores_arr, dtype=np.float64).reshape(-1),
                        float(metrics["threshold"]),
                        label=f"rated_{backend}",
                        _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                        log_results=False,
                        production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                    )
                    metrics.update(test_metrics)
                    metrics["_test_scores"] = np.asarray(
                        _test_scores_arr,
                        dtype=np.float64,
                    ).reshape(-1)
                    metrics["a3_test_scores_data_source"] = _test_src
                logger.info(
                    "A3 investigate: backend=%s test scores / metrics branch finished.",
                    backend,
                )
            logger.info(
                "A3 investigate: backend=%s computing feature_importance (n_features=%d).",
                backend,
                len(feature_cols),
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

    backend_jobs: list[tuple[Any, str]] = []
    if enable_cat:
        backend_jobs.append((_train_catboost_backend, "catboost"))
    if enable_xgb:
        backend_jobs.append((_train_xgboost_backend, "xgboost"))

    # Plan may request parallel_backend_workers>1 when multiple GPUs exist, but if only
    # one optional backend is enabled we still had len(backend_jobs)==1 and used
    # ThreadPoolExecutor — running XGBoost/CatBoost fit on a worker thread triggers
    # native faults on some Windows builds (e.g. STATUS_STACK_BUFFER_OVERRUN 0xC0000409).
    parallel_workers = int(backend_runtime_plan.get("parallel_backend_workers") or 1)
    parallel_workers = min(parallel_workers, len(backend_jobs))
    if sys.platform == "win32":
        # Two native backends concurrently on worker threads also faults (0xC0000409);
        # keep CPU/GPU bakes sequential on Windows. Linux servers retain parallel GPUs.
        parallel_workers = 1
    if backend_jobs and parallel_workers > 1:
        _preload_parallel_backend_imports(tuple(b for _, b in backend_jobs))
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
    elif backend_jobs:
        for trainer_fn, backend in backend_jobs:
            backend_name, artifact, row = _run_backend_candidate(trainer_fn, backend)
            rows[backend_name] = row
            if artifact:
                candidate_artifacts[backend_name] = artifact

    if backend_jobs:
        logger.info(
            "A3 investigate: optional backend job loop finished "
            "(parallel_workers=%s n_jobs=%d); next soft_vote / stacking.",
            parallel_workers,
            len(backend_jobs),
        )

    def _skipped_backend_row(backend: str, hint: str) -> Dict[str, Any]:
        return {
            "backend": backend,
            "error": hint,
            **_backend_runtime_manifest(
                backend,
                backend_runtime_params=backend_runtime_by_name.get(backend),
            ),
        }

    if not enable_cat and "catboost" not in rows:
        rows["catboost"] = _skipped_backend_row(
            "catboost",
            "disabled: set GBM_BAKEOFF_ENABLE_CATBOOST=1 or pass --gbm-bakeoff-catboost",
        )
    if not enable_xgb and "xgboost" not in rows:
        rows["xgboost"] = _skipped_backend_row(
            "xgboost",
            "disabled: set GBM_BAKEOFF_ENABLE_XGBOOST=1 or pass --gbm-bakeoff-xgboost",
        )

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

    logger.info(
        "A3 investigate: ensemble candidates built row_keys=%s; entering _pick_winner.",
        sorted(rows.keys()),
    )
    winner, rule = _pick_winner(rows)
    _assign_dispositions(rows, winner)
    for backend, row in rows.items():
        if backend in candidate_artifacts:
            candidate_artifacts[backend]["metrics"]["bakeoff_disposition"] = row.get("bakeoff_disposition")
            candidate_artifacts[backend]["metrics"]["model_backend"] = backend

    from trainer.training.split_file_bundle import trainer_file_backed_strict_enabled
    from trainer.training.gbm_bakeoff_disk import (
        phase_e_ap_mode,
        phase_e_predict_batch_rows,
        phase_e_predict_streaming_enabled,
        phase_e_score_memmap_enabled,
    )

    report: Dict[str, Any] = {
        "schema_version": "a3_v2",
        "winner_backend": winner,
        "selection_rule": rule,
        "selection_mode": "field_test",
        "a3_symmetric_hpo_profile": bool(_symmetric_bakeoff_hpo_enabled()),
        "a3_optuna_timeout_budget_divisor": _timeout_budget_divisor,
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
            "libsvm_disk": {
                "from_file_enabled": bool(getattr(_cfg, "GBM_BAKEOFF_FROM_FILE", True)),
                "bundle_passed": libsvm_bundle is not None,
                "cache_dir": (
                    str(libsvm_bundle.cache_dir.resolve()) if libsvm_bundle is not None else None
                ),
                "xgboost_external_memory": bool(
                    getattr(_cfg, "GBM_BAKEOFF_XGBOOST_EXTERNAL_MEMORY", False)
                ),
                "catboost_quantize": bool(getattr(_cfg, "GBM_BAKEOFF_CATBOOST_QUANTIZE", False)),
                "trainer_file_backed_strict": bool(trainer_file_backed_strict_enabled()),
                "phase_e_predict_streaming": bool(phase_e_predict_streaming_enabled()),
                "phase_e_score_memmap": bool(phase_e_score_memmap_enabled()),
                "phase_e_predict_batch_rows": int(phase_e_predict_batch_rows()),
                "phase_e_ap_mode": phase_e_ap_mode(),
            },
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
