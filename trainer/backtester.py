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
8. Report Micro and Macro-by-gaming-day metrics.

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
from sklearn.metrics import average_precision_score, fbeta_score
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
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    HK_TZ_STR: str = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    BACKTEST_HOURS: int = getattr(_cfg, "BACKTEST_HOURS", 6)
    BACKTEST_OFFSET_HOURS: int = getattr(_cfg, "BACKTEST_OFFSET_HOURS", 1)
    THRESHOLD_MIN_RECALL: Optional[float] = getattr(_cfg, "THRESHOLD_MIN_RECALL", None)
    THRESHOLD_MIN_ALERTS_PER_HOUR: Optional[float] = getattr(
        _cfg, "THRESHOLD_MIN_ALERTS_PER_HOUR", None
    )
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

    _G1_FBETA = getattr(_cfg, "G1_FBETA", 0.5)
    THRESHOLD_FBETA = getattr(_cfg, "THRESHOLD_FBETA", 0.5)
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    HK_TZ_STR = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    BACKTEST_HOURS = getattr(_cfg, "BACKTEST_HOURS", 6)
    BACKTEST_OFFSET_HOURS = getattr(_cfg, "BACKTEST_OFFSET_HOURS", 1)
    THRESHOLD_MIN_RECALL = getattr(_cfg, "THRESHOLD_MIN_RECALL", None)
    THRESHOLD_MIN_ALERTS_PER_HOUR = getattr(_cfg, "THRESHOLD_MIN_ALERTS_PER_HOUR", None)

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
    """Load model bundle for backtesting (v10 single rated model, DEC-021).

    Priority:
    1. ``model.pkl``         — v10 single rated model
    2. ``rated_model.pkl``   — legacy rated slot
    3. ``walkaway_model.pkl``— legacy single-model fallback
    """
    def _try(path: Path) -> Optional[dict]:
        if path.exists():
            return joblib.load(path)
        return None

    single = _try(MODEL_DIR / "model.pkl")
    if single is not None:
        return {"rated": single}

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
    return {"rated": rated}


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _score_df(df: pd.DataFrame, artifacts: Dict[str, Optional[dict]]) -> pd.DataFrame:
    """Add ``score`` column to *df* using the single rated model (v10 DEC-021)."""
    df = df.copy()
    df["score"] = 0.0

    bundle = artifacts.get("rated")
    if bundle is not None and not df.empty:
        avail = [c for c in bundle["features"] if c in df.columns]
        df["score"] = bundle["model"].predict_proba(df[avail])[:, 1]

    return df


def compute_micro_metrics(
    df: pd.DataFrame,
    threshold: float,
    window_hours: Optional[float] = None,
) -> dict:
    """Micro (observation-level) metrics (v10 single model: one threshold only).

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
        return {}
    df = df.copy()
    # v10: single model — only rated observations get alerts (DEC-021).
    df["is_alert"] = np.where(df["is_rated"], df["score"] >= threshold, False)

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
    # F-beta reference metric (beta=0.5, precision-weighted); not used for threshold selection
    fb = float(
        fbeta_score(df["label"], df["is_alert"], beta=_G1_FBETA, zero_division=0)
    )

    alerts_per_hour: Optional[float] = None
    if window_hours is not None and window_hours > 0:
        alerts_per_hour = n_alerts / window_hours

    return {
        "precision": prec,
        "recall": rec,
        "prauc": prauc,
        f"fbeta_{_G1_FBETA}": fb,
        "alerts": n_alerts,
        "true_alerts": n_tp,
        "positives": n_pos,
        "observations": len(df),
        "alerts_per_hour": alerts_per_hour,
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
    """Compute rated micro/macro metrics (v10 single threshold, DEC-021).

    Both the top-level ``micro``/``macro_by_visit`` and the nested
    ``rated_track`` section are computed on rated observations only, so that
    PRAUC and alert metrics are not skewed by unrated population scores.
    The ``labeled`` parameter is accepted for API compatibility but only
    ``rated_sub`` is used for metric computation.
    """
    rated_micro = compute_micro_metrics(rated_sub, threshold, window_hours)
    rated_macro = compute_macro_by_gaming_day_metrics(rated_sub, threshold)
    return {
        "rated_threshold": threshold,
        "micro": rated_micro,
        "macro_by_visit": rated_macro,
        "rated_track": {
            "micro": rated_micro,
            "macro_by_visit": rated_macro,
        },
    }


# ---------------------------------------------------------------------------
# Optuna TPE threshold search (DEC-010: F-beta objective, optional constraints)
# ---------------------------------------------------------------------------

def run_optuna_threshold_search(
    df: pd.DataFrame,
    artifacts: Dict[str, Optional[dict]],
    n_trials: int = OPTUNA_N_TRIALS,
    window_hours: Optional[float] = None,
) -> Tuple[float, float]:
    """Optuna TPE search over rated_threshold only (v10 single Rated model, DEC-009/010).

    Objective: maximise F-beta (beta=THRESHOLD_FBETA, precision-weighted) on rated
    observations, subject to optional min recall and min alerts/hour constraints.
    Returns (rated_t, rated_t) for API compatibility with dual-metric callers.
    """
    logger.info("Optuna single-threshold search: %d trials", n_trials)

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
        return float(fbeta_score(y, preds, beta=THRESHOLD_FBETA, zero_division=0))

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    if study.best_value <= 0.0:
        logger.warning(
            "No improvement found (best F%.2f=%.4f); returning model-default threshold.",
            THRESHOLD_FBETA, study.best_value,
        )
        rated_t = float((artifacts.get("rated") or {}).get("threshold", 0.5))
    else:
        rated_t = study.best_params["rated_threshold"]
        logger.info(
            "Optuna best — rated_thr=%.4f  F%.2f=%.4f",
            rated_t, THRESHOLD_FBETA, study.best_value,
        )

    return rated_t, rated_t


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
    """Full backtest pipeline for one time window (v10 single rated model, DEC-021).

    Returns a results dict with micro + macro metrics for both model-default
    thresholds and (optionally) Optuna-selected thresholds.
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

    # --- H3: mark rated observations ---
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

    # --- Rated subset (for per-track metrics) ---
    rated_sub = labeled[labeled["is_rated"]]

    # --- Metrics with model-default threshold (v10 single model) ---
    rated_t_default = float((artifacts.get("rated") or {}).get("threshold", 0.5))

    results: dict = {
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_hours": window_hours,
        "observations": len(labeled),
        "rated_obs": int(labeled["is_rated"].sum()),
        "unrated_obs": int((~labeled["is_rated"]).sum()),
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

    result = backtest(
        bets_raw, sessions_raw, artifacts, start, end,
        run_optuna=not args.skip_optuna,
        n_optuna_trials=args.n_trials,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
