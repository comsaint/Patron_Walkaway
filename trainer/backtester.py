"""trainer/backtester.py — Phase 1 Update
==========================================
Patron Walkaway — Dual-Model Backtester + G1 Threshold Selector

Pipeline
--------
1. Load bets + sessions from ClickHouse (or --use-local-parquet).
2. Resolve canonical_id via identity.py (cutoff = window end).
3. Compute labels via labels.py (C1 extended pull).
4. Compute Track-B features via features.py.
5. Route observations to Rated / Non-rated (H3).
6. Score with dual models.
7. Optuna TPE 2D threshold search (rated_threshold × nonrated_threshold).
8. Report Micro and Macro-by-visit dual metrics.

Evaluation rules (SSOT §10.3)
-------------------------------
* **Per-visit at-most-1-TP dedup** (G4): for the purpose of computing
  Macro-by-visit precision/recall, each (canonical_id, gaming_day) visit
  contributes at most one True Positive, preventing high-frequency players
  from dominating the metric.  Online scoring is NOT limited to one alert
  per visit; this dedup applies ONLY to offline evaluation.
* Micro metrics (observation-level): Precision / Recall / PR-AUC / F-beta /
  alerts-per-hour.
* Macro-by-visit metrics: unweighted mean over visits of per-visit
  Precision and Recall.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import joblib
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import average_precision_score, f1_score, fbeta_score, precision_score
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

    G1_PRECISION_MIN = _cfg.G1_PRECISION_MIN
    G1_ALERT_VOLUME_MIN_PER_HOUR = _cfg.G1_ALERT_VOLUME_MIN_PER_HOUR
    G1_FBETA = _cfg.G1_FBETA
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    HK_TZ_STR: str = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    BACKTEST_HOURS: int = getattr(_cfg, "BACKTEST_HOURS", 6)
    BACKTEST_OFFSET_HOURS: int = getattr(_cfg, "BACKTEST_OFFSET_HOURS", 1)
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

    G1_PRECISION_MIN = _cfg.G1_PRECISION_MIN
    G1_ALERT_VOLUME_MIN_PER_HOUR = _cfg.G1_ALERT_VOLUME_MIN_PER_HOUR
    G1_FBETA = _cfg.G1_FBETA
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    HK_TZ_STR = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    BACKTEST_HOURS = getattr(_cfg, "BACKTEST_HOURS", 6)
    BACKTEST_OFFSET_HOURS = getattr(_cfg, "BACKTEST_OFFSET_HOURS", 1)

try:
    from labels import compute_labels  # type: ignore[import]
    from identity import build_canonical_mapping_from_df  # type: ignore[import]
    from trainer import (  # type: ignore[import, attr-defined]
        MODEL_DIR,
        load_clickhouse_data,
        load_local_parquet,
        apply_dq,
        add_track_b_features,
        add_legacy_features,
        ALL_FEATURE_COLS,
        _to_hk,
        HISTORY_BUFFER_DAYS,
    )
except ModuleNotFoundError:
    from trainer.labels import compute_labels  # type: ignore[import]
    from trainer.identity import build_canonical_mapping_from_df  # type: ignore[import]
    from trainer.trainer import (  # type: ignore[import]
        MODEL_DIR,
        load_clickhouse_data,
        load_local_parquet,
        apply_dq,
        add_track_b_features,
        add_legacy_features,
        ALL_FEATURE_COLS,
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

def load_dual_artifacts() -> Dict[str, Optional[dict]]:
    """Load rated + non-rated model bundles.

    Falls back to legacy single-model ``walkaway_model.pkl`` for both slots
    so that the backtester can still run before the dual-model trainer has
    been executed.
    """
    def _try(path: Path) -> Optional[dict]:
        if path.exists():
            return joblib.load(path)
        return None

    rated = _try(MODEL_DIR / "rated_model.pkl")
    nonrated = _try(MODEL_DIR / "nonrated_model.pkl")
    legacy = _try(MODEL_DIR / "walkaway_model.pkl")

    if rated is None and legacy is not None:
        logger.warning("rated_model.pkl not found; using walkaway_model.pkl as fallback")
        rated = legacy
    if nonrated is None and legacy is not None:
        logger.warning("nonrated_model.pkl not found; using walkaway_model.pkl as fallback")
        nonrated = legacy

    if rated is None and nonrated is None:
        raise FileNotFoundError(
            f"No model artifacts found in {MODEL_DIR}. "
            "Run trainer.py first to produce rated_model.pkl / nonrated_model.pkl."
        )
    return {"rated": rated, "nonrated": nonrated}


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _score_df(df: pd.DataFrame, artifacts: Dict[str, Optional[dict]]) -> pd.DataFrame:
    """Add ``score`` and ``is_alert`` columns to *df* using per-observation model routing."""
    df = df.copy()
    df["score"] = np.nan

    for track, bundle in artifacts.items():
        if bundle is None:
            continue
        is_track = df["is_rated"] if track == "rated" else ~df["is_rated"]
        sub = df[is_track]
        if sub.empty:
            continue
        avail = [c for c in bundle["features"] if c in sub.columns]
        df.loc[is_track, "score"] = bundle["model"].predict_proba(sub[avail])[:, 1]

    # Fill any remaining NaN scores with 0 (observations not covered by either model)
    df["score"] = df["score"].fillna(0.0)
    return df


def compute_micro_metrics(
    df: pd.DataFrame,
    rated_threshold: float,
    nonrated_threshold: float,
    window_hours: Optional[float] = None,
) -> dict:
    """Micro (observation-level) metrics for the combined rated+nonrated population.

    Parameters
    ----------
    df:
        Must contain ``score``, ``label``, ``is_rated`` columns.
    rated_threshold / nonrated_threshold:
        Alert threshold per model track.
    window_hours:
        Duration of the evaluation window (used to compute alerts/hour).
    """
    df = df.copy()
    df["is_alert"] = np.where(
        df["is_rated"],
        df["score"] >= rated_threshold,
        df["score"] >= nonrated_threshold,
    )

    n_alerts = int(df["is_alert"].sum())
    n_tp = int((df["is_alert"] & (df["label"] == 1)).sum())
    n_pos = int((df["label"] == 1).sum())

    prec = n_tp / n_alerts if n_alerts > 0 else 0.0
    rec = n_tp / n_pos if n_pos > 0 else 0.0

    prauc = (
        float(average_precision_score(df["label"], df["score"]))
        if n_pos > 0
        else 0.0
    )
    # F-beta: beta < 1 → precision-weighted
    fb = float(
        fbeta_score(df["label"], df["is_alert"], beta=G1_FBETA, zero_division=0)
    )

    alerts_per_hour: Optional[float] = None
    if window_hours is not None and window_hours > 0:
        alerts_per_hour = n_alerts / window_hours

    return {
        "precision": prec,
        "recall": rec,
        "prauc": prauc,
        f"fbeta_{G1_FBETA}": fb,
        "alerts": n_alerts,
        "true_alerts": n_tp,
        "positives": n_pos,
        "observations": len(df),
        "alerts_per_hour": alerts_per_hour,
    }


def compute_macro_by_visit_metrics(
    df: pd.DataFrame,
    rated_threshold: float,
    nonrated_threshold: float,
) -> dict:
    """Macro-by-visit (per-visit-average) metrics.

    Each visit = (canonical_id, gaming_day).  Per-visit at-most-1-TP dedup
    is applied (G4/SSOT §10.3): a visit is a True Positive if ≥ 1 observation
    in that visit is both alerted AND labelled 1; but each visit contributes
    at most 1 TP to the count.
    """
    if "gaming_day" not in df.columns or "canonical_id" not in df.columns:
        logger.warning("Missing canonical_id or gaming_day; macro metrics unavailable")
        return {}

    df = df.copy()
    df["is_alert"] = np.where(
        df["is_rated"],
        df["score"] >= rated_threshold,
        df["score"] >= nonrated_threshold,
    )

    visit_key = ["canonical_id", "gaming_day"]
    grouped = df.groupby(visit_key)

    visit_prec_list = []
    visit_rec_list = []
    for _, grp in grouped:
        has_pos = int((grp["label"] == 1).sum()) > 0
        n_alerted = int(grp["is_alert"].sum())
        # Per-visit TP: at most 1 even if multiple alerted+positive rows
        has_tp = int((grp["is_alert"] & (grp["label"] == 1)).any())

        if n_alerted > 0:
            visit_prec_list.append(has_tp / n_alerted)
        if has_pos:
            visit_rec_list.append(float(has_tp))

    macro_prec = float(np.mean(visit_prec_list)) if visit_prec_list else 0.0
    macro_rec = float(np.mean(visit_rec_list)) if visit_rec_list else 0.0

    return {
        "macro_precision": macro_prec,
        "macro_recall": macro_rec,
        "n_visits": grouped.ngroups,
        "n_visits_with_alert": len(visit_prec_list),
        "n_visits_with_positive": len(visit_rec_list),
    }


# ---------------------------------------------------------------------------
# Optuna TPE 2D threshold search (G1 / I6)
# ---------------------------------------------------------------------------

def run_optuna_threshold_search(
    df: pd.DataFrame,
    artifacts: Dict[str, Optional[dict]],
    n_trials: int = OPTUNA_N_TRIALS,
    window_hours: Optional[float] = None,
) -> Tuple[float, float]:
    """Optuna TPE search over (rated_threshold, nonrated_threshold).

    Objective: maximise combined F1 (DEC-010; aligned with trainer threshold criterion).
    Constraints (returned as -inf if violated):
      * per-model precision ≥ G1_PRECISION_MIN
      * combined alerts/hour ≥ G1_ALERT_VOLUME_MIN_PER_HOUR  (only if window_hours given)
    """
    logger.info("Optuna 2D threshold search: %d trials", n_trials)

    y = df["label"].values
    rated_scores = np.where(df["is_rated"], df["score"], np.nan)
    nonrated_scores = np.where(~df["is_rated"], df["score"], np.nan)

    rated_mask = df["is_rated"].values
    nonrated_mask = ~df["is_rated"].values
    y_rated = y[rated_mask]
    y_nonrated = y[nonrated_mask]

    def objective(trial: optuna.Trial) -> float:
        rt = trial.suggest_float("rated_threshold", 0.01, 0.99)
        nt = trial.suggest_float("nonrated_threshold", 0.01, 0.99)

        preds_rated = rated_scores[rated_mask] >= rt if rated_mask.any() else np.array([], dtype=bool)
        preds_nonrated = nonrated_scores[nonrated_mask] >= nt if nonrated_mask.any() else np.array([], dtype=bool)

        prec_rated = float(precision_score(y_rated, preds_rated, zero_division=0)) if len(y_rated) > 0 else 1.0
        prec_nonrated = float(precision_score(y_nonrated, preds_nonrated, zero_division=0)) if len(y_nonrated) > 0 else 1.0

        if prec_rated < G1_PRECISION_MIN or prec_nonrated < G1_PRECISION_MIN:
            return float("-inf")

        n_alerts = int(preds_rated.sum() if len(preds_rated) else 0) + int(preds_nonrated.sum() if len(preds_nonrated) else 0)
        if window_hours and window_hours > 0:
            if n_alerts / window_hours < G1_ALERT_VOLUME_MIN_PER_HOUR:
                return float("-inf")

        # Combined preds for F1 (DEC-010)
        all_preds = np.zeros(len(df), dtype=bool)
        if rated_mask.any():
            all_preds[rated_mask] = preds_rated
        if nonrated_mask.any():
            all_preds[nonrated_mask] = preds_nonrated

        return float(f1_score(y, all_preds, zero_division=0))

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    if study.best_value == float("-inf"):
        logger.warning(
            "No feasible threshold found (all trials violated G1 constraints). "
            "Returning model-default thresholds."
        )
        rated_t = float((artifacts.get("rated") or {}).get("threshold", 0.5))
        nonrated_t = float((artifacts.get("nonrated") or {}).get("threshold", 0.5))
    else:
        rated_t = study.best_params["rated_threshold"]
        nonrated_t = study.best_params["nonrated_threshold"]
        logger.info(
            "Optuna best — rated_thr=%.4f  nonrated_thr=%.4f  F1=%.4f",
            rated_t, nonrated_t, study.best_value,
        )

    return rated_t, nonrated_t


# ---------------------------------------------------------------------------
# Main backtest function
# ---------------------------------------------------------------------------

def backtest(
    bets_raw: pd.DataFrame,
    sessions_raw: pd.DataFrame,
    artifacts: Dict[str, Optional[dict]],
    window_start: datetime,
    window_end: datetime,
    run_optuna: bool = True,
    n_optuna_trials: int = OPTUNA_N_TRIALS,
) -> dict:
    """Full backtest pipeline for one time window.

    Returns a results dict with micro + macro metrics for both model-default
    thresholds and (optionally) Optuna-selected thresholds.
    """
    extended_end = window_end + timedelta(minutes=max(LABEL_LOOKAHEAD_MIN, 24 * 60))
    history_start = window_start - timedelta(days=HISTORY_BUFFER_DAYS)

    # R29: apply_dq normalises payout_complete_dtm to tz-naive HK local time.
    # Ensure window boundaries used for label-row filtering are also tz-naive so
    # that the comparison never raises TypeError on mixed-tz DataFrames.
    ws_naive = window_start.replace(tzinfo=None)
    we_naive = window_end.replace(tzinfo=None)

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

    # --- Labels ---
    labeled = compute_labels(bets_df=bets, window_end=window_end, extended_end=extended_end)
    labeled = labeled[~labeled["censored"]].copy()
    labeled = labeled[
        (labeled["payout_complete_dtm"] >= ws_naive)
        & (labeled["payout_complete_dtm"] < we_naive)
    ].copy()
    if labeled.empty:
        return {"error": "No rows after label filtering"}

    # --- Legacy features ---
    labeled = add_legacy_features(labeled, sessions)
    for col in ALL_FEATURE_COLS:
        if col not in labeled.columns:
            labeled[col] = 0
    labeled[ALL_FEATURE_COLS] = labeled[ALL_FEATURE_COLS].fillna(0)

    # --- Rated / Non-rated routing (H3) ---
    # canonical_map only contains entries for players with a valid casino_player_id,
    # so every canonical_id in the mapping is a rated player (R36 fix).
    rated_ids: set = (
        set(canonical_map["canonical_id"].unique()) if not canonical_map.empty else set()
    )
    labeled["is_rated"] = labeled["canonical_id"].isin(rated_ids)

    # --- Score ---
    labeled = _score_df(labeled, artifacts)

    # --- Window duration (for alerts/hour) ---
    window_hours = (window_end - window_start).total_seconds() / 3600.0

    # --- Metrics with model-default thresholds ---
    rated_t_default = float((artifacts.get("rated") or {}).get("threshold", 0.5))
    nonrated_t_default = float((artifacts.get("nonrated") or {}).get("threshold", 0.5))

    micro_default = compute_micro_metrics(labeled, rated_t_default, nonrated_t_default, window_hours)
    macro_default = compute_macro_by_visit_metrics(labeled, rated_t_default, nonrated_t_default)

    results: dict = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_hours": window_hours,
        "observations": len(labeled),
        "rated_obs": int(labeled["is_rated"].sum()),
        "nonrated_obs": int((~labeled["is_rated"]).sum()),
        "model_default": {
            "rated_threshold": rated_t_default,
            "nonrated_threshold": nonrated_t_default,
            "micro": micro_default,
            "macro_by_visit": macro_default,
        },
    }

    # --- Optional: Optuna 2D threshold search ---
    if run_optuna:
        rated_t_opt, nonrated_t_opt = run_optuna_threshold_search(
            labeled, artifacts, n_trials=n_optuna_trials, window_hours=window_hours,
        )
        micro_opt = compute_micro_metrics(labeled, rated_t_opt, nonrated_t_opt, window_hours)
        macro_opt = compute_macro_by_visit_metrics(labeled, rated_t_opt, nonrated_t_opt)
        results["optuna"] = {
            "rated_threshold": rated_t_opt,
            "nonrated_threshold": nonrated_t_opt,
            "micro": micro_opt,
            "macro_by_visit": macro_opt,
        }

    # --- Save predictions (R30: parquet for large windows; alerts stay CSV) ---
    pred_path = BACKTEST_OUT / "backtest_predictions.parquet"
    labeled.to_parquet(pred_path, index=False)

    labeled["is_alert"] = np.where(
        labeled["is_rated"],
        labeled["score"] >= rated_t_default,
        labeled["score"] >= nonrated_t_default,
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
        help="Load bets/sessions from .data/local/*.parquet instead of ClickHouse",
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

    logger.info("Backtest window: %s → %s", start, end)

    if args.use_local_parquet:
        bets_raw, sessions_raw = load_local_parquet(start, end + timedelta(days=1))
    else:
        bets_raw, sessions_raw = load_clickhouse_data(start, end + timedelta(days=1))

    if bets_raw.empty:
        raise SystemExit("No bets for the requested window")

    result = backtest(
        bets_raw, sessions_raw, artifacts, start, end,
        run_optuna=not args.skip_optuna,
        n_optuna_trials=args.n_trials,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
