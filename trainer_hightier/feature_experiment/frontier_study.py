"""Frontier study: score aggregation / smoothing / persistence policies (no retrain)."""

from __future__ import annotations

import argparse
import importlib
import json
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from trainer_hightier.evaluation.alert_band_objective import operational_threshold_picks_for_targets
from trainer_hightier.evaluation.metrics_blocks import split_metrics_block
from trainer_hightier.evaluation.player_alert_policy import operational_simulated_metrics_block
from trainer_hightier.serving.feature_builder import prepare_lgbm_feature_matrix

_B5 = importlib.import_module("trainer_hightier.05_lgbm_train")
PlayerGameAggregationResult = _B5.PlayerGameAggregationResult
aggregate_bets_to_player_game = _B5.aggregate_bets_to_player_game
PLAYER_ID_COLUMN: Final[str] = _B5.PLAYER_ID_COLUMN
GAME_ID_COLUMN: Final[str] = _B5.GAME_ID_COLUMN
LABEL_COLUMN: Final[str] = _B5.LABEL_COLUMN
PAYOUT_TS_COLUMN: Final[str] = _B5.PAYOUT_TS_COLUMN
BET_ID_COLUMN: Final[str] = _B5.BET_ID_COLUMN
ALERT_TS_COLUMN: Final[str] = _B5.ALERT_TS_COLUMN
_coerce_group_id_series = _B5._coerce_group_id_series

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MODEL_DIR = _REPO_ROOT / "out" / "models_high_tier_mvp" / "20260609-171657-1708061"
_DEFAULT_SPLITS_DIR = _REPO_ROOT / "trainer_hightier" / "artifacts" / "training_data" / "splits"
_DEFAULT_OUT_DIR = _REPO_ROOT / "out" / "frontier_study"
_META_COLUMNS: Final[tuple[str, ...]] = (
    "player_id",
    "game_id",
    "gaming_day_event",
    "walkaway_label",
    "payout_complete_dtm",
    "bet_id",
)

SCORE_POLICIES: Final[tuple[str, ...]] = (
    "max_score",
    "p95_score",
    "top3_mean_score",
    "top5_mean_score",
    "last_score",
    "mean_score",
    "rolling3_mean_then_max",
    "rolling5_mean_then_max",
    "rolling10_mean_then_max",
    "ema_alpha_0_5_then_max",
)
PERSISTENCE_POLICIES: Final[tuple[str, ...]] = (
    "n2_within_15m",
    "n3_within_15m",
    "two_of_last_five",
)
PRECISION_FLOORS: Final[tuple[float, ...]] = (0.60, 0.55, 0.50, 0.45, 0.40)
ALERT_TARGETS: Final[tuple[float, ...]] = (0.25, 0.50, 1.00, 2.00, 3.00)


@dataclass(frozen=True)
class ScoredSplit:
    """Bet-level scored frame plus player-game aggregation for one split."""

    name: str
    frame: pd.DataFrame
    bet_scores: np.ndarray
    player_game: PlayerGameAggregationResult
    window_hours: float | None


def _load_bundle(model_dir: Path) -> dict[str, Any]:
    """Load Step 5 model bundle from artifact directory."""

    model_path = Path(model_dir) / "model.pkl"
    if not model_path.is_file():
        raise FileNotFoundError(f"model.pkl not found: {model_path}")
    return pickle.loads(model_path.read_bytes())


def _window_hours(frame: pd.DataFrame) -> float | None:
    """Return split span in hours from payout timestamps."""

    ts = pd.to_datetime(frame[PAYOUT_TS_COLUMN], errors="coerce")
    if not ts.notna().any():
        return None
    span = float((ts.max() - ts.min()).total_seconds()) / 3600.0
    return span if np.isfinite(span) and span > 0 else None


def _group_score(values: np.ndarray, policy: str) -> float:
    """Aggregate bet scores within one player-game group."""

    vals = np.asarray(values, dtype=np.float64)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return float("nan")
    if policy in ("max_score",) or policy.endswith("_then_max"):
        return float(np.max(vals))
    if policy == "p95_score":
        return float(np.quantile(vals, 0.95))
    if policy == "top3_mean_score":
        top = np.sort(vals)[-min(3, len(vals)) :]
        return float(np.mean(top))
    if policy == "top5_mean_score":
        top = np.sort(vals)[-min(5, len(vals)) :]
        return float(np.mean(top))
    if policy == "last_score":
        return float(vals[-1])
    if policy == "mean_score":
        return float(np.mean(vals))
    raise ValueError(f"unknown group score policy {policy!r}")


def _smooth_bet_scores(work: pd.DataFrame, policy: str) -> pd.Series:
    """Return smoothed bet scores for rolling/EMA policies."""

    if policy == "rolling3_mean_then_max":
        window = 3
    elif policy == "rolling5_mean_then_max":
        window = 5
    elif policy == "rolling10_mean_then_max":
        window = 10
    elif policy == "ema_alpha_0_5_then_max":
        ordered = work.sort_values(
            by=[PLAYER_ID_COLUMN, PAYOUT_TS_COLUMN, BET_ID_COLUMN],
            kind="mergesort",
        )
        return ordered.groupby(PLAYER_ID_COLUMN, sort=False)["_score"].transform(
            lambda s: s.ewm(alpha=0.5, adjust=False).mean(),
        ).reindex(work.index)
    else:
        raise ValueError(f"not a smoothing policy {policy!r}")

    ordered = work.sort_values(
        by=[PLAYER_ID_COLUMN, PAYOUT_TS_COLUMN, BET_ID_COLUMN],
        kind="mergesort",
    )
    smoothed = ordered.groupby(PLAYER_ID_COLUMN, sort=False)["_score"].transform(
        lambda s: s.rolling(window=window, min_periods=1).mean(),
    )
    return smoothed.reindex(work.index)


def aggregate_bets_to_player_game_policy(
    df: pd.DataFrame,
    scores: np.ndarray,
    *,
    split_name: str,
    policy: str,
) -> PlayerGameAggregationResult:
    """Aggregate bet-level scores to player-game grain under ``policy``."""

    if policy == "max_score":
        if len(df) != int(len(scores)):
            raise ValueError(
                f"aggregate_bets_to_player_game_policy: len mismatch split={split_name!r} "
                f"policy={policy!r}",
            )
        work = df[
            [
                PLAYER_ID_COLUMN,
                GAME_ID_COLUMN,
                LABEL_COLUMN,
                PAYOUT_TS_COLUMN,
                BET_ID_COLUMN,
                "gaming_day_event",
            ]
        ].copy()
        work["_score"] = np.asarray(scores, dtype=np.float64).reshape(-1)
        work[PLAYER_ID_COLUMN] = _coerce_group_id_series(work[PLAYER_ID_COLUMN])
        work[GAME_ID_COLUMN] = _coerce_group_id_series(work[GAME_ID_COLUMN])
        lbl = pd.to_numeric(work[LABEL_COLUMN], errors="coerce")
        alert_ts = pd.to_datetime(work[PAYOUT_TS_COLUMN], errors="coerce")
        valid = (
            work[PLAYER_ID_COLUMN].notna()
            & work[GAME_ID_COLUMN].notna()
            & lbl.notna()
            & alert_ts.notna()
            & np.isfinite(work["_score"].to_numpy())
        )
        excluded = int((~valid).sum())
        work = work.loc[valid].copy()
        if work.empty:
            return PlayerGameAggregationResult(
                y_true=np.array([], dtype=np.int8),
                scores=np.array([], dtype=np.float64),
                excluded_bets=excluded,
                player_game_count=0,
                bet_count=0,
                candidates=pd.DataFrame(
                    columns=[
                        PLAYER_ID_COLUMN,
                        GAME_ID_COLUMN,
                        "player_game_score",
                        "player_game_label",
                        ALERT_TS_COLUMN,
                        BET_ID_COLUMN,
                        "gaming_day_event",
                    ],
                ),
            )
        work = work.sort_values(
            by=["_score", PAYOUT_TS_COLUMN, BET_ID_COLUMN],
            ascending=[False, True, True],
            kind="mergesort",
        )
        grouped = (
            work.groupby([PLAYER_ID_COLUMN, GAME_ID_COLUMN], as_index=False, dropna=True)
            .agg(
                player_game_score=("_score", "max"),
                player_game_label=(LABEL_COLUMN, "max"),
                bet_count=(LABEL_COLUMN, "count"),
            )
        )
        rep = work.groupby([PLAYER_ID_COLUMN, GAME_ID_COLUMN], as_index=False, dropna=True).first()
        candidates = grouped.merge(
            rep[
                [
                    PLAYER_ID_COLUMN,
                    GAME_ID_COLUMN,
                    PAYOUT_TS_COLUMN,
                    BET_ID_COLUMN,
                    "gaming_day_event",
                ]
            ],
            on=[PLAYER_ID_COLUMN, GAME_ID_COLUMN],
            how="left",
        )
        candidates = candidates.rename(columns={PAYOUT_TS_COLUMN: ALERT_TS_COLUMN})
        return PlayerGameAggregationResult(
            y_true=np.asarray(grouped["player_game_label"], dtype=np.int8),
            scores=np.asarray(grouped["player_game_score"], dtype=np.float64),
            excluded_bets=excluded,
            player_game_count=int(len(grouped)),
            bet_count=int(len(work)),
            candidates=candidates,
        )

    if policy == "top3_mean_score":
        return aggregate_bets_to_player_game(df, scores, split_name=split_name)

    if len(df) != int(len(scores)):
        raise ValueError(
            f"aggregate_bets_to_player_game_policy: len mismatch split={split_name!r} "
            f"policy={policy!r}",
        )
    work = df[
        [
            PLAYER_ID_COLUMN,
            GAME_ID_COLUMN,
            LABEL_COLUMN,
            PAYOUT_TS_COLUMN,
            BET_ID_COLUMN,
            "gaming_day_event",
        ]
    ].copy()
    work["_score"] = np.asarray(scores, dtype=np.float64).reshape(-1)
    work[PLAYER_ID_COLUMN] = _coerce_group_id_series(work[PLAYER_ID_COLUMN])
    work[GAME_ID_COLUMN] = _coerce_group_id_series(work[GAME_ID_COLUMN])
    lbl = pd.to_numeric(work[LABEL_COLUMN], errors="coerce")
    alert_ts = pd.to_datetime(work[PAYOUT_TS_COLUMN], errors="coerce")
    valid = (
        work[PLAYER_ID_COLUMN].notna()
        & work[GAME_ID_COLUMN].notna()
        & lbl.notna()
        & alert_ts.notna()
        & np.isfinite(work["_score"].to_numpy())
    )
    excluded = int((~valid).sum())
    work = work.loc[valid].copy()
    if work.empty:
        return PlayerGameAggregationResult(
            y_true=np.array([], dtype=np.int8),
            scores=np.array([], dtype=np.float64),
            excluded_bets=excluded,
            player_game_count=0,
            bet_count=0,
            candidates=pd.DataFrame(
                columns=[
                    PLAYER_ID_COLUMN,
                    GAME_ID_COLUMN,
                    "player_game_score",
                    "player_game_label",
                    ALERT_TS_COLUMN,
                    BET_ID_COLUMN,
                    "gaming_day_event",
                ],
            ),
        )

    if policy.endswith("_then_max"):
        work["_score"] = _smooth_bet_scores(work, policy)

    work = work.sort_values(
        by=[PLAYER_ID_COLUMN, GAME_ID_COLUMN, PAYOUT_TS_COLUMN, BET_ID_COLUMN],
        kind="mergesort",
    )

    def _agg_score(s: pd.Series) -> float:
        return _group_score(s.to_numpy(), policy)

    grouped = (
        work.groupby([PLAYER_ID_COLUMN, GAME_ID_COLUMN], as_index=False, dropna=True)
        .agg(
            player_game_score=("_score", _agg_score),
            player_game_label=(LABEL_COLUMN, "max"),
            bet_count=(LABEL_COLUMN, "count"),
        )
    )
    rep = (
        work.groupby([PLAYER_ID_COLUMN, GAME_ID_COLUMN], as_index=False, dropna=True)
        .last()
    )
    candidates = grouped.merge(
        rep[
            [
                PLAYER_ID_COLUMN,
                GAME_ID_COLUMN,
                PAYOUT_TS_COLUMN,
                BET_ID_COLUMN,
                "gaming_day_event",
            ]
        ],
        on=[PLAYER_ID_COLUMN, GAME_ID_COLUMN],
        how="left",
    )
    candidates = candidates.rename(columns={PAYOUT_TS_COLUMN: ALERT_TS_COLUMN})
    return PlayerGameAggregationResult(
        y_true=np.asarray(grouped["player_game_label"], dtype=np.int8),
        scores=np.asarray(grouped["player_game_score"], dtype=np.float64),
        excluded_bets=excluded,
        player_game_count=int(len(grouped)),
        bet_count=int(len(work)),
        candidates=candidates,
    )


def _score_split_frame(
    bundle: dict[str, Any],
    split_path: Path,
) -> tuple[pd.DataFrame, np.ndarray, float | None]:
    """Load parquet and score all bets once."""

    feature_columns = tuple(bundle["feature_columns"])
    columns = list(dict.fromkeys([*_META_COLUMNS, *feature_columns]))
    frame = pd.read_parquet(split_path, columns=columns)
    x_mat = prepare_lgbm_feature_matrix(
        frame,
        feature_columns=feature_columns,
        categorical_columns=tuple(bundle.get("categorical_columns", ())),
        category_categories=dict(bundle.get("category_categories", {})),
    )
    bet_scores = bundle["model"].predict_proba(x_mat)[:, 1]
    return frame, bet_scores, _window_hours(frame)


def _load_scored_split(
    bundle: dict[str, Any],
    split_path: Path,
    split_name: str,
    policy: str,
    *,
    cached: tuple[pd.DataFrame, np.ndarray, float | None] | None = None,
) -> ScoredSplit:
    """Load parquet, score bets, aggregate with policy."""

    if cached is None:
        frame, bet_scores, window_hours = _score_split_frame(bundle, split_path)
    else:
        frame, bet_scores, window_hours = cached
    player_game = aggregate_bets_to_player_game_policy(
        frame,
        bet_scores,
        split_name=split_name,
        policy=policy,
    )
    return ScoredSplit(split_name, frame, bet_scores, player_game, window_hours)


def _threshold_table(
    y_true: np.ndarray,
    scores: np.ndarray,
    window_hours: float | None,
) -> pd.DataFrame:
    """Enumerate player-game thresholds and naive metrics."""

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
    out = pd.DataFrame(
        {
            "threshold": scs[boundaries],
            "alerts": alerts,
            "precision": precision,
            "recall": recall,
        },
    )
    out["alerts_per_hour"] = np.nan if window_hours is None else out["alerts"] / float(window_hours)
    return out


def _pick_at_precision(table: pd.DataFrame, min_precision: float) -> pd.Series | None:
    """Pick max-recall row under a precision floor."""

    feasible = table.loc[table["precision"] >= float(min_precision) - 1e-15]
    if feasible.empty:
        return None
    return feasible.sort_values(
        ["recall", "precision", "threshold"],
        ascending=[False, False, False],
    ).iloc[0]


def _metrics_at_threshold(split: ScoredSplit, threshold: float) -> dict[str, Any]:
    """Return player-game and operational metrics at one threshold."""

    pg = split_metrics_block(
        split.name,
        split.player_game.y_true,
        split.player_game.scores,
        threshold,
        window_hours=split.window_hours,
    )
    op = operational_simulated_metrics_block(
        split.name,
        split.player_game.candidates,
        threshold,
        window_hours=split.window_hours,
    )
    return {**pg, **op}


def _attach_gaming_day(
    split: ScoredSplit,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    """Attach ``gaming_day_event`` to candidate/alert rows via player-game keys."""

    if "gaming_day_event" in frame.columns:
        return frame
    day_map = (
        split.frame[[PLAYER_ID_COLUMN, GAME_ID_COLUMN, "gaming_day_event"]]
        .drop_duplicates(subset=[PLAYER_ID_COLUMN, GAME_ID_COLUMN])
    )
    return frame.merge(day_map, on=[PLAYER_ID_COLUMN, GAME_ID_COLUMN], how="left")


def _fp_player_day_diagnostics(
    split: ScoredSplit,
    threshold: float,
) -> dict[str, Any]:
    """Compute player-day FP concentration for raised operational alerts."""

    from trainer_hightier.evaluation.player_alert_policy import simulate_player_cooldown_alerts

    simulated = simulate_player_cooldown_alerts(
        split.player_game.candidates,
        threshold=threshold,
    )
    raised = simulated.loc[simulated["is_raised"]].copy()
    if raised.empty:
        return {
            "top10_player_day_fp_share": 0.0,
            "top1_player_day_fp_count": 0,
            "player_days_with_alerts": 0,
        }
    raised = _attach_gaming_day(split, raised)
    raised["is_fp"] = (
        pd.to_numeric(raised["player_game_label"], errors="coerce").fillna(0).astype(int) == 0
    )
    raised["_pd_key"] = (
        raised[PLAYER_ID_COLUMN].astype(str)
        + "@"
        + raised["gaming_day_event"].astype(str)
    )
    fp_by_day = raised.loc[raised["is_fp"]].groupby("_pd_key").size()
    total_fp = int(raised["is_fp"].sum())
    if total_fp == 0:
        return {
            "top10_player_day_fp_share": 0.0,
            "top1_player_day_fp_count": 0,
            "player_days_with_alerts": int(raised["_pd_key"].nunique()),
        }
    top10_fp = int(fp_by_day.sort_values(ascending=False).head(10).sum())
    top1_fp = int(fp_by_day.max()) if not fp_by_day.empty else 0
    return {
        "top10_player_day_fp_share": float(top10_fp) / float(total_fp),
        "top1_player_day_fp_count": top1_fp,
        "player_days_with_alerts": int(raised["_pd_key"].nunique()),
    }


def _persistence_min_count(policy: str) -> int:
    """Minimum evidence count required for a persistence policy."""

    if policy == "n2_within_15m":
        return 2
    if policy == "n3_within_15m":
        return 3
    if policy == "two_of_last_five":
        return 2
    raise ValueError(f"unknown persistence policy {policy!r}")


def _filter_candidates_persistence(
    split: ScoredSplit,
    threshold: float,
    policy: str,
) -> pd.DataFrame:
    """Drop player-game candidates failing persistence at ``threshold``."""

    candidates = split.player_game.candidates.copy()
    if candidates.empty:
        return candidates
    scores = pd.to_numeric(candidates["player_game_score"], errors="coerce")
    candidates = candidates.loc[scores >= float(threshold)].copy()
    if candidates.empty:
        return candidates

    bets = split.frame[[PLAYER_ID_COLUMN, PAYOUT_TS_COLUMN]].copy()
    bets["_score"] = split.bet_scores
    bets["_ts"] = pd.to_datetime(bets[PAYOUT_TS_COLUMN], errors="coerce")
    bets["_above"] = bets["_score"] >= float(threshold)
    cand = candidates.copy()
    cand["_alert_ts"] = pd.to_datetime(cand[ALERT_TS_COLUMN], errors="coerce")
    min_count = _persistence_min_count(policy)

    if policy in ("n2_within_15m", "n3_within_15m"):
        cand["_window_start"] = cand["_alert_ts"] - pd.Timedelta(minutes=15)
        cand["_cand_idx"] = cand.index
        merged = cand.merge(bets, on=PLAYER_ID_COLUMN, how="left")
        in_window = (
            merged["_above"]
            & (merged["_ts"] >= merged["_window_start"])
            & (merged["_ts"] <= merged["_alert_ts"])
        )
        counts = merged.loc[in_window].groupby("_cand_idx").size()
        keep_idx = counts[counts >= min_count].index
        return candidates.loc[keep_idx].copy()

    if policy == "two_of_last_five":
        keep_idx: list[Any] = []
        for idx, row in cand.iterrows():
            player_bets = bets.loc[bets[PLAYER_ID_COLUMN] == row[PLAYER_ID_COLUMN]].copy()
            player_bets = player_bets.loc[player_bets["_ts"] <= row["_alert_ts"]].tail(5)
            if int(player_bets["_above"].sum()) >= min_count:
                keep_idx.append(idx)
        return candidates.loc[keep_idx].copy()

    raise ValueError(f"unknown persistence policy {policy!r}")


def _metrics_persistence_at_threshold(
    split: ScoredSplit,
    threshold: float,
    policy: str,
) -> dict[str, Any]:
    """Operational metrics with persistence-filtered candidates."""

    filtered = _filter_candidates_persistence(split, threshold, policy)
    y_true = split.player_game.y_true
    scores = split.player_game.scores
    pg = split_metrics_block(
        split.name,
        y_true,
        scores,
        threshold,
        window_hours=split.window_hours,
    )
    op = operational_simulated_metrics_block(
        split.name,
        filtered,
        threshold,
        window_hours=split.window_hours,
    )
    diag = _fp_player_day_diagnostics_persistence(split, threshold, policy, filtered)
    return {**pg, **op, **diag}


def _fp_player_day_diagnostics_persistence(
    split: ScoredSplit,
    threshold: float,
    policy: str,
    filtered: pd.DataFrame,
) -> dict[str, Any]:
    """Player-day FP diagnostics for persistence-filtered candidates."""

    from trainer_hightier.evaluation.player_alert_policy import simulate_player_cooldown_alerts

    simulated = simulate_player_cooldown_alerts(filtered, threshold=threshold)
    raised = simulated.loc[simulated["is_raised"]].copy()
    if raised.empty or "gaming_day_event" not in filtered.columns:
        return {
            "top10_player_day_fp_share": 0.0,
            "top1_player_day_fp_count": 0,
            "player_days_with_alerts": 0,
        }
    raised = raised.merge(
        filtered[[PLAYER_ID_COLUMN, GAME_ID_COLUMN, "gaming_day_event"]],
        on=[PLAYER_ID_COLUMN, GAME_ID_COLUMN],
        how="left",
    )
    raised["is_fp"] = (
        pd.to_numeric(raised["player_game_label"], errors="coerce").fillna(0).astype(int) == 0
    )
    raised["_pd_key"] = (
        raised[PLAYER_ID_COLUMN].astype(str)
        + "@"
        + raised["gaming_day_event"].astype(str)
    )
    fp_by_day = raised.loc[raised["is_fp"]].groupby("_pd_key").size()
    total_fp = int(raised["is_fp"].sum())
    if total_fp == 0:
        return {
            "top10_player_day_fp_share": 0.0,
            "top1_player_day_fp_count": 0,
            "player_days_with_alerts": int(raised["_pd_key"].nunique()),
        }
    top10_fp = int(fp_by_day.sort_values(ascending=False).head(10).sum())
    return {
        "top10_player_day_fp_share": float(top10_fp) / float(total_fp),
        "top1_player_day_fp_count": int(fp_by_day.max()),
        "player_days_with_alerts": int(raised["_pd_key"].nunique()),
    }


def _collect_thresholds(val_table: pd.DataFrame) -> list[tuple[str, float]]:
    """Collect validation-selected thresholds for floor and alert-target views."""

    picks: list[tuple[str, float]] = []
    seen: set[float] = set()
    for floor in PRECISION_FLOORS:
        pick = _pick_at_precision(val_table, floor)
        if pick is None:
            continue
        thr = float(pick["threshold"])
        if thr not in seen:
            picks.append((f"floor_{floor:.2f}", thr))
            seen.add(thr)
    feasible = val_table.loc[val_table["precision"] >= 0.6 - 1e-15]
    for target in ALERT_TARGETS:
        under = feasible.loc[feasible["alerts_per_hour"] <= target + 1e-15]
        if under.empty:
            continue
        pick = under.sort_values("recall", ascending=False).iloc[0]
        thr = float(pick["threshold"])
        if thr not in seen:
            picks.append((f"target_{target:.2f}hr", thr))
            seen.add(thr)
    best = _pick_at_precision(val_table, 0.6)
    if best is not None:
        thr = float(best["threshold"])
        if thr not in seen:
            picks.append(("best_min_prec_0.60", thr))
    return picks


def _evaluate_score_policy(
    policy: str,
    bundle: dict[str, Any],
    splits_dir: Path,
    *,
    val_cached: tuple[pd.DataFrame, np.ndarray, float | None],
    test_cached: tuple[pd.DataFrame, np.ndarray, float | None],
) -> list[dict[str, Any]]:
    """Evaluate one score-based policy across val/test threshold picks."""

    val_split = _load_scored_split(
        bundle, splits_dir / "val.parquet", "val", policy, cached=val_cached,
    )
    test_split = _load_scored_split(
        bundle, splits_dir / "test.parquet", "test", policy, cached=test_cached,
    )
    val_table = _threshold_table(
        val_split.player_game.y_true,
        val_split.player_game.scores,
        val_split.window_hours,
    )
    rows: list[dict[str, Any]] = []
    threshold_picks = _collect_thresholds(val_table)
    seen_thr = {thr for _, thr in threshold_picks}
    for pick_name, threshold in operational_threshold_picks_for_targets(
        val_split.player_game.candidates,
        window_hours=val_split.window_hours,
        split_prefix="val",
    ):
        if threshold not in seen_thr:
            threshold_picks.append((pick_name, threshold))
            seen_thr.add(threshold)
    for pick_name, threshold in threshold_picks:
        val_m = _metrics_at_threshold(val_split, threshold)
        test_m = _metrics_at_threshold(test_split, threshold)
        test_diag = _fp_player_day_diagnostics(test_split, threshold)
        rows.append(_build_result_row(policy, pick_name, threshold, val_m, test_m, test_diag))
    return rows


def _evaluate_persistence_policy(
    policy: str,
    bundle: dict[str, Any],
    splits_dir: Path,
    *,
    val_cached: tuple[pd.DataFrame, np.ndarray, float | None],
    test_cached: tuple[pd.DataFrame, np.ndarray, float | None],
) -> list[dict[str, Any]]:
    """Evaluate persistence policy using max_score val threshold picks."""

    val_split = _load_scored_split(
        bundle, splits_dir / "val.parquet", "val", "max_score", cached=val_cached,
    )
    test_split = _load_scored_split(
        bundle, splits_dir / "test.parquet", "test", "max_score", cached=test_cached,
    )
    val_table = _threshold_table(
        val_split.player_game.y_true,
        val_split.player_game.scores,
        val_split.window_hours,
    )
    rows: list[dict[str, Any]] = []
    threshold_picks = _collect_thresholds(val_table)
    seen_thr = {thr for _, thr in threshold_picks}
    for pick_name, threshold in operational_threshold_picks_for_targets(
        val_split.player_game.candidates,
        window_hours=val_split.window_hours,
        split_prefix="val",
    ):
        if threshold not in seen_thr:
            threshold_picks.append((pick_name, threshold))
            seen_thr.add(threshold)
    for pick_name, threshold in threshold_picks:
        val_m = _metrics_persistence_at_threshold(val_split, threshold, policy)
        test_m = _metrics_persistence_at_threshold(test_split, threshold, policy)
        test_diag = {
            "top10_player_day_fp_share": test_m.get("top10_player_day_fp_share"),
            "top1_player_day_fp_count": test_m.get("top1_player_day_fp_count"),
            "player_days_with_alerts": test_m.get("player_days_with_alerts"),
        }
        rows.append(_build_result_row(policy, pick_name, threshold, val_m, test_m, test_diag))
    return rows


def _build_result_row(
    policy: str,
    pick_name: str,
    threshold: float,
    val_m: dict[str, Any],
    test_m: dict[str, Any],
    test_diag: dict[str, Any],
) -> dict[str, Any]:
    """Build one CSV/report row."""

    prefix_val = "val"
    prefix_test = "test"
    return {
        "policy": policy,
        "pick_name": pick_name,
        "threshold": threshold,
        "val_pg_precision": val_m.get(f"{prefix_val}_precision"),
        "val_pg_alerts_per_hour": val_m.get(f"{prefix_val}_alerts_per_hour"),
        "val_op_precision": val_m.get(f"{prefix_val}_operational_simulated_precision"),
        "val_op_alerts_per_hour": val_m.get(f"{prefix_val}_operational_simulated_alerts_per_hour"),
        "val_op_suppression_rate": val_m.get(f"{prefix_val}_operational_simulated_suppression_rate"),
        "test_op_precision": test_m.get(f"{prefix_test}_operational_simulated_precision"),
        "test_op_alerts_per_hour": test_m.get(f"{prefix_test}_operational_simulated_alerts_per_hour"),
        "test_op_alerts": test_m.get(f"{prefix_test}_operational_simulated_alerts"),
        "test_op_tp": test_m.get(f"{prefix_test}_operational_simulated_true_positives"),
        "test_op_fp": test_m.get(f"{prefix_test}_operational_simulated_false_positives"),
        "test_top10_fp_share": test_diag.get("top10_player_day_fp_share"),
        "test_top1_fp_count": test_diag.get("top1_player_day_fp_count"),
        "test_player_days_with_alerts": test_diag.get("player_days_with_alerts"),
    }


def _baseline_reproduction_check(
    bundle: dict[str, Any],
    splits_dir: Path,
    metrics_path: Path,
) -> dict[str, Any]:
    """Compare max_score study output to stored training metrics."""

    test_split = _load_scored_split(bundle, splits_dir / "test.parquet", "test", "max_score")
    threshold = float(bundle["threshold"])
    test_m = _metrics_at_threshold(test_split, threshold)
    stored: dict[str, Any] = {}
    if metrics_path.is_file():
        stored = json.loads(metrics_path.read_text(encoding="utf-8"))
    return {
        "bundle_threshold": threshold,
        "study_test_op_precision": test_m.get("test_operational_simulated_precision"),
        "study_test_op_alerts_per_hour": test_m.get("test_operational_simulated_alerts_per_hour"),
        "stored_test_op_precision": stored.get("test_operational_simulated_precision"),
        "stored_test_op_alerts_per_hour": stored.get("test_operational_simulated_alerts_per_hour"),
        "stored_step5_threshold": stored.get("step5_threshold"),
    }


def _format_pct(value: float | None) -> str:
    """Format proportion as percentage."""

    if value is None or not np.isfinite(float(value)):
        return "NA"
    return f"{100.0 * float(value):.2f}%"


def _format_num(value: float | int | None, digits: int = 3) -> str:
    """Format numeric value."""

    if value is None or not np.isfinite(float(value)):
        return "NA"
    return f"{float(value):.{digits}f}"


def _render_report(
    model_dir: Path,
    baseline_check: dict[str, Any],
    results_df: pd.DataFrame,
) -> str:
    """Render markdown frontier study report."""

    baseline_rows = results_df[
        (results_df["policy"] == "max_score") & (results_df["pick_name"] == "best_min_prec_0.60")
    ]
    best_prec = results_df.sort_values(
        ["test_op_precision", "test_op_alerts_per_hour"],
        ascending=[False, False],
    ).head(5)
    best_vol = results_df.sort_values(
        ["test_op_alerts_per_hour", "test_op_precision"],
        ascending=[False, False],
    ).head(5)
    cols = [
        ("policy", "policy"),
        ("pick", "pick_name"),
        ("thr", "threshold"),
        ("val op prec", "val_op_precision"),
        ("val op/hr", "val_op_alerts_per_hour"),
        ("test op prec", "test_op_precision"),
        ("test op/hr", "test_op_alerts_per_hour"),
        ("top10 FP%", "test_top10_fp_share"),
    ]

    def _md_table(df: pd.DataFrame) -> str:
        rows = []
        for _, row in df.iterrows():
            rows.append(
                {
                    "policy": row["policy"],
                    "pick_name": row["pick_name"],
                    "threshold": _format_num(row["threshold"], 6),
                    "val_op_precision": _format_pct(row["val_op_precision"]),
                    "val_op_alerts_per_hour": _format_num(row["val_op_alerts_per_hour"], 3),
                    "test_op_precision": _format_pct(row["test_op_precision"]),
                    "test_op_alerts_per_hour": _format_num(row["test_op_alerts_per_hour"], 3),
                    "test_top10_fp_share": _format_pct(row["test_top10_fp_share"]),
                },
            )
        header = "| " + " | ".join(c[0] for c in cols) + " |"
        sep = "| " + " | ".join("---" for _ in cols) + " |"
        body = [
            "| "
            + " | ".join(str(r[c[1]]) for c in cols)
            + " |"
            for r in rows
        ]
        return "\n".join([header, sep, *body]) if rows else "No rows."

    success = results_df[
        (results_df["test_op_precision"] >= 0.50)
        & (results_df["test_op_alerts_per_hour"] >= 0.75)
    ]
    baseline_floor = results_df[
        (results_df["policy"] == "max_score") & (results_df["pick_name"] == "floor_0.60")
    ]
    alt_floor = results_df[results_df["pick_name"] == "floor_0.60"].sort_values(
        "test_op_alerts_per_hour",
        ascending=False,
    )
    if not baseline_floor.empty and not alt_floor.empty:
        base = baseline_floor.iloc[0]
        best_alt = alt_floor.iloc[0]
        alt_gain_hr = float(best_alt["test_op_alerts_per_hour"]) - float(base["test_op_alerts_per_hour"])
        alt_gain_prec = float(best_alt["test_op_precision"]) - float(base["test_op_precision"])
        if alt_gain_hr >= 0.10 and alt_gain_prec >= -0.02:
            conclusion = (
                f"Alternative aggregation improves the min_prec=0.60 frontier: "
                f"best `{best_alt['policy']}` yields test op "
                f"{100*best_alt['test_op_precision']:.1f}% @ "
                f"{best_alt['test_op_alerts_per_hour']:.3f}/hr vs baseline max_score "
                f"{100*base['test_op_precision']:.1f}% @ {base['test_op_alerts_per_hour']:.3f}/hr. "
                "Recommend prototyping evaluation/serving aggregation change before trajectory features."
            )
        elif not success.empty:
            conclusion = (
                "At least one policy approaches the success gate "
                "(test op >=50% @ >=0.75/hr). Consider implementing the best policy in evaluation/serving."
            )
        else:
            conclusion = (
                "Policies do not reach 50% @ 0.75/hr, and gains at min_prec=0.60 are modest. "
                "Proceed to PIT-safe trajectory feature engineering."
            )
    elif not success.empty:
        conclusion = (
            "At least one policy approaches the success gate "
            "(test op >=50% @ >=0.75/hr). Consider implementing the best policy in evaluation/serving."
        )
    else:
        conclusion = (
            "No policy meaningfully improves the operational frontier vs baseline. "
            "Proceed to PIT-safe trajectory feature engineering."
        )

    return "\n\n".join(
        [
            "# Frontier Study Report",
            f"Model: `{model_dir}`",
            "## Baseline Reproduction",
            f"- Bundle threshold: `{baseline_check['bundle_threshold']:.6f}`",
            f"- Study test op precision: `{_format_pct(baseline_check['study_test_op_precision'])}`",
            f"- Stored test op precision: `{_format_pct(baseline_check['stored_test_op_precision'])}`",
            f"- Study test op alerts/hr: `{_format_num(baseline_check['study_test_op_alerts_per_hour'], 3)}`",
            f"- Stored test op alerts/hr: `{_format_num(baseline_check['stored_test_op_alerts_per_hour'], 3)}`",
            "## Best Test Operational Precision (top 5)",
            _md_table(best_prec),
            "## Best Test Operational Alert Rate (top 5)",
            _md_table(best_vol),
            "## Conclusion",
            conclusion,
        ],
    ) + "\n"


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, default=_DEFAULT_MODEL_DIR)
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS_DIR)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    return parser.parse_args()


def main() -> None:
    """Run frontier study and write report artifacts."""

    args = parse_args()
    bundle = _load_bundle(args.model_dir)
    metrics_path = Path(args.model_dir) / "training_metrics.json"
    baseline_check = _baseline_reproduction_check(bundle, args.splits_dir, metrics_path)

    val_cached = _score_split_frame(bundle, args.splits_dir / "val.parquet")
    test_cached = _score_split_frame(bundle, args.splits_dir / "test.parquet")

    all_rows: list[dict[str, Any]] = []
    for policy in SCORE_POLICIES:
        all_rows.extend(
            _evaluate_score_policy(
                policy, bundle, args.splits_dir, val_cached=val_cached, test_cached=test_cached,
            ),
        )
    for policy in PERSISTENCE_POLICIES:
        all_rows.extend(
            _evaluate_persistence_policy(
                policy, bundle, args.splits_dir, val_cached=val_cached, test_cached=test_cached,
            ),
        )

    results_df = pd.DataFrame(all_rows)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    run_id = Path(args.model_dir).name.split("-")[0]
    csv_path = args.out_dir / f"{run_id}_frontier_results.csv"
    report_path = args.out_dir / f"{run_id}_frontier_report.md"
    diag_path = args.out_dir / f"{run_id}_policy_diagnostics.json"

    results_df.to_csv(csv_path, index=False)
    report = _render_report(args.model_dir, baseline_check, results_df)
    report_path.write_text(report, encoding="utf-8")
    diag_path.write_text(
        json.dumps({"baseline_reproduction": baseline_check}, indent=2, default=str),
        encoding="utf-8",
    )
    print(report)
    print(f"Wrote {csv_path}")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
