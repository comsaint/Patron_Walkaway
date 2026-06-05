"""Offline player-level alert cooldown simulation for Step 5 evaluation."""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Final, Mapping

import numpy as np
import pandas as pd

from trainer_hightier.config import ALERT_HORIZON_MIN, PlayerAlertPolicyConfig
from trainer_hightier.evaluation.metrics_blocks import metrics_at_threshold

PLAYER_ID_COLUMN: Final[str] = "player_id"
GAME_ID_COLUMN: Final[str] = "game_id"
SCORE_COLUMN: Final[str] = "player_game_score"
LABEL_COLUMN: Final[str] = "player_game_label"
ALERT_TS_COLUMN: Final[str] = "alert_ts"
TIE_BREAK_COLUMN: Final[str] = "bet_id"
TRAIN_ALERT_TS_SOURCE: Final[str] = "payout_complete_dtm"
POLICY_METADATA_KEYS: Final[tuple[str, ...]] = (
    "player_alert_policy_suppression_enabled",
    "player_alert_policy_cooldown_min",
    "player_alert_policy_threshold_selection_enabled",
    "player_alert_policy_sample_weight_enabled",
    "player_alert_policy_train_alert_ts_source",
    "player_alert_policy_operational_metrics_reported",
)


@dataclass(frozen=True)
class PlayerAlertPolicyDecision:
    """Per-representative-bet serving suppression audit fields for ``prediction_log``."""

    candidate: bool
    raised: bool
    suppressed: bool
    suppression_reason: str | None
    cooldown_min: int | None
    last_raised_ts: str | None
    decision_ts: str | None


def build_player_alert_policy_metadata(
    policy: PlayerAlertPolicyConfig,
    *,
    train_alert_ts_source: str = TRAIN_ALERT_TS_SOURCE,
    operational_metrics_reported: bool = True,
) -> dict[str, Any]:
    """Flatten shared policy settings for training metrics / model bundle metadata."""

    return {
        "player_alert_policy_suppression_enabled": bool(policy.suppression_enabled),
        "player_alert_policy_cooldown_min": int(policy.cooldown_min),
        "player_alert_policy_threshold_selection_enabled": bool(policy.threshold_selection_enabled),
        "player_alert_policy_sample_weight_enabled": bool(policy.sample_weight_enabled),
        "player_alert_policy_train_alert_ts_source": str(train_alert_ts_source),
        "player_alert_policy_operational_metrics_reported": bool(operational_metrics_reported),
    }


def compare_player_alert_policies(
    artifact_metrics: Mapping[str, Any],
    serving_policy: PlayerAlertPolicyConfig,
) -> list[str]:
    """Return human-readable mismatch lines; empty when aligned or artifact keys are absent."""

    if not any(k in artifact_metrics for k in POLICY_METADATA_KEYS):
        return []
    mismatches: list[str] = []
    art_sup = artifact_metrics.get("player_alert_policy_suppression_enabled")
    if art_sup is not None and bool(art_sup) != bool(serving_policy.suppression_enabled):
        mismatches.append(
            f"suppression_enabled artifact={bool(art_sup)} serving={bool(serving_policy.suppression_enabled)}",
        )
    art_cd = artifact_metrics.get("player_alert_policy_cooldown_min")
    if art_cd is not None and int(art_cd) != int(serving_policy.cooldown_min):
        mismatches.append(
            f"cooldown_min artifact={int(art_cd)} serving={int(serving_policy.cooldown_min)}",
        )
    return mismatches


def warn_player_alert_policy_mismatch(
    logger: logging.Logger,
    *,
    training_metrics: Mapping[str, Any],
    serving_policy: PlayerAlertPolicyConfig,
) -> None:
    """Log structured warning when artifact policy differs from serving config."""

    if not any(k in training_metrics for k in POLICY_METADATA_KEYS):
        logger.debug(
            "[hightier_scorer] player_alert_policy_artifact_missing: no training policy metadata",
        )
        return
    mismatches = compare_player_alert_policies(training_metrics, serving_policy)
    if mismatches:
        logger.warning(
            "[hightier_scorer] player_alert_policy_mismatch: %s",
            "; ".join(mismatches),
        )


def load_last_player_alert_ts_by_player(
    conn: sqlite3.Connection,
    player_ids: list[int],
) -> dict[int, pd.Timestamp]:
    """Return ``MAX(ts)`` per ``player_id`` from raised alerts in ``state.db``."""

    if not player_ids:
        return {}
    uniq = sorted({int(p) for p in player_ids})
    placeholders = ",".join("?" * len(uniq))
    rows = conn.execute(
        f"SELECT player_id, MAX(ts) FROM alerts WHERE player_id IN ({placeholders}) GROUP BY player_id",
        uniq,
    ).fetchall()
    out: dict[int, pd.Timestamp] = {}
    for pid, ts in rows:
        if ts is None:
            continue
        parsed = pd.Timestamp(ts)
        if pd.notna(parsed):
            out[int(pid)] = parsed
    return out


def _suppression_reason(cooldown_min: int) -> str:
    return f"player_cooldown_{int(cooldown_min)}m"


def _normalize_bet_id_key(bet_id: object) -> str:
    if bet_id is None or (isinstance(bet_id, float) and math.isnan(bet_id)):
        return ""
    return str(bet_id).strip()


def apply_serving_player_alert_suppression(
    alerts_df: pd.DataFrame,
    *,
    conn: sqlite3.Connection | None,
    suppression_enabled: bool,
    cooldown_min: int,
    alert_ts_col: str = "ts",
    score_col: str = "score",
    tie_break_col: str = "bet_id",
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, PlayerAlertPolicyDecision]]:
    """Apply production cooldown to player-game alert candidates.

    Returns raised alerts, suppressed alerts, and a decision map keyed by ``bet_id``.
    """

    cd = int(cooldown_min)
    if cd <= 0:
        raise ValueError(f"cooldown_min must be positive; got {cooldown_min!r}")
    if alerts_df.empty:
        return pd.DataFrame(), pd.DataFrame(), {}

    work = alerts_df.copy()
    work["_alert_ts"] = pd.to_datetime(work[alert_ts_col], errors="coerce")
    if work["_alert_ts"].isna().any():
        bad = int(work["_alert_ts"].isna().sum())
        raise ValueError(f"alerts has {bad} non-parsable {alert_ts_col!r} values")

    decisions: dict[str, PlayerAlertPolicyDecision] = {}
    if not suppression_enabled:
        for row in work.itertuples(index=False):
            bet_key = _normalize_bet_id_key(getattr(row, tie_break_col))
            decision_ts = str(getattr(row, alert_ts_col))
            decisions[bet_key] = PlayerAlertPolicyDecision(
                candidate=True,
                raised=True,
                suppressed=False,
                suppression_reason=None,
                cooldown_min=None,
                last_raised_ts=None,
                decision_ts=decision_ts,
            )
        return work, pd.DataFrame(), decisions

    player_ids = (
        pd.to_numeric(work["player_id"], errors="coerce").dropna().astype(int).unique().tolist()
    )
    db_last = load_last_player_alert_ts_by_player(conn, player_ids) if conn is not None else {}
    sort_cols = ["player_id", "_alert_ts", score_col]
    ascending = [True, True, False]
    if tie_break_col in work.columns:
        work["_tie_break_sort"] = pd.to_numeric(work[tie_break_col], errors="coerce").fillna(-1)
        sort_cols.append("_tie_break_sort")
        ascending.append(True)
    ordered = work.sort_values(by=sort_cols, ascending=ascending, kind="mergesort")
    cooldown_delta = pd.Timedelta(minutes=cd)
    cycle_last: dict[int, pd.Timestamp] = {}
    raised_idx: list[Any] = []
    suppressed_idx: list[Any] = []
    reason = _suppression_reason(cd)

    for idx, row in ordered.iterrows():
        pid = int(row["player_id"])
        ts = pd.Timestamp(row["_alert_ts"])
        decision_ts = str(row[alert_ts_col])
        bet_key = _normalize_bet_id_key(row[tie_break_col])
        prev_db = db_last.get(pid)
        prev_cycle = cycle_last.get(pid)
        prev = prev_db
        if prev_cycle is not None and (prev is None or prev_cycle > prev):
            prev = prev_cycle
        last_raised_iso = prev.isoformat() if prev is not None else None
        if prev is not None and (ts - prev) < cooldown_delta:
            suppressed_idx.append(idx)
            decisions[bet_key] = PlayerAlertPolicyDecision(
                candidate=True,
                raised=False,
                suppressed=True,
                suppression_reason=reason,
                cooldown_min=cd,
                last_raised_ts=last_raised_iso,
                decision_ts=decision_ts,
            )
        else:
            raised_idx.append(idx)
            cycle_last[pid] = ts
            decisions[bet_key] = PlayerAlertPolicyDecision(
                candidate=True,
                raised=True,
                suppressed=False,
                suppression_reason=None,
                cooldown_min=cd,
                last_raised_ts=last_raised_iso,
                decision_ts=decision_ts,
            )

    raised_df = work.loc[raised_idx].copy() if raised_idx else pd.DataFrame()
    suppressed_df = work.loc[suppressed_idx].copy() if suppressed_idx else pd.DataFrame()
    return raised_df, suppressed_df, decisions


_REQUIRED_CANDIDATE_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        PLAYER_ID_COLUMN,
        GAME_ID_COLUMN,
        SCORE_COLUMN,
        LABEL_COLUMN,
        ALERT_TS_COLUMN,
    },
)


def _validate_candidates_frame(candidates: pd.DataFrame) -> None:
    """Raise if ``candidates`` is missing required policy-simulation columns."""

    if candidates.empty:
        return
    missing = sorted(_REQUIRED_CANDIDATE_COLUMNS.difference(candidates.columns))
    if missing:
        raise ValueError(
            f"candidates missing required columns {missing}; "
            f"expected at least {sorted(_REQUIRED_CANDIDATE_COLUMNS)}; "
            f"got {sorted(candidates.columns)!r}",
        )
    null_ts = int(candidates[ALERT_TS_COLUMN].isna().sum())
    if null_ts > 0:
        raise ValueError(
            f"candidates has {null_ts} null {ALERT_TS_COLUMN!r} rows; "
            f"expected 0 null alert timestamps",
        )


def simulate_player_cooldown_alerts(
    candidates: pd.DataFrame,
    *,
    threshold: float,
    cooldown_min: int = ALERT_HORIZON_MIN,
    tie_break_col: str = TIE_BREAK_COLUMN,
) -> pd.DataFrame:
    """Return candidate rows with ``is_candidate``, ``is_raised``, ``is_suppressed``.

    Candidates above ``threshold`` are processed per ``player_id`` in chronological
    order. A row is suppressed when ``alert_ts - last_raised_alert_ts < cooldown_min``.
    Boundary rule: ``< cooldown_min`` suppress, ``>= cooldown_min`` allow.
    """

    if not math.isfinite(float(threshold)):
        raise ValueError(f"threshold must be finite; got {threshold!r}")
    cd = int(cooldown_min)
    if cd <= 0:
        raise ValueError(f"cooldown_min must be positive; got {cooldown_min!r}")

    if candidates.empty:
        return pd.DataFrame(
            columns=[
                *sorted(_REQUIRED_CANDIDATE_COLUMNS),
                tie_break_col,
                "is_candidate",
                "is_raised",
                "is_suppressed",
            ],
        )

    _validate_candidates_frame(candidates)
    work = candidates.copy()
    work[ALERT_TS_COLUMN] = pd.to_datetime(work[ALERT_TS_COLUMN], errors="coerce")
    if work[ALERT_TS_COLUMN].isna().any():
        bad = int(work[ALERT_TS_COLUMN].isna().sum())
        raise ValueError(f"candidates has {bad} non-parsable {ALERT_TS_COLUMN!r} values")

    scores = pd.to_numeric(work[SCORE_COLUMN], errors="coerce").to_numpy(dtype=np.float64)
    if not np.isfinite(scores).all():
        raise ValueError("player_game_score must be finite (no NaN/inf)")

    work["is_candidate"] = scores >= float(threshold)
    work["is_raised"] = False
    work["is_suppressed"] = False

    above = work.loc[work["is_candidate"]].copy()
    if above.empty:
        return work

    sort_cols = [PLAYER_ID_COLUMN, ALERT_TS_COLUMN, SCORE_COLUMN]
    ascending = [True, True, False]
    if tie_break_col in above.columns:
        above["_tie_break_sort"] = pd.to_numeric(above[tie_break_col], errors="coerce").fillna(-1)
        sort_cols.append("_tie_break_sort")
        ascending.append(True)

    above = above.sort_values(by=sort_cols, ascending=ascending, kind="mergesort")
    cooldown_delta = pd.Timedelta(minutes=cd)
    last_raised: dict[Any, pd.Timestamp] = {}

    for idx, row in above.iterrows():
        pid = row[PLAYER_ID_COLUMN]
        ts = pd.Timestamp(row[ALERT_TS_COLUMN])
        prev = last_raised.get(pid)
        if prev is not None and (ts - prev) < cooldown_delta:
            work.at[idx, "is_suppressed"] = True
        else:
            work.at[idx, "is_raised"] = True
            last_raised[pid] = ts

    return work


def operational_simulated_metrics_block(
    split: str,
    candidates: pd.DataFrame,
    threshold: float,
    *,
    cooldown_min: int = ALERT_HORIZON_MIN,
    window_hours: float | None = None,
    tie_break_col: str = TIE_BREAK_COLUMN,
) -> dict[str, Any]:
    """Build flat ``operational_simulated_*`` metrics for one split."""

    prefix = f"{split}_operational_simulated"
    if candidates.empty:
        return {
            f"{prefix}_precision": 0.0,
            f"{prefix}_recall": 0.0,
            f"{prefix}_f1": 0.0,
            f"{prefix}_alerts": 0,
            f"{prefix}_candidate_alerts": 0,
            f"{prefix}_suppressed_alerts": 0,
            f"{prefix}_suppression_rate": 0.0,
            f"{prefix}_true_positives": 0,
            f"{prefix}_false_positives": 0,
            f"{prefix}_false_negatives_conservative": 0,
            f"{prefix}_positives": 0,
            f"{prefix}_window_hours": float(window_hours) if window_hours is not None else None,
            f"{prefix}_alerts_per_hour": None,
        }

    simulated = simulate_player_cooldown_alerts(
        candidates,
        threshold=threshold,
        cooldown_min=cooldown_min,
        tie_break_col=tie_break_col,
    )
    labels_all = (
        pd.to_numeric(simulated[LABEL_COLUMN], errors="coerce").fillna(0).astype(np.int8).to_numpy()
    )
    n_pos_all = int(np.sum(labels_all == 1))

    raised_mask = simulated["is_raised"].to_numpy(dtype=bool)
    y_raised = labels_all[raised_mask]
    tp = int(np.sum(y_raised == 1))
    fp = int(np.sum(y_raised == 0))
    fn = int(n_pos_all - tp)
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / n_pos_all if n_pos_all > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0

    candidate_count = int(simulated["is_candidate"].sum())
    raised_count = int(simulated["is_raised"].sum())
    suppressed_count = int(simulated["is_suppressed"].sum())
    suppression_rate = (
        float(suppressed_count) / float(candidate_count) if candidate_count > 0 else 0.0
    )

    out = {
        f"{prefix}_precision": float(prec),
        f"{prefix}_recall": float(rec),
        f"{prefix}_f1": float(f1),
        f"{prefix}_alerts": raised_count,
        f"{prefix}_candidate_alerts": candidate_count,
        f"{prefix}_suppressed_alerts": suppressed_count,
        f"{prefix}_suppression_rate": float(suppression_rate),
        f"{prefix}_true_positives": tp,
        f"{prefix}_false_positives": fp,
        f"{prefix}_false_negatives_conservative": fn,
        f"{prefix}_positives": n_pos_all,
        f"{prefix}_window_hours": float(window_hours) if window_hours is not None else None,
        f"{prefix}_alerts_per_hour": None,
    }
    if window_hours is not None and math.isfinite(float(window_hours)) and float(window_hours) > 0:
        out[f"{prefix}_alerts_per_hour"] = float(raised_count) / float(window_hours)
    return out


def player_game_metrics_from_candidates(
    candidates: pd.DataFrame,
    threshold: float,
) -> tuple[float, float, float, int]:
    """Compute naive player-game metrics directly from a candidate frame."""

    if candidates.empty:
        return 0.0, 0.0, 0.0, 0
    y = (
        pd.to_numeric(candidates[LABEL_COLUMN], errors="coerce").fillna(0).astype(np.int8).to_numpy()
    )
    s = pd.to_numeric(candidates[SCORE_COLUMN], errors="coerce").to_numpy(dtype=np.float64)
    return metrics_at_threshold(y, s, threshold)
