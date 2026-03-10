"""trainer/backtester.py — Phase 1 Update
==========================================
Patron Walkaway — Single Rated Model Backtester (F-beta threshold, DEC-010 / v10)

Pipeline
--------
1. Load bets + sessions from ClickHouse (or --use-local-parquet).
2. Resolve canonical_id via identity.py (cutoff = window end).
3. Compute labels via labels.py (C1 extended pull).
4. Compute Track-B features via features.py.
5. Route observations: rated only (H3).
6. Score with single rated model.
7. Optuna TPE 1D threshold search (rated_threshold).
8. Report observation-level (micro) metrics aligned with trainer keys.

Evaluation (observation-level, trainer-aligned)
------------------------------------------------
* Micro metrics: flat dict with trainer-style keys (test_ap, test_precision,
  test_recall, test_f1, test_fbeta_05, threshold, test_samples, test_positives,
  test_random_ap, alerts, alerts_per_hour). F-beta reference uses DEC-010 beta.
* Empty or invalid subset returns same keys with zeros to avoid downstream KeyError.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import average_precision_score, fbeta_score, precision_recall_curve
from zoneinfo import ZoneInfo

optuna.logging.set_verbosity(optuna.logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtester")

# ---------------------------------------------------------------------------
# Imports from sibling modules (try/except for package vs. script execution)
# ---------------------------------------------------------------------------
try:
    import config as _cfg  # type: ignore[import]

    # G1_PRECISION_MIN / G1_ALERT_VOLUME_MIN_PER_HOUR intentionally not imported
    # — deprecated per DEC-009/010. Backtester threshold objective is F-beta.
    _G1_FBETA: float = getattr(_cfg, "G1_FBETA", 0.5)  # kept for fbeta reference metric only
    THRESHOLD_FBETA: float = getattr(_cfg, "THRESHOLD_FBETA", 0.5)
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    OPTUNA_TIMEOUT_SECONDS: Optional[int] = getattr(_cfg, "OPTUNA_TIMEOUT_SECONDS", 10 * 60)
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    HK_TZ_STR: str = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    BACKTEST_HOURS: int = getattr(_cfg, "BACKTEST_HOURS", 6)
    BACKTEST_OFFSET_HOURS: int = getattr(_cfg, "BACKTEST_OFFSET_HOURS", 1)
    THRESHOLD_MIN_RECALL: Optional[float] = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)
    THRESHOLD_MIN_ALERTS_PER_HOUR: Optional[float] = getattr(
        _cfg, "THRESHOLD_MIN_ALERTS_PER_HOUR", None
    )
    UNRATED_VOLUME_LOG: bool = bool(getattr(_cfg, "UNRATED_VOLUME_LOG", True))
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

    _G1_FBETA = getattr(_cfg, "G1_FBETA", 0.5)
    THRESHOLD_FBETA = getattr(_cfg, "THRESHOLD_FBETA", 0.5)
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    OPTUNA_TIMEOUT_SECONDS: Optional[int] = getattr(_cfg, "OPTUNA_TIMEOUT_SECONDS", 10 * 60)  # type: ignore[no-redef]
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    HK_TZ_STR = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    BACKTEST_HOURS = getattr(_cfg, "BACKTEST_HOURS", 6)
    BACKTEST_OFFSET_HOURS = getattr(_cfg, "BACKTEST_OFFSET_HOURS", 1)
    THRESHOLD_MIN_RECALL = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)
    THRESHOLD_MIN_ALERTS_PER_HOUR = getattr(_cfg, "THRESHOLD_MIN_ALERTS_PER_HOUR", None)
    UNRATED_VOLUME_LOG = bool(getattr(_cfg, "UNRATED_VOLUME_LOG", True))  # type: ignore[no-redef]

try:
    from labels import compute_labels  # type: ignore[import]
    from identity import build_canonical_mapping_from_df  # type: ignore[import]
    from schema_io import normalize_bets_sessions  # type: ignore[import]
    from features import coerce_feature_dtypes  # type: ignore[import]
    from trainer import (  # type: ignore[import, attr-defined]
        MODEL_DIR,
        load_clickhouse_data,
        load_local_parquet,
        apply_dq,
        add_track_b_features,
        compute_track_llm_features,
        load_feature_spec,
        load_player_profile,
        join_player_profile,
        _to_hk,
        HISTORY_BUFFER_DAYS,
    )
except ModuleNotFoundError:
    from trainer.labels import compute_labels  # type: ignore[import]
    from trainer.identity import build_canonical_mapping_from_df  # type: ignore[import]
    from trainer.schema_io import normalize_bets_sessions  # type: ignore[import]
    from trainer.features import coerce_feature_dtypes  # type: ignore[import]
    from trainer.trainer import (  # type: ignore[import]
        MODEL_DIR,
        load_clickhouse_data,
        load_local_parquet,
        apply_dq,
        add_track_b_features,
        compute_track_llm_features,
        load_feature_spec,
        load_player_profile,
        join_player_profile,
        _to_hk,
        HISTORY_BUFFER_DAYS,
    )

HK_TZ = ZoneInfo(HK_TZ_STR)

BASE_DIR = Path(__file__).parent
BACKTEST_OUT = BASE_DIR / "out_backtest"
BACKTEST_OUT.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Artifact loading
# ---------------------------------------------------------------------------

def load_dual_artifacts() -> Dict[str, Any]:
    """Load model bundle for backtesting (v10 single rated model, DEC-021).

    Priority:
    1. ``model.pkl``         — v10 single rated model
    2. ``rated_model.pkl``   — legacy rated slot
    3. ``walkaway_model.pkl``— legacy single-model fallback

    Also loads ``feature_list.json`` (if present) into the returned dict under
    the key ``"feature_list_meta"`` so that backtest() can distinguish profile
    features from non-profile features for NaN-fill logic (R127-1).
    """
    def _try(path: Path) -> Optional[dict]:
        if path.exists():
            return joblib.load(path)
        return None

    single = _try(MODEL_DIR / "model.pkl")
    if single is not None:
        artifacts: Dict[str, Any] = {"rated": single}
    else:
        rated = _try(MODEL_DIR / "rated_model.pkl")
        legacy = _try(MODEL_DIR / "walkaway_model.pkl")

        if rated is None and legacy is not None:
            logger.warning("rated_model.pkl not found; using walkaway_model.pkl as fallback")
            rated = legacy

        if rated is None:
            raise FileNotFoundError(
                f"No model artifacts found in {MODEL_DIR}. "
                "Run trainer.py first to produce model.pkl / rated_model.pkl."
            )
        artifacts = {"rated": rated}

    _fl_path = MODEL_DIR / "feature_list.json"
    if _fl_path.exists():
        try:
            artifacts["feature_list_meta"] = json.loads(_fl_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load feature_list.json: %s", exc)
            artifacts["feature_list_meta"] = []
    else:
        artifacts["feature_list_meta"] = []

    return artifacts


# ---------------------------------------------------------------------------
# Metric helpers (trainer-aligned: precision-at-recall PLAN § Backtester precision-at-recall)
# ---------------------------------------------------------------------------

_TARGET_RECALLS = (0.001, 0.01, 0.1, 0.5)  # DEC-026


def _zeroed_flat_metrics(threshold: float, window_hours: Optional[float]) -> dict:
    """Return trainer-style flat metrics dict with zeros (empty/invalid subset).

    Includes test_precision_at_recall_{r}, threshold_at_recall_{r},
    alerts_per_minute_at_recall_{r} for r in (0.001, 0.01, 0.1, 0.5) (DEC-026).
    """
    alerts_per_hour: Optional[float] = None
    if window_hours is not None and window_hours > 0:
        alerts_per_hour = 0.0 / window_hours
    out = {
        "test_ap": 0.0,
        "test_precision": 0.0,
        "test_recall": 0.0,
        "test_f1": 0.0,
        "test_fbeta_05": 0.0,
        "threshold": threshold,
        "test_samples": 0,
        "test_positives": 0,
        "test_random_ap": 0.0,
        "alerts": 0,
        "alerts_per_hour": alerts_per_hour,
    }
    for r in _TARGET_RECALLS:
        out[f"test_precision_at_recall_{r}"] = None
        out[f"threshold_at_recall_{r}"] = None
        out[f"alerts_per_minute_at_recall_{r}"] = None
    return out


def _score_df(df: pd.DataFrame, artifacts: Dict[str, Any]) -> pd.DataFrame:
    """Add ``score`` column to *df* using the single rated model (v10 DEC-021).

    PLAN § Train–Serve Parity: uses full artifact feature list (order and set)
    for predict_proba; caller must ensure all columns exist (non-profile 0, profile NaN).
    """
    df = df.copy()
    df["score"] = 0.0

    bundle = artifacts.get("rated")
    if bundle is not None and not df.empty:
        model_features = list(bundle.get("features") or [])
        if model_features:
            # Full feature list for train-serve parity (PLAN § Train–Serve Parity).
            df["score"] = bundle["model"].predict_proba(df[model_features])[:, 1]

    return df


def compute_micro_metrics(
    df: pd.DataFrame,
    threshold: float,
    window_hours: Optional[float] = None,
) -> dict:
    """Observation-level metrics aligned with trainer test_* key names (v10 single model).

    Returns a flat dict with trainer-style keys: test_ap, test_precision, test_recall,
    test_f1, test_fbeta_05, threshold, test_samples, test_positives, test_random_ap,
    alerts, alerts_per_hour; and test_precision_at_recall_0.01/0.1/0.5 (PLAN § Backtester
    precision-at-recall; None when empty/invalid/single-class).
    Empty or invalid (e.g. NaN labels, single-class) subset returns same keys with zeros.

    Parameters
    ----------
    df:
        Must contain ``score``, ``label``, ``is_rated`` columns.
    threshold:
        Alert threshold (v10 single rated model; only rated observations receive alerts).
    window_hours:
        Duration of the evaluation window (used to compute alerts/hour).
    """
    if df.empty:
        return _zeroed_flat_metrics(threshold, window_hours)
    missing = [c for c in ("score", "label", "is_rated") if c not in df.columns]
    if missing:
        raise ValueError(
            f"compute_micro_metrics requires columns: score, label, is_rated; missing: {missing}"
        )
    if "label" in df.columns and df["label"].isna().any():
        logger.warning(
            "compute_micro_metrics: label contains NaN — returning zeroed flat metrics (trainer-aligned)."
        )
        return _zeroed_flat_metrics(threshold, window_hours)
    if df["score"].isna().any():
        logger.warning(
            "compute_micro_metrics: score contains NaN — returning zeroed flat metrics (precision_at_recall keys None)."
        )
        return _zeroed_flat_metrics(threshold, window_hours)
    df = df.copy()
    # v10: single model — only rated observations get alerts (DEC-021).
    df["is_alert"] = np.where(df["is_rated"], df["score"] >= threshold, False)

    n_alerts = int(df["is_alert"].sum())
    n_tp = int((df["is_alert"] & (df["label"] == 1)).sum())
    n_pos = int((df["label"] == 1).sum())
    n_samples = len(df)

    prec = n_tp / n_alerts if n_alerts > 0 else 0.0
    rec = n_tp / n_pos if n_pos > 0 else 0.0
    f1 = 2.0 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    test_random_ap = (n_pos / n_samples) if n_samples > 0 else 0.0

    # Single-class (all positive or all negative): test_ap = 0.0 to align with trainer R1100.
    if n_pos == 0 or n_pos == n_samples:
        ap = 0.0
    else:
        ap = float(average_precision_score(df["label"], df["score"]))
    # F-beta reference metric (beta=0.5, precision-weighted); not used for threshold selection
    fb = float(
        fbeta_score(df["label"], df["is_alert"], beta=_G1_FBETA, zero_division=0)
    )

    alerts_per_hour: Optional[float] = None
    if window_hours is not None and window_hours > 0:
        alerts_per_hour = n_alerts / window_hours

    # Precision at fixed recall levels + threshold and alerts_per_minute (DEC-026)
    precision_at_recall: Dict[str, Optional[float]] = {}
    if n_pos == 0 or n_pos == n_samples:
        for r in _TARGET_RECALLS:
            precision_at_recall[f"test_precision_at_recall_{r}"] = None
            precision_at_recall[f"threshold_at_recall_{r}"] = None
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None
    else:
        pr_prec, pr_rec, pr_thresholds = precision_recall_curve(df["label"], df["score"])
        pr_p = pr_prec[:-1]
        pr_r = pr_rec[:-1]
        window_minutes = (window_hours * 60.0) if (window_hours is not None and window_hours > 0) else None
        for r in _TARGET_RECALLS:
            mask = pr_r >= r
            if mask.any():
                valid_idx = np.where(mask)[0]
                best_local = int(np.argmax(pr_p[valid_idx]))
                best_idx = int(valid_idx[best_local])
                thr_r = float(pr_thresholds[best_idx])
                n_at_r = int((df["score"].values >= thr_r).sum())
                apm_r = (n_at_r / window_minutes) if window_minutes else None
                precision_at_recall[f"test_precision_at_recall_{r}"] = float(pr_p[best_idx])
                precision_at_recall[f"threshold_at_recall_{r}"] = thr_r
                precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = apm_r
            else:
                precision_at_recall[f"test_precision_at_recall_{r}"] = None
                precision_at_recall[f"threshold_at_recall_{r}"] = None
                precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None

    return {
        "test_ap": ap,
        "test_precision": prec,
        "test_recall": rec,
        "test_f1": f1,
        "test_fbeta_05": fb,
        "threshold": threshold,
        "test_samples": n_samples,
        "test_positives": n_pos,
        "test_random_ap": test_random_ap,
        "alerts": n_alerts,
        "alerts_per_hour": alerts_per_hour,
        **precision_at_recall,
    }


def compute_macro_by_gaming_day_metrics(
    df: pd.DataFrame,
    threshold: float,
) -> dict:
    """Macro-by-gaming-day (per-gaming-day-average) metrics.

    Grouping key = (canonical_id, gaming_day).  Per-gaming-day at-most-1-TP
    dedup is applied (G4/SSOT §10.3): a gaming day is a True Positive if
    >= 1 observation in that day is both alerted AND labelled 1; but each
    gaming day contributes at most 1 TP to the count.

    Note: run-level Macro metrics (per-run dedup using run_id) are deferred
    to Phase 2 (DEC-012).  This function uses gaming_day as a pragmatic
    Phase 1 approximation.
    """
    if df.empty:
        return {}
    if "gaming_day" not in df.columns or "canonical_id" not in df.columns:
        logger.warning("Missing canonical_id or gaming_day; macro metrics unavailable")
        return {}

    df = df.copy()
    df["is_alert"] = np.where(df["is_rated"], df["score"] >= threshold, False)

    group_key = ["canonical_id", "gaming_day"]
    grouped = df.groupby(group_key)

    day_prec_list: list = []
    day_rec_list: list = []
    for _, grp in grouped:
        has_pos = int((grp["label"] == 1).sum()) > 0
        n_alerted = int(grp["is_alert"].sum())
        has_tp = int((grp["is_alert"] & (grp["label"] == 1)).any())

        if n_alerted > 0:
            day_prec_list.append(has_tp / n_alerted)
        if has_pos:
            day_rec_list.append(float(has_tp))

    macro_prec = float(np.mean(day_prec_list)) if day_prec_list else 0.0
    macro_rec = float(np.mean(day_rec_list)) if day_rec_list else 0.0

    return {
        "macro_precision": macro_prec,
        "macro_recall": macro_rec,
        "n_gaming_days": grouped.ngroups,
        "n_gaming_days_with_alert": len(day_prec_list),
        "n_gaming_days_with_positive": len(day_rec_list),
    }


# ---------------------------------------------------------------------------
# Combined + per-track metrics helper (reduces duplicate metric calls — R1204)
# ---------------------------------------------------------------------------

def _compute_section_metrics(
    labeled: pd.DataFrame,
    rated_sub: pd.DataFrame,
    threshold: float,
    window_hours: Optional[float],
) -> dict:
    """Compute rated observation-level metrics (v10 single threshold, DEC-021).

    Metrics are computed on rated observations only (``rated_sub``) so that
    PRAUC and alert metrics are not skewed by unrated population scores.
    The ``labeled`` parameter is accepted for API compatibility but only
    ``rated_sub`` is used for metric computation.

    Returns a flat dict (trainer-style keys): test_ap, test_precision, ...,
    threshold, rated_threshold, alerts, alerts_per_hour (PLAN step 3: no ``micro``
    nest; backtest_metrics.json model_default/optuna are flat).
    """
    rated_micro = compute_micro_metrics(rated_sub, threshold, window_hours)
    return {
        **rated_micro,
        "rated_threshold": threshold,
    }


# ---------------------------------------------------------------------------
# Optuna TPE threshold search (DEC-010 / DEC-026: precision objective, optional constraints)
# ---------------------------------------------------------------------------

def run_optuna_threshold_search(
    df: pd.DataFrame,
    artifacts: Dict[str, Any],
    n_trials: int = OPTUNA_N_TRIALS,
    window_hours: Optional[float] = None,
) -> Tuple[float, float]:
    """Optuna TPE search over rated_threshold only (v10 single Rated model, DEC-009/010/026).

    Objective: maximise Precision (DEC-026) on rated observations, subject to
    recall >= THRESHOLD_MIN_RECALL and optional min alerts/hour constraints.
    Returns (rated_t, rated_t) for API compatibility with dual-metric callers.
    """
    # Log Optuna time-budget status before optimization begins.
    if OPTUNA_TIMEOUT_SECONDS is None or OPTUNA_TIMEOUT_SECONDS <= 0:
        logger.info(
            "Optuna single-threshold search: n_trials=%d, timeout=disabled (OPTUNA_TIMEOUT_SECONDS=%s)",
            n_trials,
            OPTUNA_TIMEOUT_SECONDS,
        )
    else:
        logger.info(
            "Optuna single-threshold search: n_trials=%d, timeout=%ds (~%.1f min)",
            n_trials,
            int(OPTUNA_TIMEOUT_SECONDS),
            float(OPTUNA_TIMEOUT_SECONDS) / 60.0,
        )

    rated_sub = df[df["is_rated"]]
    if rated_sub.empty:
        default_t = float((artifacts.get("rated") or {}).get("threshold", 0.5))
        return default_t, default_t

    y = rated_sub["label"].values
    scores = rated_sub["score"].values

    def objective(trial: optuna.Trial) -> float:
        rt = trial.suggest_float("rated_threshold", 0.01, 0.99)
        preds = scores >= rt
        if THRESHOLD_MIN_RECALL is not None:
            n_pos = int((y == 1).sum())
            tp = int((preds & (y == 1)).sum())
            rec = tp / n_pos if n_pos > 0 else 0.0
            if rec < THRESHOLD_MIN_RECALL:
                return 0.0
        if (
            THRESHOLD_MIN_ALERTS_PER_HOUR is not None
            and window_hours is not None
            and window_hours > 0
        ):
            alerts_per_hour = float(preds.sum()) / float(window_hours)
            if alerts_per_hour < THRESHOLD_MIN_ALERTS_PER_HOUR:
                return 0.0
        # DEC-026: maximise Precision (at recall >= THRESHOLD_MIN_RECALL).
        tp = int((preds & (y == 1)).sum())
        fp = int((preds & (y == 0)).sum())
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        return float(prec)

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    _timeout = (
        float(OPTUNA_TIMEOUT_SECONDS)
        if OPTUNA_TIMEOUT_SECONDS is not None and OPTUNA_TIMEOUT_SECONDS > 0
        else None
    )
    study.optimize(objective, n_trials=n_trials, timeout=_timeout, show_progress_bar=False)

    if study.best_value <= 0.0:
        logger.warning(
            "No improvement found (best precision=%.4f); returning model-default threshold.",
            study.best_value,
        )
        rated_t = float((artifacts.get("rated") or {}).get("threshold", 0.5))
    else:
        rated_t = study.best_params["rated_threshold"]
        logger.info(
            "Optuna best — rated_thr=%.4f  precision=%.4f (DEC-026)",
            rated_t, study.best_value,
        )

    return rated_t, rated_t


# ---------------------------------------------------------------------------
# Main backtest function
# ---------------------------------------------------------------------------

def backtest(
    bets_raw: pd.DataFrame,
    sessions_raw: pd.DataFrame,
    artifacts: Dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    run_optuna: bool = True,
    n_optuna_trials: int = OPTUNA_N_TRIALS,
    use_local_parquet: bool = False,
) -> dict:
    """Full backtest pipeline for one time window (v10 single rated model, DEC-021).

    Returns a results dict with micro + macro metrics for both model-default
    thresholds and (optionally) Optuna-selected thresholds.

    Notes
    -----
    Caller (e.g. main) must pass already-normalized bets/sessions; parameter
    names are historical.  Unnormalized data leads to apply_dq/downstream type
    contract mismatch vs trainer/scorer (PLAN § Post-Load Normalizer).
    """
    extended_end = window_end + timedelta(minutes=max(LABEL_LOOKAHEAD_MIN, 24 * 60))
    history_start = window_start - timedelta(days=HISTORY_BUFFER_DAYS)

    # DEC-018: strip tz from all boundaries so pipeline interior is uniformly
    # tz-naive HK local time, matching apply_dq R23 output contract.
    window_start = window_start.replace(tzinfo=None) if window_start.tzinfo else window_start
    window_end   = window_end.replace(tzinfo=None)   if window_end.tzinfo   else window_end
    extended_end = extended_end.replace(tzinfo=None)  if extended_end.tzinfo  else extended_end
    # Aliases kept for label-filter clarity below.
    ws_naive = window_start
    we_naive = window_end

    # --- DQ ---
    bets, sessions = apply_dq(
        bets_raw, sessions_raw, window_start, extended_end,
        bets_history_start=history_start,
    )
    if bets.empty:
        return {"error": "No bets after DQ"}

    # --- Identity ---
    canonical_map = build_canonical_mapping_from_df(sessions, cutoff_dtm=window_end)
    if not canonical_map.empty and "player_id" in canonical_map.columns:
        bets = bets.merge(
            canonical_map[["player_id", "canonical_id"]].drop_duplicates("player_id"),
            on="player_id",
            how="left",
        )
    else:
        bets["canonical_id"] = bets["player_id"].astype(str)
    bets["canonical_id"] = bets["canonical_id"].fillna(bets["player_id"].astype(str))

    # --- Track-B features (full history for context) ---
    bets = add_track_b_features(bets, canonical_map, window_end)

    # --- Track LLM on FULL bets (PLAN § Train–Serve Parity) ---
    # Compute before label filtering so window features see same history as trainer/scorer.
    _track_llm_degraded = False
    _spec_path = MODEL_DIR / "feature_spec.yaml"
    if _spec_path.exists():
        feature_spec = load_feature_spec(_spec_path)
    else:
        feature_spec = load_feature_spec(Path(__file__).parent / "feature_spec" / "features_candidates.yaml")
    try:
        _bets_llm_result = compute_track_llm_features(
            bets,
            feature_spec=feature_spec,
            cutoff_time=window_end,
        )
        # R222 Review #4: candidates may be non-list (e.g. dict) in YAML; treat as no candidates.
        _raw_candidates = (feature_spec.get("track_llm") or {}).get("candidates")
        _candidates = _raw_candidates if isinstance(_raw_candidates, list) else []
        _llm_cand_ids = [c.get("feature_id") for c in _candidates]
        _bets_llm_feature_cols = [
            fid for fid in _llm_cand_ids
            if fid and fid in _bets_llm_result.columns
        ]
        if _bets_llm_feature_cols and "bet_id" in _bets_llm_result.columns:
            bets = bets.merge(
                _bets_llm_result[["bet_id"] + _bets_llm_feature_cols].drop_duplicates("bet_id"),
                on="bet_id",
                how="left",
            )
    except Exception as exc:
        logger.error("Track LLM failed in backtester: %s", exc)
        logger.warning(
            "Track LLM failed; artifact LLM features will be zero-filled. Backtest scores may be unreliable."
        )
        _track_llm_degraded = True

    # --- Labels ---
    labeled = compute_labels(bets_df=bets, window_end=window_end, extended_end=extended_end)
    labeled = labeled[~labeled["censored"]].copy()
    labeled = labeled[
        (labeled["payout_complete_dtm"] >= ws_naive)
        & (labeled["payout_complete_dtm"] < we_naive)
    ].copy()
    if labeled.empty:
        return {"error": "No rows after label filtering", "track_llm_degraded": _track_llm_degraded}

    # --- player_profile PIT join (PLAN § Train–Serve Parity) ---
    # R222 Review #2: pass [] when no rated players so load_player_profile does not load full table.
    _rated_cids = (
        list(canonical_map["canonical_id"].astype(str).unique())
        if not canonical_map.empty
        else []
    )
    profile_df = load_player_profile(
        window_start,
        window_end,
        use_local_parquet=use_local_parquet,
        canonical_ids=_rated_cids,
    )
    labeled = join_player_profile(labeled, profile_df)

    # Zero-fill non-profile artifact features (R127-1 / train-serve parity).
    # Profile features keep NaN when no snapshot exists — LightGBM uses its
    # trained NaN-aware default-child path, matching trainer.py / scorer.py.
    _artifact_features: list = list((artifacts.get("rated") or {}).get("features") or [])
    _artifact_meta: List[Any] = list(artifacts.get("feature_list_meta") or [])
    _profile_in_artifact: set = {
        e["name"] for e in _artifact_meta
        if isinstance(e, dict) and e.get("track") in ("track_profile", "profile")
    }
    # R131-2: when meta empty (missing/old-format JSON), fallback so profile cols keep NaN.
    if not _profile_in_artifact and _artifact_features:
        try:
            from trainer.features import PROFILE_FEATURE_COLS as _PF
        except Exception:
            _PF = []
        _profile_in_artifact = set(_PF) & set(_artifact_features)
    _non_profile_artifact = [c for c in _artifact_features if c not in _profile_in_artifact]
    for col in _non_profile_artifact:
        if col not in labeled.columns:
            labeled[col] = 0
    if _non_profile_artifact:
        labeled[_non_profile_artifact] = labeled[_non_profile_artifact].fillna(0)
    # PLAN § Train–Serve Parity: profile columns as NaN when missing (R74/R79).
    for col in _profile_in_artifact:
        if col not in labeled.columns:
            labeled[col] = np.nan

    # PLAN § Train–Serve Parity: coerce dtypes before score (train-serve parity with trainer/scorer).
    if _artifact_features:
        coerce_feature_dtypes(labeled, _artifact_features)
        labeled[_non_profile_artifact] = labeled[_non_profile_artifact].fillna(0)

    # --- H3: mark rated observations ---
    # canonical_map only contains entries for players with a valid casino_player_id,
    # so every canonical_id in the mapping is a rated player (R36 fix).
    rated_ids: set = (
        set(canonical_map["canonical_id"].unique()) if not canonical_map.empty else set()
    )
    labeled["is_rated"] = labeled["canonical_id"].isin(rated_ids)

    # --- Exclude unrated before model (PLAN: 取得 bet 後排除 unrated 再送模型) ---
    n_rated_orig = int(labeled["is_rated"].sum())
    n_unrated_orig = int((~labeled["is_rated"]).sum())
    unrated_players_orig = (
        int(
            labeled.loc[~labeled["is_rated"], "canonical_id"]
            .dropna()
            .astype(str)
            .nunique()
        )
        if n_unrated_orig > 0
        else 0
    )
    if UNRATED_VOLUME_LOG and n_unrated_orig > 0:
        logger.info(
            "[backtester] Excluded %d unrated observations (%d players); scoring %d rated.",
            n_unrated_orig,
            unrated_players_orig,
            n_rated_orig,
        )
    labeled = labeled[labeled["is_rated"]].copy()
    if labeled.empty:
        return {
            "error": "No rated observations in window",
            "rated_obs": 0,
            "unrated_obs": n_unrated_orig,
            "observations": n_unrated_orig,
            "track_llm_degraded": _track_llm_degraded,
        }

    # --- Score (rated only) ---
    labeled = _score_df(labeled, artifacts)

    # --- Window duration (for alerts/hour) ---
    window_hours = (window_end - window_start).total_seconds() / 3600.0

    # --- Rated subset (labeled is already rated-only) ---
    rated_sub = labeled

    # --- Metrics with model-default threshold (v10 single model) ---
    rated_t_default = float((artifacts.get("rated") or {}).get("threshold", 0.5))

    results: dict = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_hours": window_hours,
        "observations": n_rated_orig + n_unrated_orig,
        "rated_obs": n_rated_orig,
        "unrated_obs": n_unrated_orig,
        "track_llm_degraded": _track_llm_degraded,
        "model_default": _compute_section_metrics(
            labeled, rated_sub,
            rated_t_default, window_hours,
        ),
    }

    # --- Optional: Optuna single-threshold search ---
    if run_optuna:
        rated_t_opt, _ = run_optuna_threshold_search(
            labeled, artifacts, n_trials=n_optuna_trials, window_hours=window_hours,
        )
        results["optuna"] = _compute_section_metrics(
            labeled, rated_sub,
            rated_t_opt, window_hours,
        )

    # --- Save predictions (R30: parquet for large windows; alerts stay CSV) ---
    pred_path = BACKTEST_OUT / "backtest_predictions.parquet"
    labeled.to_parquet(pred_path, index=False)

    labeled["is_alert"] = np.where(
        labeled["is_rated"],
        labeled["score"] >= rated_t_default,
        False,
    )
    alerts_df = labeled[labeled["is_alert"]].copy()
    alerts_path = BACKTEST_OUT / "backtest_alerts.csv"
    alerts_df.to_csv(alerts_path, index=False)

    metrics_path = BACKTEST_OUT / "backtest_metrics.json"
    with metrics_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)

    results["predictions_path"] = str(pred_path)
    results["alerts_path"] = str(alerts_path)
    results["metrics_path"] = str(metrics_path)

    return results


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _default_window() -> Tuple[datetime, datetime]:
    now = datetime.now(HK_TZ)
    return (
        now - timedelta(hours=BACKTEST_HOURS + BACKTEST_OFFSET_HOURS),
        now - timedelta(hours=BACKTEST_OFFSET_HOURS),
    )


def _parse_window(args) -> Tuple[datetime, datetime]:
    if args.start or args.end:
        if not (args.start and args.end):
            raise ValueError("Provide both --start and --end or neither")
        start = _to_hk(pd.to_datetime(args.start).to_pydatetime())
        end = _to_hk(pd.to_datetime(args.end).to_pydatetime())
        return start, end
    return _default_window()


def main() -> None:
    parser = argparse.ArgumentParser(description="Patron Walkaway — Phase 1 Backtester")
    parser.add_argument("--start", default=None, help="Window start (YYYY-MM-DD HH:MM or ISO)")
    parser.add_argument("--end",   default=None, help="Window end")
    parser.add_argument(
        "--use-local-parquet", action="store_true",
        help="Load bets/sessions from data/*.parquet instead of ClickHouse",
    )
    parser.add_argument(
        "--skip-optuna", action="store_true",
        help="Skip Optuna 2D threshold search (faster)",
    )
    parser.add_argument(
        "--n-trials", type=int, default=OPTUNA_N_TRIALS,
        help=f"Optuna trials for threshold search (default: {OPTUNA_N_TRIALS})",
    )
    args = parser.parse_args()

    artifacts = load_dual_artifacts()
    start, end = _parse_window(args)

    logger.info("Backtest window: %s -> %s", start, end)

    if args.use_local_parquet:
        bets_raw, sessions_raw = load_local_parquet(start, end + timedelta(days=1))
    else:
        bets_raw, sessions_raw = load_clickhouse_data(start, end + timedelta(days=1))

    if bets_raw.empty:
        raise SystemExit("No bets for the requested window")

    # Post-Load Normalizer (PLAN § Post-Load Normalizer Phase 3)
    bets_norm, sessions_norm = normalize_bets_sessions(bets_raw, sessions_raw)

    result = backtest(
        bets_norm, sessions_norm, artifacts, start, end,
        run_optuna=not args.skip_optuna,
        n_optuna_trials=args.n_trials,
        use_local_parquet=args.use_local_parquet,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
