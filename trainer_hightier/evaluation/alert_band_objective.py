"""Alert-band precision objective: fixed operational capacity → maximize precision."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from trainer_hightier.evaluation.player_alert_policy import (
    ALERT_HORIZON_MIN,
    SCORE_COLUMN,
    operational_simulated_metrics_block,
)

DEFAULT_TARGET_ALERTS_PER_HOUR: tuple[float, ...] = (1.0, 2.0)
_SCALAR_MIN_WEIGHT: float = 100.0
_SCALAR_MEAN_WEIGHT: float = 1.0
_SCALAR_RECALL_TIEBREAK_WEIGHT: float = 0.01


@dataclass(frozen=True)
class OperationalCapacityPoint:
    """Metrics at one target alerts/hour after threshold search + cooldown simulation."""

    target_alerts_per_hour: float
    target_alert_count: int
    threshold: float
    precision: float
    recall: float
    alerts: int
    alerts_per_hour: float | None
    true_positives: int


@dataclass(frozen=True)
class AlertBandEvaluation:
    """Band objective result for one scored validation split."""

    scalar_score: float
    deployment_target_alerts_per_hour: float
    deployment_threshold: float
    points: tuple[OperationalCapacityPoint, ...]
    min_precision: float
    mean_precision: float


def target_alert_count(window_hours: float | None, alerts_per_hour: float) -> int:
    """Convert target alerts/hour to integer alert budget for a split window."""

    if window_hours is None or not math.isfinite(float(window_hours)) or float(window_hours) <= 0:
        return 0
    return max(1, int(round(float(alerts_per_hour) * float(window_hours))))


def _rate_slug(alerts_per_hour: float) -> str:
    """Filesystem-safe slug for one alerts/hour rate (``1p0`` for 1.0)."""

    txt = f"{float(alerts_per_hour):g}".replace(".", "p")
    return re.sub(r"[^0-9a-zA-Z]+", "_", txt)


def threshold_for_target_operational_alerts(
    candidates: pd.DataFrame,
    target_alerts: int,
    *,
    window_hours: float | None,
    requested_alerts_per_hour: float,
    cooldown_min: int = ALERT_HORIZON_MIN,
    split_prefix: str = "val",
) -> OperationalCapacityPoint:
    """Binary-search threshold so operational raised alerts approximate ``target_alerts``."""

    if candidates.empty or int(target_alerts) <= 0:
        return OperationalCapacityPoint(
            target_alerts_per_hour=float(requested_alerts_per_hour),
            target_alert_count=int(target_alerts),
            threshold=float("nan"),
            precision=0.0,
            recall=0.0,
            alerts=0,
            alerts_per_hour=None,
            true_positives=0,
        )

    scores = pd.to_numeric(candidates[SCORE_COLUMN], errors="coerce").to_numpy(dtype=np.float64)
    finite = scores[np.isfinite(scores)]
    if finite.size == 0:
        return OperationalCapacityPoint(
            target_alerts_per_hour=float(requested_alerts_per_hour),
            target_alert_count=int(target_alerts),
            threshold=float("nan"),
            precision=0.0,
            recall=0.0,
            alerts=0,
            alerts_per_hour=None,
            true_positives=0,
        )

    lo = float(np.min(finite) - 1e-9)
    hi = float(np.max(finite) + 1e-9)
    best: dict[str, Any] | None = None
    for _ in range(48):
        mid = (lo + hi) / 2.0
        block = operational_simulated_metrics_block(
            split_prefix,
            candidates,
            mid,
            cooldown_min=cooldown_min,
            window_hours=window_hours,
        )
        prefix = f"{split_prefix}_operational_simulated"
        alerts = int(block[f"{prefix}_alerts"])
        err = abs(alerts - int(target_alerts))
        row = {
            "threshold": mid,
            "alerts": alerts,
            "precision": float(block[f"{prefix}_precision"]),
            "recall": float(block[f"{prefix}_recall"]),
            "alerts_per_hour": block[f"{prefix}_alerts_per_hour"],
            "true_positives": int(block[f"{prefix}_true_positives"]),
        }
        if best is None or err < best["_err"]:
            best = {**row, "_err": err}
        if alerts > int(target_alerts):
            lo = mid
        else:
            hi = mid
    assert best is not None
    ahr = best["alerts_per_hour"]
    return OperationalCapacityPoint(
        target_alerts_per_hour=float(requested_alerts_per_hour),
        target_alert_count=int(target_alerts),
        threshold=float(best["threshold"]),
        precision=float(best["precision"]),
        recall=float(best["recall"]),
        alerts=int(best["alerts"]),
        alerts_per_hour=float(ahr) if ahr is not None else None,
        true_positives=int(best["true_positives"]),
    )


def alert_band_scalar_score(
    precisions: Mapping[float, float],
    *,
    recall_at_primary: float,
    primary_target_alerts_per_hour: float = DEFAULT_TARGET_ALERTS_PER_HOUR[0],
) -> float:
    """Encode lexicographic band rule as one scalar for Optuna."""

    if not precisions:
        return -1.0
    p_vals = [float(precisions[k]) for k in sorted(precisions)]
    p_min = min(p_vals)
    p_mean = sum(p_vals) / float(len(p_vals))
    recall = float(recall_at_primary)
    return (
        _SCALAR_MIN_WEIGHT * p_min
        + _SCALAR_MEAN_WEIGHT * p_mean
        + _SCALAR_RECALL_TIEBREAK_WEIGHT * recall
    )


def evaluate_alert_band_on_candidates(
    candidates: pd.DataFrame,
    *,
    window_hours: float | None,
    target_alerts_per_hour: tuple[float, ...] = DEFAULT_TARGET_ALERTS_PER_HOUR,
    deployment_target_alerts_per_hour: float = DEFAULT_TARGET_ALERTS_PER_HOUR[0],
    cooldown_min: int = ALERT_HORIZON_MIN,
    split_prefix: str = "val",
) -> AlertBandEvaluation:
    """Evaluate precision at each target alerts/hour and return band scalar score."""

    points: list[OperationalCapacityPoint] = []
    precision_by_rate: dict[float, float] = {}
    for rate in target_alerts_per_hour:
        k = target_alert_count(window_hours, rate)
        pt = threshold_for_target_operational_alerts(
            candidates,
            k,
            window_hours=window_hours,
            requested_alerts_per_hour=float(rate),
            cooldown_min=cooldown_min,
            split_prefix=split_prefix,
        )
        points.append(pt)
        precision_by_rate[float(rate)] = float(pt.precision)

    deploy_pt = next(
        (p for p in points if math.isclose(p.target_alerts_per_hour, float(deployment_target_alerts_per_hour))),
        points[0] if points else None,
    )
    if deploy_pt is None:
        raise ValueError("evaluate_alert_band_on_candidates: no deployment target point computed")
    p_vals = list(precision_by_rate.values())
    scalar = alert_band_scalar_score(
        precision_by_rate,
        recall_at_primary=float(deploy_pt.recall),
        primary_target_alerts_per_hour=float(deployment_target_alerts_per_hour),
    )
    return AlertBandEvaluation(
        scalar_score=scalar,
        deployment_target_alerts_per_hour=float(deployment_target_alerts_per_hour),
        deployment_threshold=float(deploy_pt.threshold),
        points=tuple(points),
        min_precision=min(p_vals) if p_vals else 0.0,
        mean_precision=(sum(p_vals) / len(p_vals)) if p_vals else 0.0,
    )


def alert_band_metrics_block(
    split_prefix: str,
    candidates: pd.DataFrame,
    *,
    window_hours: float | None,
    target_alerts_per_hour: tuple[float, ...] = DEFAULT_TARGET_ALERTS_PER_HOUR,
    cooldown_min: int = ALERT_HORIZON_MIN,
) -> dict[str, Any]:
    """Flat report keys ``{split}_op_precision_at_{rate}_alert_per_hour`` etc."""

    band = evaluate_alert_band_on_candidates(
        candidates,
        window_hours=window_hours,
        target_alerts_per_hour=target_alerts_per_hour,
        cooldown_min=cooldown_min,
        split_prefix=split_prefix,
    )
    out: dict[str, Any] = {
        f"{split_prefix}_alert_band_scalar_score": float(band.scalar_score),
        f"{split_prefix}_alert_band_min_precision": float(band.min_precision),
        f"{split_prefix}_alert_band_mean_precision": float(band.mean_precision),
    }
    for pt in band.points:
        slug = _rate_slug(pt.target_alerts_per_hour)
        out[f"{split_prefix}_op_precision_at_{slug}_alerts_per_hour"] = float(pt.precision)
        out[f"{split_prefix}_op_recall_at_{slug}_alerts_per_hour"] = float(pt.recall)
        out[f"{split_prefix}_op_alerts_at_{slug}_alerts_per_hour"] = int(pt.alerts)
        out[f"{split_prefix}_op_true_positives_at_{slug}_alerts_per_hour"] = int(pt.true_positives)
        out[f"{split_prefix}_op_threshold_at_{slug}_alerts_per_hour"] = float(pt.threshold)
        if pt.alerts_per_hour is not None:
            out[f"{split_prefix}_op_actual_alerts_per_hour_at_{slug}"] = float(pt.alerts_per_hour)
    return out


def operational_threshold_picks_for_targets(
    candidates: pd.DataFrame,
    *,
    window_hours: float | None,
    target_alerts_per_hour: tuple[float, ...] = DEFAULT_TARGET_ALERTS_PER_HOUR,
    split_prefix: str = "val",
    name_prefix: str = "op_band",
) -> list[tuple[str, float]]:
    """Return ``(pick_name, threshold)`` pairs using the shared capacity search."""

    picks: list[tuple[str, float]] = []
    for rate in target_alerts_per_hour:
        k = target_alert_count(window_hours, float(rate))
        pt = threshold_for_target_operational_alerts(
            candidates,
            k,
            window_hours=window_hours,
            requested_alerts_per_hour=float(rate),
            split_prefix=split_prefix,
        )
        if math.isfinite(pt.threshold):
            picks.append((f"{name_prefix}_{float(rate):g}hr", float(pt.threshold)))
    return picks
