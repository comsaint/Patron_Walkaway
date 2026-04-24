"""Precision uplift R2: optional ranking-focused sample_weight recipes (A2).

Recipes adjust **training** sample_weight on rated rows only (DEC-013 run-level
weights remain the base).  Intended for exploratory / comparative runs; keep
multipliers conservative to avoid silent loss blow-ups on laptop-scale data.

Environment fallback: ``PRECISION_UPLIFT_RANKING_RECIPE`` when CLI does not pass
``--ranking-recipe``. When both are unset, the default recipe is ``r2_top_band_light``
(DEC-044); use ``baseline`` explicitly to disable A2-style reweighting.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("trainer")

RANKING_RECIPE_BASELINE = "baseline"
RANKING_RECIPE_TOP_BAND = "r2_top_band_light"
RANKING_RECIPE_HNM = "r2_hnm_light"
RANKING_RECIPE_COMBINED = "r2_combined_light"

# When CLI and PRECISION_UPLIFT_RANKING_RECIPE are both unset / empty (DEC-044).
RANKING_RECIPE_DEFAULT: str = RANKING_RECIPE_TOP_BAND

VALID_RANKING_RECIPES: frozenset[str] = frozenset(
    {
        RANKING_RECIPE_BASELINE,
        RANKING_RECIPE_TOP_BAND,
        RANKING_RECIPE_HNM,
        RANKING_RECIPE_COMBINED,
    }
)

RANKING_RECIPE_ENV = "PRECISION_UPLIFT_RANKING_RECIPE"

_DEFAULT_PROXY_MAX_COLS = 64


def resolve_ranking_recipe(cli_value: Optional[str]) -> str:
    """Return a normalized recipe id (defaults to ``RANKING_RECIPE_DEFAULT``)."""
    raw = (cli_value if cli_value is not None else os.environ.get(RANKING_RECIPE_ENV, "")).strip().lower()
    if not raw:
        raw = RANKING_RECIPE_DEFAULT
    if raw not in VALID_RANKING_RECIPES:
        logger.warning(
            "Unknown %s=%r; using %r. Valid: %s",
            RANKING_RECIPE_ENV,
            raw,
            RANKING_RECIPE_BASELINE,
            ", ".join(sorted(VALID_RANKING_RECIPES)),
        )
        raw = RANKING_RECIPE_BASELINE
    return raw


def _numeric_proxy(
    df: pd.DataFrame,
    feature_cols: Sequence[str],
    *,
    max_cols: int = _DEFAULT_PROXY_MAX_COLS,
) -> np.ndarray:
    """Row-wise sum of numeric feature values (NaN treated as 0) for top-band masks."""
    cols: list[str] = []
    for c in feature_cols:
        if c not in df.columns or c in ("label", "is_rated"):
            continue
        if len(cols) >= max_cols:
            break
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    if not cols:
        return np.zeros(len(df), dtype=float)
    mat = df[cols].to_numpy(dtype=float, copy=False)
    return np.nan_to_num(mat, nan=0.0, posinf=0.0, neginf=0.0).sum(axis=1)


def apply_top_band_reweighting(
    train_rated: pd.DataFrame,
    base_sw: pd.Series,
    feature_cols: Sequence[str],
    *,
    neg_high_quantile: float = 0.90,
    neg_mult: float = 1.6,
    pos_high_quantile: float = 0.75,
    pos_mult: float = 1.15,
) -> Tuple[pd.Series, Dict[str, Any]]:
    """Upweight high-proxy negatives (FP-prone region) and mid-high positives."""
    sw = base_sw.astype(float).copy()
    y = train_rated["label"].to_numpy(dtype=float)
    proxy = _numeric_proxy(train_rated, feature_cols)
    neg_mask = y == 0.0
    pos_mask = y == 1.0
    n_tb_neg = 0
    n_tb_pos = 0
    if neg_mask.any():
        thr_n = float(np.quantile(proxy[neg_mask], neg_high_quantile))
        sel = neg_mask & (proxy >= thr_n)
        n_tb_neg = int(sel.sum())
        sw.loc[sel] *= neg_mult
    if pos_mask.any():
        thr_p = float(np.quantile(proxy[pos_mask], pos_high_quantile))
        selp = pos_mask & (proxy >= thr_p)
        n_tb_pos = int(selp.sum())
        sw.loc[selp] *= pos_mult
    meta = {
        "ranking_recipe_top_band_neg_boosted": n_tb_neg,
        "ranking_recipe_top_band_pos_boosted": n_tb_pos,
        "ranking_recipe_proxy_cols_used": min(len(feature_cols), _DEFAULT_PROXY_MAX_COLS),
    }
    return sw, meta


def apply_pseudo_hnm_quantile(
    train_rated: pd.DataFrame,
    base_sw: pd.Series,
    feature_cols: Sequence[str],
    *,
    neg_tail_quantile: float = 0.98,
    mult: float = 2.0,
) -> Tuple[pd.Series, Dict[str, Any]]:
    """Aggressive upweight for the hardest negative tail by proxy (no second model fit)."""
    sw = base_sw.astype(float).copy()
    y = train_rated["label"].to_numpy(dtype=float)
    proxy = _numeric_proxy(train_rated, feature_cols)
    neg_mask = y == 0.0
    n_hnm = 0
    if neg_mask.any():
        thr = float(np.quantile(proxy[neg_mask], neg_tail_quantile))
        sel = neg_mask & (proxy >= thr)
        n_hnm = int(sel.sum())
        sw.loc[sel] *= mult
    return sw, {"ranking_recipe_pseudo_hnm_neg_boosted": n_hnm}


def apply_ranking_recipe_pre_optuna_weights(
    train_rated: pd.DataFrame,
    base_sw: pd.Series,
    recipe: str,
    feature_cols: Sequence[str],
) -> Tuple[pd.Series, Dict[str, Any]]:
    """Return sample weights to use for Optuna + first-pass training (top-band / pseudo-HNM)."""
    recipe_n = resolve_ranking_recipe(recipe)
    meta: Dict[str, Any] = {"ranking_recipe": recipe_n, "ranking_recipe_phase": "pre_optuna"}
    if recipe_n == RANKING_RECIPE_BASELINE:
        return base_sw.astype(float).copy(), meta
    sw = base_sw.astype(float).copy()
    if recipe_n in (RANKING_RECIPE_TOP_BAND, RANKING_RECIPE_COMBINED):
        sw, m1 = apply_top_band_reweighting(train_rated, sw, feature_cols)
        meta.update(m1)
    if recipe_n in (RANKING_RECIPE_HNM, RANKING_RECIPE_COMBINED):
        sw, m2 = apply_pseudo_hnm_quantile(train_rated, sw, feature_cols)
        meta.update(m2)
    sw = sw.clip(lower=1e-12)
    meta["ranking_recipe_weight_max"] = float(sw.max()) if len(sw) else 0.0
    meta["ranking_recipe_weight_mean"] = float(sw.mean()) if len(sw) else 0.0
    return sw, meta


def refine_weights_hnm_shallow_lgbm(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    sw: pd.Series,
    lgb_classifier_params: Mapping[str, Any],
    *,
    n_estimators_cap: int = 120,
    neg_score_quantile: float = 0.88,
    boost_mult: float = 1.75,
) -> Tuple[pd.Series, Dict[str, Any]]:
    """Second-pass light GBDT fit to upweight high-scoring negatives (minimal HNM).

    *lgb_classifier_params* must be a full ``LGBMClassifier`` kwargs dict (caller should
    merge ``_lgb_params_for_pipeline()`` with Optuna ``hp`` to avoid import cycles here).
    """
    import lightgbm as lgb

    meta: Dict[str, Any] = {"ranking_recipe_phase": "hnm_shallow_refine"}
    if X_train.empty or len(y_train) == 0 or y_train.nunique() < 2:
        meta["ranking_recipe_hnm_skipped"] = "single_class_or_empty"
        return sw.astype(float).copy(), meta
    sw2 = sw.astype(float).copy()
    params = dict(lgb_classifier_params)
    try:
        ne = int(params.get("n_estimators", 400))
    except (TypeError, ValueError):
        ne = 400
    params["n_estimators"] = max(30, min(ne, int(n_estimators_cap)))
    # Keep classifier stable on small samples
    params.setdefault("min_child_samples", 5)
    clf = lgb.LGBMClassifier(**params)
    clf.fit(X_train, y_train, sample_weight=sw2)
    proba = np.asarray(clf.predict_proba(X_train)[:, 1], dtype=float)
    yv = np.asarray(y_train, dtype=float)
    neg = yv == 0.0
    n_boost = 0
    if neg.any() and int(neg.sum()) >= 5:
        thr = float(np.quantile(proba[neg], neg_score_quantile))
        boost_idx = neg & (proba >= thr)
        n_boost = int(boost_idx.sum())
        sw2.loc[boost_idx] *= boost_mult
    sw2 = sw2.clip(lower=1e-12)
    meta["ranking_recipe_hnm_shallow_neg_boosted"] = n_boost
    meta["ranking_recipe_weight_max"] = float(sw2.max())
    return sw2, meta


def read_libsvm_weight_file(
    weight_path: Path,
    *,
    expected_rows: int,
    default_weight: float = 1.0,
) -> pd.Series:
    """Load ``.weight`` file as float series with strict row-count guard."""
    if expected_rows < 0:
        raise ValueError("expected_rows must be non-negative")
    if not weight_path.exists():
        return pd.Series(default_weight, index=np.arange(expected_rows, dtype=np.int64), dtype=float)
    vals: list[float] = []
    with open(weight_path, encoding="utf-8") as wf:
        for raw in wf:
            s = raw.strip()
            if not s:
                vals.append(default_weight)
                continue
            try:
                vals.append(float(s))
            except ValueError:
                vals.append(default_weight)
    if len(vals) != expected_rows:
        logger.warning(
            "LibSVM weight row mismatch: %s has %d rows, expected %d; falling back to uniform.",
            weight_path,
            len(vals),
            expected_rows,
        )
        return pd.Series(default_weight, index=np.arange(expected_rows, dtype=np.int64), dtype=float)
    out = pd.Series(vals, index=np.arange(expected_rows, dtype=np.int64), dtype=float)
    return out.clip(lower=1e-12)


def write_libsvm_weight_file(weight_path: Path, weights: pd.Series) -> None:
    """Atomically write training weights to ``.libsvm.weight``."""
    weight_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = weight_path.with_suffix(weight_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as wf:
        for v in weights.astype(float).to_numpy(copy=False):
            wf.write(f"{float(v)}\n")
    os.replace(tmp_path, weight_path)


def invalidate_lgb_binary_cache_for_libsvm(train_libsvm_path: Path) -> Optional[Path]:
    """Delete stale LightGBM ``.bin`` next to LibSVM train file."""
    bin_path = train_libsvm_path.parent / (train_libsvm_path.stem + ".bin")
    if not bin_path.is_file():
        return None
    try:
        bin_path.unlink()
        return bin_path
    except OSError as exc:
        logger.warning("Failed to invalidate stale LGB binary cache %s: %s", bin_path, exc)
        return None


def build_final_ranking_weights_in_memory(
    train_rated: pd.DataFrame,
    base_sw: pd.Series,
    recipe: str,
    feature_cols: Sequence[str],
    *,
    lgb_classifier_params: Optional[Mapping[str, Any]] = None,
) -> Tuple[pd.Series, Dict[str, Any]]:
    """Build canonical final training weights (stage-1 + optional stage-2 HNM)."""
    sw_pre, meta_pre = apply_ranking_recipe_pre_optuna_weights(
        train_rated,
        base_sw,
        recipe,
        feature_cols,
    )
    meta: Dict[str, Any] = dict(meta_pre)
    meta["ranking_weight_source"] = "in_memory"
    recipe_n = str(meta_pre.get("ranking_recipe", resolve_ranking_recipe(recipe)))
    can_refine = (
        recipe_n in (RANKING_RECIPE_HNM, RANKING_RECIPE_COMBINED)
        and lgb_classifier_params is not None
        and not train_rated.empty
        and train_rated.get("label") is not None
    )
    if can_refine:
        X = train_rated[[c for c in feature_cols if c in train_rated.columns]]
        y = train_rated["label"]
        sw_post, meta_hnm = refine_weights_hnm_shallow_lgbm(
            X,
            y,
            sw_pre,
            lgb_classifier_params,
        )
        meta.update(meta_hnm)
        meta["ranking_hnm_mode"] = "in_memory_shallow_lgbm"
        sw_final = sw_post
    else:
        if recipe_n in (RANKING_RECIPE_HNM, RANKING_RECIPE_COMBINED):
            meta["ranking_hnm_mode"] = "none"
            meta.setdefault("ranking_recipe_hnm_skipped", "missing_inmemory_or_params")
        else:
            meta["ranking_hnm_mode"] = "none"
        sw_final = sw_pre
    sw_final = sw_final.astype(float).clip(lower=1e-12)
    meta["ranking_weight_finalized"] = True
    meta["ranking_recipe_weight_mean"] = float(sw_final.mean()) if len(sw_final) else 0.0
    meta["ranking_recipe_weight_max"] = float(sw_final.max()) if len(sw_final) else 0.0
    return sw_final, meta


def build_final_ranking_weights_from_libsvm_proxy(
    train_libsvm_path: Path,
    base_sw: pd.Series,
    recipe: str,
) -> Tuple[pd.Series, Dict[str, Any]]:
    """OOM-safe fallback: derive stage-1 style ranking weights from LibSVM stream."""
    recipe_n = resolve_ranking_recipe(recipe)
    sw = base_sw.astype(float).copy()
    if recipe_n == RANKING_RECIPE_BASELINE:
        return sw, {
            "ranking_recipe": recipe_n,
            "ranking_weight_source": "libsvm_proxy_stream",
            "ranking_weight_finalized": True,
            "ranking_hnm_mode": "none",
            "ranking_recipe_weight_mean": float(sw.mean()) if len(sw) else 0.0,
            "ranking_recipe_weight_max": float(sw.max()) if len(sw) else 0.0,
        }
    labels: list[int] = []
    proxy_vals: list[float] = []
    with open(train_libsvm_path, encoding="utf-8") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            toks = s.split()
            try:
                labels.append(1 if float(toks[0]) > 0.0 else 0)
            except ValueError:
                labels.append(0)
            acc = 0.0
            for tok in toks[1:]:
                if ":" not in tok:
                    continue
                try:
                    acc += float(tok.split(":", 1)[1])
                except ValueError:
                    continue
            proxy_vals.append(acc)
    n = min(len(sw), len(proxy_vals))
    if n <= 0:
        return sw, {
            "ranking_recipe": recipe_n,
            "ranking_weight_source": "libsvm_proxy_stream",
            "ranking_weight_finalized": True,
            "ranking_hnm_mode": "none",
            "ranking_recipe_hnm_skipped": "empty_or_unreadable_libsvm",
        }
    y = np.asarray(labels[:n], dtype=float)
    proxy = np.asarray(proxy_vals[:n], dtype=float)
    neg_mask = y == 0.0
    pos_mask = y == 1.0
    n_tb_neg = 0
    n_tb_pos = 0
    n_hnm = 0
    if recipe_n in (RANKING_RECIPE_TOP_BAND, RANKING_RECIPE_COMBINED):
        if neg_mask.any():
            thr_n = float(np.quantile(proxy[neg_mask], 0.90))
            sel = neg_mask & (proxy >= thr_n)
            idx = np.flatnonzero(sel)
            n_tb_neg = int(idx.size)
            sw.iloc[idx] *= 1.6
        if pos_mask.any():
            thr_p = float(np.quantile(proxy[pos_mask], 0.75))
            sel = pos_mask & (proxy >= thr_p)
            idx = np.flatnonzero(sel)
            n_tb_pos = int(idx.size)
            sw.iloc[idx] *= 1.15
    if recipe_n in (RANKING_RECIPE_HNM, RANKING_RECIPE_COMBINED):
        if neg_mask.any():
            thr_h = float(np.quantile(proxy[neg_mask], 0.98))
            sel = neg_mask & (proxy >= thr_h)
            idx = np.flatnonzero(sel)
            n_hnm = int(idx.size)
            sw.iloc[idx] *= 2.0
    sw = sw.clip(lower=1e-12)
    return sw, {
        "ranking_recipe": recipe_n,
        "ranking_weight_source": "libsvm_proxy_stream",
        "ranking_weight_finalized": True,
        "ranking_hnm_mode": (
            "sampled_shallow_lgbm_stream_rewrite"
            if recipe_n in (RANKING_RECIPE_HNM, RANKING_RECIPE_COMBINED)
            else "none"
        ),
        "ranking_recipe_top_band_neg_boosted": n_tb_neg,
        "ranking_recipe_top_band_pos_boosted": n_tb_pos,
        "ranking_recipe_pseudo_hnm_neg_boosted": n_hnm,
        "ranking_recipe_hnm_skipped": "shallow_lgbm_streaming_not_available_using_proxy_tail",
        "ranking_recipe_weight_mean": float(sw.mean()) if len(sw) else 0.0,
        "ranking_recipe_weight_max": float(sw.max()) if len(sw) else 0.0,
    }
