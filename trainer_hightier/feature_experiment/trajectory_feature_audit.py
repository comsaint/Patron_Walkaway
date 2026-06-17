"""Zero-retrain FP/TP separation audit for PIT-safe trajectory candidate features."""

from __future__ import annotations

import argparse
import importlib
import json
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import stats
from sklearn.metrics import roc_auc_score

from trainer_hightier.config import HK_TZ
from trainer_hightier.evaluation.player_alert_policy import (
    ALERT_TS_COLUMN,
    GAME_ID_COLUMN,
    LABEL_COLUMN,
    PLAYER_ID_COLUMN,
    SCORE_COLUMN,
    operational_simulated_metrics_block,
    simulate_player_cooldown_alerts,
)
from trainer_hightier.serving.feature_builder import prepare_lgbm_feature_matrix

_B5 = importlib.import_module("trainer_hightier.05_lgbm_train")
aggregate_bets_to_player_game = _B5.aggregate_bets_to_player_game
PAYOUT_TS_COLUMN: Final[str] = _B5.PAYOUT_TS_COLUMN
BET_ID_COLUMN: Final[str] = _B5.BET_ID_COLUMN
WALKAWAY_LABEL_COLUMN: Final[str] = _B5.LABEL_COLUMN

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MODEL_DIR = _REPO_ROOT / "out" / "models_high_tier_mvp" / "20260609-171657-1708061"
_DEFAULT_SPLITS_DIR = _REPO_ROOT / "trainer_hightier" / "artifacts" / "training_data" / "splits"
_DEFAULT_OUT_DIR = _REPO_ROOT / "out" / "trajectory_feature_audit"
_DEFAULT_BOOTSTRAP_ITERS: Final[int] = 200
_DEFAULT_RANDOM_SEED: Final[int] = 42

_BASE_COLS: Final[tuple[str, ...]] = (
    PLAYER_ID_COLUMN,
    GAME_ID_COLUMN,
    "session_id",
    "gaming_day_event",
    WALKAWAY_LABEL_COLUMN,
    PAYOUT_TS_COLUMN,
    BET_ID_COLUMN,
    "wager",
    "casino_win",
    "patron__adt__w180d_m1snap",
)

_FEATURE_FAMILIES: Final[dict[str, tuple[str, ...]]] = {
    "clock_context": (
        "fe__clock__hour_of_day",
        "fe__clock__day_of_week",
        "fe__clock__is_weekend",
        "fe__clock__is_late_night",
        "fe__clock__is_lunch_hour",
        "fe__clock__is_dinner_hour",
    ),
    "time_budget": (
        "fe__traj__elapsed_since_session_start_sec",
        "fe__traj__bets_in_session_so_far",
        "fe__traj__wager_in_session_so_far",
        "fe__traj__elapsed_over_own_median_session_sec_w30d",
        "fe__traj__bets_in_session_over_own_median_bets_w30d",
        "fe__traj__wager_in_session_over_own_median_wager_w30d",
    ),
    "money_traj": (
        "fe__traj__session_net_pnl_so_far",
        "fe__traj__session_net_pnl_over_adt",
        "fe__traj__drawdown_from_session_hwm",
        "fe__traj__drawdown_over_adt",
        "fe__traj__time_since_session_hwm_sec",
        "fe__traj__current_loss_streak_len",
        "fe__traj__current_win_streak_len",
        "fe__traj__last_loss_magnitude_over_session_avg_loss",
        "fe__traj__cumulative_loss_over_session_avg_loss",
    ),
    "pace_break": (
        "fe__traj__gap_since_prev_bet_sec",
        "fe__traj__gap_slope_last5",
        "fe__traj__gap_ratio_to_session_median_so_far",
        "fe__traj__gap_ratio_to_own_median_w30d",
        "fe__traj__wager_slope_last5",
        "fe__traj__wager_deescalation_last5",
        "fe__traj__pace_bets_cnt_w5m",
        "fe__traj__pace_deceleration_w5m_vs_w30m",
    ),
    "cashflow": (
        "txn__has_cash_out__w15m",
        "txn__cash_out_cnt__w1h",
        "txn__net_cash_flow__w1h",
    ),
}

ALL_CANDIDATE_FEATURES: Final[tuple[str, ...]] = tuple(
    f for feats in _FEATURE_FAMILIES.values() for f in feats
)


@dataclass(frozen=True)
class SplitAuditContext:
    """Scored split with bet features and player-game candidates."""

    name: str
    bets: pd.DataFrame
    bet_scores: np.ndarray
    candidates: pd.DataFrame
    window_hours: float | None
    threshold: float


def _load_bundle(model_dir: Path) -> dict[str, Any]:
    """Load Step 5 model bundle."""

    path = model_dir / "model.pkl"
    if not path.is_file():
        raise FileNotFoundError(f"model.pkl not found: {path}")
    return pickle.loads(path.read_bytes())


def _window_hours(frame: pd.DataFrame) -> float | None:
    """Return split span in hours."""

    ts = pd.to_datetime(frame[PAYOUT_TS_COLUMN], errors="coerce")
    if not ts.notna().any():
        return None
    span = float((ts.max() - ts.min()).total_seconds()) / 3600.0
    return span if np.isfinite(span) and span > 0 else None


def _score_split(
    bundle: dict[str, Any],
    split_path: Path,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Load parquet columns and score bets."""

    feature_columns = tuple(bundle["feature_columns"])
    schema_cols = set(pq.ParquetFile(split_path).schema_arrow.names)
    want = list(dict.fromkeys([*_BASE_COLS, *feature_columns]))
    columns = [c for c in want if c in schema_cols]
    missing_base = [c for c in _BASE_COLS if c not in schema_cols]
    if missing_base:
        raise ValueError(f"{split_path.name} missing base columns {missing_base}")
    frame = pd.read_parquet(split_path, columns=columns)
    x_mat = prepare_lgbm_feature_matrix(
        frame,
        feature_columns=feature_columns,
        categorical_columns=tuple(bundle.get("categorical_columns", ())),
        category_categories=dict(bundle.get("category_categories", {})),
    )
    scores = bundle["model"].predict_proba(x_mat)[:, 1]
    return frame, scores


def _player_loss_flag(casino_win: pd.Series) -> pd.Series:
    """Return True when the player lost the bet (casino_win > 0)."""

    return pd.to_numeric(casino_win, errors="coerce").fillna(0.0) > 0.0


def _streak_len(flags: np.ndarray) -> np.ndarray:
    """Run-length of consecutive True values ending at each index."""

    out = np.zeros(len(flags), dtype=np.int32)
    run = 0
    for i, flag in enumerate(flags.astype(bool)):
        run = run + 1 if flag else 0
        out[i] = run
    return out


def _rolling_slope(values: np.ndarray, window: int = 5) -> np.ndarray:
    """Rolling linear slope over the last ``window`` observations."""

    n = len(values)
    out = np.full(n, np.nan, dtype=np.float64)
    x = np.arange(window, dtype=np.float64)
    x_mean = x.mean()
    denom = np.sum((x - x_mean) ** 2)
    for i in range(window - 1, n):
        y = values[i - window + 1 : i + 1]
        if np.any(~np.isfinite(y)):
            continue
        y_mean = y.mean()
        numer = np.sum((x - x_mean) * (y - y_mean))
        out[i] = numer / denom if denom > 0 else 0.0
    return out


def _compute_player_session_baselines(train: pd.DataFrame) -> pd.DataFrame:
    """Build per-player median session stats from train only."""

    work = train[[PLAYER_ID_COLUMN, "session_id", PAYOUT_TS_COLUMN, "wager"]].copy()
    work[PAYOUT_TS_COLUMN] = pd.to_datetime(work[PAYOUT_TS_COLUMN], errors="coerce")
    work = work.dropna(subset=[PLAYER_ID_COLUMN, "session_id", PAYOUT_TS_COLUMN])
    sess = (
        work.groupby([PLAYER_ID_COLUMN, "session_id"], as_index=False)
        .agg(
            session_start=(PAYOUT_TS_COLUMN, "min"),
            session_end=(PAYOUT_TS_COLUMN, "max"),
            session_bets=("wager", "count"),
            session_wager=("wager", "sum"),
        )
    )
    sess["session_duration_sec"] = (
        sess["session_end"] - sess["session_start"]
    ).dt.total_seconds()
    baselines = (
        sess.groupby(PLAYER_ID_COLUMN, as_index=False)
        .agg(
            own_median_session_sec_w30d=("session_duration_sec", "median"),
            own_median_session_bets_w30d=("session_bets", "median"),
            own_median_session_wager_w30d=("session_wager", "median"),
        )
    )
    gap_work = train[[PLAYER_ID_COLUMN, PAYOUT_TS_COLUMN]].copy()
    gap_work[PAYOUT_TS_COLUMN] = pd.to_datetime(gap_work[PAYOUT_TS_COLUMN], errors="coerce")
    gap_work = gap_work.sort_values([PLAYER_ID_COLUMN, PAYOUT_TS_COLUMN], kind="mergesort")
    gap_work["gap_sec"] = gap_work.groupby(PLAYER_ID_COLUMN)[PAYOUT_TS_COLUMN].diff().dt.total_seconds()
    gap_med = (
        gap_work.dropna(subset=["gap_sec"])
        .groupby(PLAYER_ID_COLUMN, as_index=False)["gap_sec"]
        .median()
        .rename(columns={"gap_sec": "own_median_gap_sec_w30d"})
    )
    return baselines.merge(gap_med, on=PLAYER_ID_COLUMN, how="left")


def _compute_trajectory_features(
    df: pd.DataFrame,
    baselines: pd.DataFrame,
) -> pd.DataFrame:
    """Add PIT-safe trajectory candidate columns to bet-level frame."""

    work = df.copy()
    work[PAYOUT_TS_COLUMN] = pd.to_datetime(work[PAYOUT_TS_COLUMN], errors="coerce")
    work["wager"] = pd.to_numeric(work["wager"], errors="coerce").fillna(0.0)
    work["casino_win"] = pd.to_numeric(work["casino_win"], errors="coerce").fillna(0.0)
    work["patron__adt__w180d_m1snap"] = pd.to_numeric(
        work["patron__adt__w180d_m1snap"],
        errors="coerce",
    )
    if work[PAYOUT_TS_COLUMN].dt.tz is None:
        ts_hk = work[PAYOUT_TS_COLUMN].dt.tz_localize(HK_TZ, ambiguous="NaT", nonexistent="NaT")
    else:
        ts_hk = work[PAYOUT_TS_COLUMN].dt.tz_convert(HK_TZ)
    work["fe__clock__hour_of_day"] = ts_hk.dt.hour.astype(float)
    work["fe__clock__day_of_week"] = ts_hk.dt.isocalendar().day.astype(float)
    work["fe__clock__is_weekend"] = (ts_hk.dt.dayofweek >= 5).astype(float)
    work["fe__clock__is_late_night"] = (
        (work["fe__clock__hour_of_day"] >= 0) & (work["fe__clock__hour_of_day"] < 6)
    ).astype(float)
    work["fe__clock__is_lunch_hour"] = work["fe__clock__hour_of_day"].isin([11, 12, 13]).astype(float)
    work["fe__clock__is_dinner_hour"] = work["fe__clock__hour_of_day"].isin([17, 18, 19]).astype(float)

    sort_cols = [PLAYER_ID_COLUMN, "session_id", PAYOUT_TS_COLUMN, BET_ID_COLUMN]
    work = work.sort_values(sort_cols, kind="mergesort")
    grp = [PLAYER_ID_COLUMN, "session_id"]
    session_start = work.groupby(grp)[PAYOUT_TS_COLUMN].transform("min")
    work["fe__traj__elapsed_since_session_start_sec"] = (
        work[PAYOUT_TS_COLUMN] - session_start
    ).dt.total_seconds()
    work["fe__traj__bets_in_session_so_far"] = work.groupby(grp).cumcount() + 1
    work["fe__traj__wager_in_session_so_far"] = work.groupby(grp)["wager"].cumsum()

    work["_player_pnl"] = -work["casino_win"]
    work["fe__traj__session_net_pnl_so_far"] = work.groupby(grp)["_player_pnl"].cumsum()
    adt = work["patron__adt__w180d_m1snap"].replace(0, np.nan)
    work["fe__traj__session_net_pnl_over_adt"] = work["fe__traj__session_net_pnl_so_far"] / adt
    hwm = work.groupby(grp)["fe__traj__session_net_pnl_so_far"].cummax()
    work["fe__traj__drawdown_from_session_hwm"] = hwm - work["fe__traj__session_net_pnl_so_far"]
    work["fe__traj__drawdown_over_adt"] = work["fe__traj__drawdown_from_session_hwm"] / adt

    def _time_since_hwm(group: pd.DataFrame) -> pd.Series:
        last_hwm_ts = None
        last_hwm_val = None
        out: list[float] = []
        for ts, val in zip(group[PAYOUT_TS_COLUMN], group["fe__traj__session_net_pnl_so_far"]):
            if last_hwm_val is None or val >= last_hwm_val:
                last_hwm_val = val
                last_hwm_ts = ts
                out.append(0.0)
            else:
                out.append(float((ts - last_hwm_ts).total_seconds()) if last_hwm_ts is not None else 0.0)
        return pd.Series(out, index=group.index)

    work["fe__traj__time_since_session_hwm_sec"] = (
        work.groupby(grp, group_keys=False).apply(_time_since_hwm)
    )

    loss_flag = _player_loss_flag(work["casino_win"]).to_numpy()
    work["fe__traj__current_loss_streak_len"] = (
        work.groupby(grp, group_keys=False)["casino_win"]
        .transform(lambda s: pd.Series(_streak_len(_player_loss_flag(s).to_numpy()), index=s.index))
    )
    win_flag = (~_player_loss_flag(work["casino_win"])).to_numpy()
    work["fe__traj__current_win_streak_len"] = (
        work.groupby(grp, group_keys=False)["casino_win"]
        .transform(lambda s: pd.Series(_streak_len((~_player_loss_flag(s)).to_numpy()), index=s.index))
    )

    loss_mag = work["casino_win"].where(_player_loss_flag(work["casino_win"]), 0.0)
    session_avg_loss = (
        work.assign(_loss_mag=loss_mag)
        .groupby(grp)["_loss_mag"]
        .transform(lambda s: s.replace(0, np.nan).expanding().mean())
    )
    work["fe__traj__last_loss_magnitude_over_session_avg_loss"] = np.where(
        session_avg_loss > 0,
        loss_mag / session_avg_loss,
        np.nan,
    )
    cum_loss = work.groupby(grp)["casino_win"].transform(
        lambda s: s.where(_player_loss_flag(s), 0.0).cumsum(),
    )
    work["fe__traj__cumulative_loss_over_session_avg_loss"] = np.where(
        session_avg_loss > 0,
        cum_loss / session_avg_loss,
        np.nan,
    )

    work["fe__traj__gap_since_prev_bet_sec"] = (
        work.groupby(PLAYER_ID_COLUMN)[PAYOUT_TS_COLUMN].diff().dt.total_seconds()
    )
    work["fe__traj__gap_slope_last5"] = (
        work.groupby(PLAYER_ID_COLUMN)["fe__traj__gap_since_prev_bet_sec"]
        .transform(lambda s: pd.Series(_rolling_slope(s.to_numpy()), index=s.index))
    )
    session_gap_med = work.groupby(grp)["fe__traj__gap_since_prev_bet_sec"].transform(
        lambda s: s.expanding().median(),
    )
    work["fe__traj__gap_ratio_to_session_median_so_far"] = (
        work["fe__traj__gap_since_prev_bet_sec"] / session_gap_med.replace(0, np.nan)
    )
    work["fe__traj__wager_slope_last5"] = (
        work.groupby(PLAYER_ID_COLUMN)["wager"]
        .transform(lambda s: pd.Series(_rolling_slope(s.to_numpy()), index=s.index))
    )
    work["fe__traj__wager_deescalation_last5"] = -work["fe__traj__wager_slope_last5"]

    work["_ts"] = work[PAYOUT_TS_COLUMN]
    work = work.sort_values([PLAYER_ID_COLUMN, "_ts"], kind="mergesort")
    work["fe__traj__pace_bets_cnt_w5m"] = (
        work.groupby(PLAYER_ID_COLUMN, group_keys=False)
        .apply(_bets_in_last_minutes, minutes=5)
    )
    work["fe__traj__pace_deceleration_w5m_vs_w30m"] = np.nan

    work = work.merge(baselines, on=PLAYER_ID_COLUMN, how="left")
    work["fe__traj__elapsed_over_own_median_session_sec_w30d"] = (
        work["fe__traj__elapsed_since_session_start_sec"]
        / work["own_median_session_sec_w30d"].replace(0, np.nan)
    )
    work["fe__traj__bets_in_session_over_own_median_bets_w30d"] = (
        work["fe__traj__bets_in_session_so_far"]
        / work["own_median_session_bets_w30d"].replace(0, np.nan)
    )
    work["fe__traj__wager_in_session_over_own_median_wager_w30d"] = (
        work["fe__traj__wager_in_session_so_far"]
        / work["own_median_session_wager_w30d"].replace(0, np.nan)
    )
    work["fe__traj__gap_ratio_to_own_median_w30d"] = (
        work["fe__traj__gap_since_prev_bet_sec"]
        / work["own_median_gap_sec_w30d"].replace(0, np.nan)
    )

    for col in _FEATURE_FAMILIES["cashflow"]:
        work[col] = np.nan
    return work


def _bets_in_last_minutes(group: pd.DataFrame, *, minutes: int) -> pd.Series:
    """Count bets in trailing window per row (PIT-safe)."""

    ts = group["_ts"].to_numpy(dtype="datetime64[ns]")
    out = np.zeros(len(group), dtype=np.float64)
    delta = np.timedelta64(minutes, "m")
    start_idx = 0
    for i in range(len(group)):
        while start_idx < i and ts[start_idx] < ts[i] - delta:
            start_idx += 1
        out[i] = float(i - start_idx + 1)
    return pd.Series(out, index=group.index)


def _build_split_context(
    bundle: dict[str, Any],
    split_name: str,
    split_path: Path,
    *,
    baselines: pd.DataFrame,
    threshold: float,
) -> SplitAuditContext:
    """Score, aggregate, and attach trajectory features for one split."""

    bets_raw, bet_scores = _score_split(bundle, split_path)
    work = bets_raw.copy()
    work["_bet_score"] = np.asarray(bet_scores, dtype=np.float64)
    bets = _compute_trajectory_features(work, baselines)
    aligned_scores = bets["_bet_score"].to_numpy(dtype=np.float64)
    pg = aggregate_bets_to_player_game(bets, aligned_scores, split_name=split_name)
    return SplitAuditContext(
        name=split_name,
        bets=bets,
        bet_scores=bet_scores,
        candidates=pg.candidates,
        window_hours=_window_hours(bets),
        threshold=threshold,
    )


def _baseline_metrics(ctx: SplitAuditContext) -> dict[str, Any]:
    """Reproduce operational metrics at bundle threshold."""

    return operational_simulated_metrics_block(
        ctx.name,
        ctx.candidates,
        ctx.threshold,
        window_hours=ctx.window_hours,
    )


def _build_alert_cohorts(ctx: SplitAuditContext) -> pd.DataFrame:
    """Return raised operational alerts joined to representative bet features."""

    simulated = simulate_player_cooldown_alerts(ctx.candidates, threshold=ctx.threshold)
    raised = simulated.loc[simulated["is_raised"]].copy()
    if raised.empty:
        return raised
    feat_cols = [BET_ID_COLUMN, PLAYER_ID_COLUMN, GAME_ID_COLUMN, *ALL_CANDIDATE_FEATURES]
    bet_feats = ctx.bets[feat_cols].drop_duplicates(subset=[BET_ID_COLUMN])
    out = raised.merge(bet_feats, on=[BET_ID_COLUMN, PLAYER_ID_COLUMN, GAME_ID_COLUMN], how="left")
    out["is_tp"] = pd.to_numeric(out[LABEL_COLUMN], errors="coerce").fillna(0).astype(int) == 1
    out["cohort"] = "operational_raised"
    out["score_band"] = np.where(
        out[SCORE_COLUMN] >= ctx.threshold,
        "at_or_above_threshold",
        "below_threshold",
    )
    return out


def _build_near_threshold_cohort(ctx: SplitAuditContext, *, band: float = 0.05) -> pd.DataFrame:
    """Near-threshold player-game candidates for secondary diagnostics."""

    scores = pd.to_numeric(ctx.candidates[SCORE_COLUMN], errors="coerce")
    low = ctx.threshold - float(band)
    mask = (scores >= low) & (scores < ctx.threshold)
    near = ctx.candidates.loc[mask].copy()
    if near.empty:
        return near
    feat_cols = [BET_ID_COLUMN, PLAYER_ID_COLUMN, GAME_ID_COLUMN, *ALL_CANDIDATE_FEATURES]
    bet_feats = ctx.bets[feat_cols].drop_duplicates(subset=[BET_ID_COLUMN])
    out = near.merge(bet_feats, on=[BET_ID_COLUMN, PLAYER_ID_COLUMN, GAME_ID_COLUMN], how="left")
    out["is_tp"] = pd.to_numeric(out[LABEL_COLUMN], errors="coerce").fillna(0).astype(int) == 1
    out["cohort"] = "near_threshold"
    out["score_band"] = "within_band_below_threshold"
    return out


def _safe_auc(y_true: np.ndarray, values: np.ndarray) -> float | None:
    """Univariate AUC; None when undefined."""

    mask = np.isfinite(values)
    y = y_true[mask].astype(int)
    x = values[mask]
    if y.size == 0 or len(np.unique(y)) < 2:
        return None
    try:
        return float(roc_auc_score(y, x))
    except ValueError:
        return None


def _bootstrap_auc_ci(
    y_true: np.ndarray,
    values: np.ndarray,
    *,
    n_iters: int,
    seed: int,
) -> tuple[float | None, float | None]:
    """Bootstrap 95% CI for univariate AUC."""

    if n_iters <= 0:
        return None, None
    mask = np.isfinite(values)
    y = y_true[mask].astype(int)
    x = values[mask]
    if y.size < 10 or len(np.unique(y)) < 2:
        return None, None
    rng = np.random.default_rng(seed)
    aucs: list[float] = []
    for _ in range(int(n_iters)):
        idx = rng.integers(0, len(y), size=len(y))
        y_sample = y[idx]
        if len(np.unique(y_sample)) < 2:
            continue
        aucs.append(float(roc_auc_score(y_sample, x[idx])))
    if not aucs:
        return None, None
    return float(np.quantile(aucs, 0.025)), float(np.quantile(aucs, 0.975))


def _threshold_table(
    y_true: np.ndarray,
    scores: np.ndarray,
) -> pd.DataFrame:
    """Enumerate player-game score thresholds and precision/recall."""

    y_arr = np.asarray(y_true, dtype=np.int8).reshape(-1)
    s_arr = np.asarray(scores, dtype=np.float64).reshape(-1)
    order = np.argsort(-s_arr, kind="mergesort")
    ys = y_arr[order].astype(np.int64)
    scs = s_arr[order]
    boundaries = np.flatnonzero(np.r_[True, scs[1:] != scs[:-1]])
    ends = np.r_[boundaries[1:], len(scs)]
    tp = np.cumsum(ys)[ends - 1]
    alerts = ends.astype(np.int64)
    positives = int(np.sum(y_arr == 1))
    precision = tp / alerts
    recall = tp / float(positives) if positives > 0 else np.zeros_like(tp, dtype=np.float64)
    return pd.DataFrame(
        {
            "threshold": scs[boundaries],
            "alerts": alerts,
            "precision": precision,
            "recall": recall,
        },
    )


def _pick_at_precision(table: pd.DataFrame, min_precision: float) -> pd.Series | None:
    """Pick the highest-recall threshold under a validation precision floor."""

    feasible = table.loc[table["precision"] >= float(min_precision) - 1e-15]
    if feasible.empty:
        return None
    return feasible.sort_values(
        ["recall", "precision", "threshold"],
        ascending=[False, False, False],
    ).iloc[0]


def _resolve_audit_threshold(
    bundle: dict[str, Any],
    val_context: SplitAuditContext,
    *,
    threshold_mode: str,
    precision_floor: float,
) -> tuple[float, dict[str, Any]]:
    """Resolve audit threshold and metadata."""

    if threshold_mode == "bundle":
        return float(bundle["threshold"]), {
            "threshold_mode": "bundle",
            "precision_floor": None,
            "val_pick_feasible": True,
        }
    if threshold_mode != "val_precision_floor":
        raise ValueError(f"unknown threshold_mode={threshold_mode!r}")
    table = _threshold_table(
        val_context.candidates[LABEL_COLUMN].to_numpy(),
        val_context.candidates[SCORE_COLUMN].to_numpy(),
    )
    pick = _pick_at_precision(table, precision_floor)
    if pick is None:
        raise ValueError(f"no validation threshold satisfies precision_floor={precision_floor}")
    return float(pick["threshold"]), {
        "threshold_mode": "val_precision_floor",
        "precision_floor": float(precision_floor),
        "val_pick_feasible": True,
        "val_pick_player_game_precision": float(pick["precision"]),
        "val_pick_player_game_recall": float(pick["recall"]),
        "val_pick_player_game_alerts": int(pick["alerts"]),
    }


def _feature_separation_row(
    split: str,
    cohort: str,
    feature: str,
    family: str,
    frame: pd.DataFrame,
    *,
    bootstrap_iters: int = 0,
    random_seed: int = _DEFAULT_RANDOM_SEED,
) -> dict[str, Any]:
    """Compute TP/FP separation metrics for one feature."""

    if frame.empty or feature not in frame.columns:
        return {
            "split": split,
            "cohort": cohort,
            "feature": feature,
            "family": family,
            "tp_count": 0,
            "fp_count": 0,
            "missing_rate": 1.0,
        }
    vals = pd.to_numeric(frame[feature], errors="coerce")
    y = frame["is_tp"].to_numpy(dtype=int)
    tp_vals = vals[frame["is_tp"]].to_numpy(dtype=np.float64)
    fp_vals = vals[~frame["is_tp"]].to_numpy(dtype=np.float64)
    tp_count = int(np.sum(frame["is_tp"]))
    fp_count = int(len(frame) - tp_count)
    values_arr = vals.to_numpy(dtype=np.float64)
    auc_ci_low, auc_ci_high = _bootstrap_auc_ci(
        y,
        values_arr,
        n_iters=bootstrap_iters,
        seed=random_seed + abs(hash((split, cohort, feature))) % 1_000_000,
    )
    row: dict[str, Any] = {
        "split": split,
        "cohort": cohort,
        "feature": feature,
        "family": family,
        "tp_count": tp_count,
        "fp_count": fp_count,
        "missing_rate": float(vals.isna().mean()),
        "tp_median": float(np.nanmedian(tp_vals)) if tp_count else None,
        "fp_median": float(np.nanmedian(fp_vals)) if fp_count else None,
        "tp_p75": float(np.nanpercentile(tp_vals, 75)) if tp_count else None,
        "fp_p75": float(np.nanpercentile(fp_vals, 75)) if fp_count else None,
        "std_mean_diff": None,
        "auc": _safe_auc(y, values_arr),
        "auc_ci_low": auc_ci_low,
        "auc_ci_high": auc_ci_high,
        "ks_stat": None,
        "ks_pvalue": None,
        "direction": None,
    }
    if tp_count and fp_count:
        tp_finite = tp_vals[np.isfinite(tp_vals)]
        fp_finite = fp_vals[np.isfinite(fp_vals)]
        if tp_finite.size and fp_finite.size:
            pooled = np.concatenate([tp_finite, fp_finite])
            std = float(np.std(pooled))
            if std > 0:
                row["std_mean_diff"] = abs(float(np.mean(tp_finite) - np.mean(fp_finite))) / std
            ks = stats.ks_2samp(tp_finite, fp_finite, method="auto")
            row["ks_stat"] = float(ks.statistic)
            row["ks_pvalue"] = float(ks.pvalue)
            if row["tp_median"] is not None and row["fp_median"] is not None:
                row["direction"] = "higher_tp" if row["tp_median"] > row["fp_median"] else "higher_fp"
    return row


def _family_for(feature: str) -> str:
    """Return feature family name."""

    for family, feats in _FEATURE_FAMILIES.items():
        if feature in feats:
            return family
    return "unknown"


def _quantile_drift(val_vals: pd.Series, test_vals: pd.Series) -> float | None:
    """Max absolute quantile drift between val and test among operational alerts."""

    q_points = [0.1, 0.25, 0.5, 0.75, 0.9]
    v = pd.to_numeric(val_vals, errors="coerce").dropna()
    t = pd.to_numeric(test_vals, errors="coerce").dropna()
    if v.empty or t.empty:
        return None
    drift = [abs(float(v.quantile(q)) - float(t.quantile(q))) for q in q_points]
    return float(max(drift))


def _fp_player_day_share(alerts: pd.DataFrame) -> dict[str, Any]:
    """Top player-day FP concentration for false positives."""

    if alerts.empty:
        return {"fp_alerts": 0, "top10_fp_share": None, "top_player_days": []}
    fps = alerts.loc[~alerts["is_tp"]].copy()
    if fps.empty or "gaming_day_event" not in fps.columns:
        return {"fp_alerts": 0, "top10_fp_share": None, "top_player_days": []}
    fps["player_day"] = (
        fps[PLAYER_ID_COLUMN].astype(str) + "@" + fps["gaming_day_event"].astype(str)
    )
    counts = fps["player_day"].value_counts()
    top10 = counts.head(10)
    share = float(top10.sum()) / float(len(fps)) if len(fps) else None
    return {
        "fp_alerts": int(len(fps)),
        "top10_fp_share": share,
        "top_player_days": [
            {"player_day": str(k), "fp_count": int(v)} for k, v in top10.items()
        ],
    }


def _simulate_feature_filter(
    alerts: pd.DataFrame,
    feature: str,
    *,
    direction: str,
) -> dict[str, Any]:
    """Drop worst decile by feature and report retained precision."""

    if alerts.empty or feature not in alerts.columns:
        return {"feature": feature, "retained_alerts": 0, "retained_precision": None}
    work = alerts.copy()
    vals = pd.to_numeric(work[feature], errors="coerce")
    finite = vals[np.isfinite(vals)]
    if finite.empty:
        return {"feature": feature, "retained_alerts": 0, "retained_precision": None}
    cutoff = float(np.quantile(finite, 0.1 if direction == "higher_tp" else 0.9))
    if direction == "higher_tp":
        keep = vals >= cutoff
    else:
        keep = vals <= cutoff
    kept = work.loc[keep.fillna(False)]
    tp = int(kept["is_tp"].sum())
    total = int(len(kept))
    prec = float(tp / total) if total else None
    return {
        "feature": feature,
        "direction": direction,
        "retained_alerts": total,
        "retained_precision": prec,
        "baseline_precision": float(work["is_tp"].mean()) if len(work) else None,
    }


def _recommendations(results: pd.DataFrame) -> list[dict[str, str]]:
    """Summarize which families advance vs drop."""

    recs: list[dict[str, str]] = []
    op = results[results["cohort"] == "operational_raised"]
    for family in _FEATURE_FAMILIES:
        sub = op[op["family"] == family]
        if sub.empty:
            recs.append({"family": family, "decision": "drop", "reason": "no metrics"})
            continue
        if family == "cashflow" and sub["missing_rate"].max() >= 0.99:
            recs.append(
                {
                    "family": family,
                    "decision": "blocked",
                    "reason": "txn columns unavailable in current splits",
                },
            )
            continue
        val = sub[sub["split"] == "val"].set_index("feature")
        test = sub[sub["split"] == "test"].set_index("feature")
        shared = val.index.intersection(test.index)
        stable: list[str] = []
        unstable: list[str] = []
        for feat in shared:
            auc_val = float(val.loc[feat, "auc"]) if pd.notna(val.loc[feat, "auc"]) else 0.5
            auc_test = float(test.loc[feat, "auc"]) if pd.notna(test.loc[feat, "auc"]) else 0.5
            dir_val = val.loc[feat, "direction"]
            dir_test = test.loc[feat, "direction"]
            if auc_val > 0.55 and auc_test > 0.55 and dir_val == dir_test:
                stable.append(feat)
            elif max(auc_val, auc_test) > 0.58 and (dir_val != dir_test or min(auc_val, auc_test) < 0.52):
                unstable.append(feat)
        if stable:
            recs.append(
                {
                    "family": family,
                    "decision": "advance",
                    "reason": f"{len(stable)} stable feature(s) pass val+test separation",
                },
            )
        elif unstable:
            recs.append(
                {
                    "family": family,
                    "decision": "investigate",
                    "reason": (
                        f"{len(unstable)} feature(s) strong on one split but unstable "
                        f"(direction flip or val/test gap)"
                    ),
                },
            )
        else:
            best = sub.sort_values("auc", ascending=False).head(1)
            auc_txt = f"best_auc={best['auc'].iloc[0]:.3f}" if not best.empty else "no_auc"
            recs.append({"family": family, "decision": "deprioritize", "reason": auc_txt})
    return recs


def _write_report(
    out_dir: Path,
    *,
    baseline: dict[str, Any],
    results: pd.DataFrame,
    policy_diag: dict[str, Any],
    recommendations: list[dict[str, str]],
) -> None:
    """Write markdown summary."""

    lines = [
        "# Trajectory Feature FP/TP Audit Report",
        "",
        "## Baseline (top3_mean @ selected threshold)",
        "",
        f"- threshold mode: `{policy_diag.get('threshold_mode')}`",
        f"- threshold: `{policy_diag.get('threshold')}`",
        f"- val operational precision: **{baseline['val_operational_simulated_precision']:.1%}** "
        f"@ {baseline['val_operational_simulated_alerts_per_hour']:.3f}/hr",
        f"- test operational precision: **{baseline['test_operational_simulated_precision']:.1%}** "
        f"@ {baseline['test_operational_simulated_alerts_per_hour']:.3f}/hr",
        "",
        "## Recommendations",
        "",
    ]
    for rec in recommendations:
        lines.append(f"- **{rec['family']}**: {rec['decision']} — {rec['reason']}")
    lines.extend(["", "## Top features by test AUC (operational raised cohort)", ""])
    top = (
        results[(results["cohort"] == "operational_raised") & (results["split"] == "test")]
        .sort_values("auc", ascending=False)
        .head(10)
    )
    if top.empty:
        lines.append("No operational alerts in test split.")
    else:
        val_lookup = results[
            (results["cohort"] == "operational_raised") & (results["split"] == "val")
        ].set_index("feature")
        for _, row in top.iterrows():
            val_auc = val_lookup.loc[row["feature"], "auc"] if row["feature"] in val_lookup.index else None
            val_dir = val_lookup.loc[row["feature"], "direction"] if row["feature"] in val_lookup.index else None
            val_txt = f"val AUC={val_auc:.3f}/{val_dir}" if val_auc is not None and pd.notna(val_auc) else "val n/a"
            ci_txt = ""
            if pd.notna(row.get("auc_ci_low")) and pd.notna(row.get("auc_ci_high")):
                ci_txt = f", test AUC 95% CI=[{row['auc_ci_low']:.3f}, {row['auc_ci_high']:.3f}]"
            lines.append(
                f"- `{row['feature']}`: test AUC={row['auc']:.3f} ({row['direction']}), "
                f"{val_txt}, KS={row['ks_stat']:.3f}{ci_txt}",
            )
    advanced = [rec for rec in recommendations if rec["decision"] == "advance"]
    if advanced:
        conclusion = (
            "At least one candidate family passed the val/test same-direction AUC gate. "
            "Treat this as an audit signal for follow-up planning, not automatic registry "
            "promotion or retrain approval."
        )
    else:
        conclusion = (
            "No candidate feature achieved stable FP/TP separation on both val and test "
            "(AUC > 0.55, same direction). Do not retrain on test-only uplift."
        )
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            conclusion,
            "Cashflow requires txn enrichment before audit.",
            "",
            "## FP concentration",
            "",
        ],
    )
    for split in ("val", "test"):
        diag = policy_diag.get(split, {})
        share = diag.get("baseline_fp_concentration", {}).get("top10_fp_share")
        lines.append(f"- {split} top10 FP share: {share}")
    path = out_dir / "trajectory_feature_audit_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(
    *,
    model_dir: Path,
    splits_dir: Path,
    out_dir: Path,
    threshold_mode: str = "bundle",
    precision_floor: float = 0.50,
    bootstrap_iters: int = _DEFAULT_BOOTSTRAP_ITERS,
    random_seed: int = _DEFAULT_RANDOM_SEED,
) -> None:
    """Execute full trajectory feature audit pipeline."""

    bundle = _load_bundle(model_dir)
    initial_threshold = float(bundle["threshold"])
    train_path = splits_dir / "train.parquet"
    baselines = _compute_player_session_baselines(
        pd.read_parquet(train_path, columns=list(_BASE_COLS)),
    )

    contexts: dict[str, SplitAuditContext] = {}
    baseline_metrics: dict[str, Any] = {}
    for split in ("val", "test"):
        ctx = _build_split_context(
            bundle,
            split,
            splits_dir / f"{split}.parquet",
            baselines=baselines,
            threshold=initial_threshold,
        )
        contexts[split] = ctx

    threshold, threshold_meta = _resolve_audit_threshold(
        bundle,
        contexts["val"],
        threshold_mode=threshold_mode,
        precision_floor=precision_floor,
    )
    contexts = {
        name: SplitAuditContext(
            name=ctx.name,
            bets=ctx.bets,
            bet_scores=ctx.bet_scores,
            candidates=ctx.candidates,
            window_hours=ctx.window_hours,
            threshold=threshold,
        )
        for name, ctx in contexts.items()
    }
    for split, ctx in contexts.items():
        baseline_metrics.update(_baseline_metrics(ctx))

    rows: list[dict[str, Any]] = []
    policy_diag: dict[str, Any] = {
        "threshold": threshold,
        "score_aggregation": "top3_mean",
        "bootstrap_iters": int(bootstrap_iters),
        **threshold_meta,
    }
    alert_frames: dict[str, pd.DataFrame] = {}

    for split, ctx in contexts.items():
        alerts = _build_alert_cohorts(ctx)
        near = _build_near_threshold_cohort(ctx)
        alert_frames[split] = alerts
        if not alerts.empty and "gaming_day_event" not in alerts.columns:
            day_map = ctx.bets[[BET_ID_COLUMN, "gaming_day_event"]].drop_duplicates()
            alerts = alerts.merge(day_map, on=BET_ID_COLUMN, how="left")
            alert_frames[split] = alerts
        policy_diag[split] = {
            "baseline_metrics": {k: v for k, v in baseline_metrics.items() if k.startswith(split)},
            "baseline_fp_concentration": _fp_player_day_share(alerts),
            "near_threshold_count": int(len(near)),
        }
        for cohort_name, frame in (
            ("operational_raised", alerts),
            ("near_threshold", near),
        ):
            for feature in ALL_CANDIDATE_FEATURES:
                rows.append(
                    _feature_separation_row(
                        split,
                        cohort_name,
                        feature,
                        _family_for(feature),
                        frame,
                        bootstrap_iters=bootstrap_iters,
                        random_seed=random_seed,
                    ),
                )

    results = pd.DataFrame(rows)
    val_alerts = alert_frames.get("val", pd.DataFrame())
    test_alerts = alert_frames.get("test", pd.DataFrame())
    drift_rows: list[dict[str, Any]] = []
    for feature in ALL_CANDIDATE_FEATURES:
        if val_alerts.empty or test_alerts.empty:
            continue
        drift = _quantile_drift(val_alerts[feature], test_alerts[feature])
        drift_rows.append({"feature": feature, "quantile_drift_val_test": drift})
    if drift_rows:
        drift_df = pd.DataFrame(drift_rows)
        results = results.merge(drift_df, on="feature", how="left")

    filter_diag: list[dict[str, Any]] = []
    op_test = results[
        (results["split"] == "test")
        & (results["cohort"] == "operational_raised")
        & results["auc"].notna()
    ].sort_values("auc", ascending=False)
    for _, row in op_test.head(5).iterrows():
        direction = row.get("direction") or "higher_tp"
        filter_diag.append(
            _simulate_feature_filter(test_alerts, row["feature"], direction=str(direction)),
        )
    policy_diag["feature_filter_simulations"] = filter_diag

    recommendations = _recommendations(results)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    results_path = out_dir / f"{stamp}_trajectory_feature_audit_results.csv"
    results.to_csv(results_path, index=False)
    results.to_csv(out_dir / "trajectory_feature_audit_results.csv", index=False)
    (out_dir / "trajectory_feature_policy_diagnostics.json").write_text(
        json.dumps(policy_diag, indent=2, default=str),
        encoding="utf-8",
    )
    _write_report(
        out_dir,
        baseline={
            "val_operational_simulated_precision": baseline_metrics["val_operational_simulated_precision"],
            "val_operational_simulated_alerts_per_hour": baseline_metrics[
                "val_operational_simulated_alerts_per_hour"
            ],
            "test_operational_simulated_precision": baseline_metrics[
                "test_operational_simulated_precision"
            ],
            "test_operational_simulated_alerts_per_hour": baseline_metrics[
                "test_operational_simulated_alerts_per_hour"
            ],
        },
        results=results,
        policy_diag=policy_diag,
        recommendations=recommendations,
    )
    print(json.dumps({"baseline": baseline_metrics, "recommendations": recommendations}, indent=2))


def main() -> None:
    """CLI entrypoint."""

    parser = argparse.ArgumentParser(description="Trajectory feature FP/TP audit (zero retrain)")
    parser.add_argument("--model-dir", type=Path, default=_DEFAULT_MODEL_DIR)
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS_DIR)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    parser.add_argument(
        "--threshold-mode",
        choices=("bundle", "val_precision_floor"),
        default="bundle",
        help="Use stored bundle threshold or select a threshold on validation player-game precision.",
    )
    parser.add_argument("--precision-floor", type=float, default=0.50)
    parser.add_argument("--bootstrap-iters", type=int, default=_DEFAULT_BOOTSTRAP_ITERS)
    parser.add_argument("--random-seed", type=int, default=_DEFAULT_RANDOM_SEED)
    args = parser.parse_args()
    run_audit(
        model_dir=args.model_dir,
        splits_dir=args.splits_dir,
        out_dir=args.out_dir,
        threshold_mode=args.threshold_mode,
        precision_floor=args.precision_floor,
        bootstrap_iters=args.bootstrap_iters,
        random_seed=args.random_seed,
    )


if __name__ == "__main__":
    main()
