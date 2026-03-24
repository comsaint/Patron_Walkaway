"""Shared DEC-026 / DEC-032 threshold selection on the precision–recall curve.

Trainer validation, backtester oracle metrics, and the future online calibration
script must use the same constraints:

- ``recall_floor``: require PR-curve recall >= this value (``None`` = skip).
- ``min_alert_count``: minimum number of samples with score >= threshold
  (coerced to at least ``1``).
- ``min_alerts_per_hour`` + ``window_hours``: optional density guard; applied only
  when both are finite and ``window_hours > 0``.

PLAN / DECISION_LOG may refer to ``select_threshold_dec026`` — that is an alias
of :func:`pick_threshold_dec026`.

Selection objective: **maximize precision** over candidates satisfying the mask
(argmax ties break to the first maximum, matching numpy).
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from sklearn.metrics import precision_recall_curve

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Dec026ThresholdPick:
    """Result of :func:`pick_threshold_dec026`."""

    threshold: float
    precision: float
    recall: float
    fbeta: float
    f1: float
    is_fallback: bool


def dec026_sanitize_per_hour_params(
    window_hours: Optional[float],
    min_alerts_per_hour: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    """Return (window_hours, min_alerts_per_hour) safe for per-hour guard.

    Non-finite or non-numeric values disable the per-hour constraint (with log).
    """
    wh_out: Optional[float] = window_hours
    mah_out: Optional[float] = min_alerts_per_hour

    if window_hours is not None:
        try:
            wf = float(window_hours)
        except (TypeError, ValueError):
            logger.warning(
                "pick_threshold_dec026: window_hours not numeric (%r); skipping per-hour guard",
                window_hours,
            )
            wh_out = None
        else:
            if not math.isfinite(wf):
                logger.warning(
                    "pick_threshold_dec026: window_hours non-finite (%r); skipping per-hour guard",
                    window_hours,
                )
                wh_out = None
            elif wf <= 0.0:
                wh_out = None

    if min_alerts_per_hour is not None:
        try:
            mf = float(min_alerts_per_hour)
        except (TypeError, ValueError):
            logger.warning(
                "pick_threshold_dec026: min_alerts_per_hour not numeric (%r); skipping per-hour guard",
                min_alerts_per_hour,
            )
            mah_out = None
        else:
            if not math.isfinite(mf):
                logger.warning(
                    "pick_threshold_dec026: min_alerts_per_hour non-finite (%r); skipping per-hour guard",
                    min_alerts_per_hour,
                )
                mah_out = None

    return wh_out, mah_out


def dec026_pr_alert_arrays(
    y_true: np.ndarray,
    y_score: np.ndarray,
) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]]:
    """Compute PR-curve slices and alert counts for DEC-026 (single sklearn call).

    Returns ``(pr_p, pr_r, pr_thresholds, alert_counts, n)`` or ``None`` if
    inputs are invalid or single-class (caller should use fallback / None metrics).
    """
    y_t = np.asarray(y_true, dtype=float)
    y_s = np.asarray(y_score, dtype=float)
    n = int(y_t.shape[0])
    if n == 0 or y_s.shape[0] != n:
        return None
    if np.any(np.isnan(y_t)) or np.any(np.isnan(y_s)):
        return None
    if not np.all((y_t == 0.0) | (y_t == 1.0)):
        return None

    n_pos = int(np.sum(y_t == 1))
    n_neg = int(np.sum(y_t == 0))
    if n_pos == 0 or n_neg == 0:
        return None

    pr_prec, pr_rec, pr_thresholds = precision_recall_curve(y_t, y_s)
    pr_p = pr_prec[:-1]
    pr_r = pr_rec[:-1]
    _sorted_scores = np.sort(y_s)
    alert_counts = n - np.searchsorted(_sorted_scores, pr_thresholds, side="left")
    return pr_p, pr_r, pr_thresholds, alert_counts, n


def pick_threshold_dec026_from_pr_arrays(
    pr_p: np.ndarray,
    pr_r: np.ndarray,
    pr_thresholds: np.ndarray,
    alert_counts: np.ndarray,
    *,
    recall_floor: Optional[float],
    min_alert_count: int,
    min_alerts_per_hour: Optional[float] = None,
    window_hours: Optional[float] = None,
    fbeta_beta: float = 0.5,
) -> Dec026ThresholdPick:
    """Run DEC-026 mask + argmax precision given precomputed PR / alert arrays.

    ``window_hours`` / ``min_alerts_per_hour`` should already be sanitized via
    :func:`dec026_sanitize_per_hour_params` when reusing across multiple recall
    floors (avoids duplicate warnings).
    """
    min_ac = max(1, int(min_alert_count))

    valid = alert_counts >= min_ac
    if recall_floor is not None:
        valid = valid & (pr_r >= float(recall_floor))

    if min_alerts_per_hour is not None and window_hours is not None:
        wh = float(window_hours)
        if wh > 0.0:
            aph = alert_counts.astype(np.float64) / wh
            valid = valid & (aph >= float(min_alerts_per_hour))

    if not np.any(valid):
        return Dec026ThresholdPick(0.5, 0.0, 0.0, 0.0, 0.0, True)

    prec_arr = np.where(valid, pr_p, -1.0)
    best_idx = int(np.argmax(prec_arr))

    best_t = float(pr_thresholds[best_idx])
    best_prec = float(pr_p[best_idx])
    best_rec = float(pr_r[best_idx])

    b = float(fbeta_beta)
    denom = b * b * pr_p + pr_r
    with np.errstate(divide="ignore", invalid="ignore"):
        fbeta_arr = np.where(denom > 0, (1.0 + b * b) * pr_p * pr_r / denom, 0.0)
    best_fbeta = float(fbeta_arr[best_idx])
    best_f1 = (
        2.0 * best_prec * best_rec / (best_prec + best_rec)
        if (best_prec + best_rec) > 0
        else 0.0
    )

    return Dec026ThresholdPick(
        threshold=best_t,
        precision=best_prec,
        recall=best_rec,
        fbeta=best_fbeta,
        f1=best_f1,
        is_fallback=False,
    )


def pick_threshold_dec026(
    y_true: np.ndarray,
    y_score: np.ndarray,
    *,
    recall_floor: Optional[float],
    min_alert_count: int,
    min_alerts_per_hour: Optional[float] = None,
    window_hours: Optional[float] = None,
    fbeta_beta: float = 0.5,
) -> Dec026ThresholdPick:
    """Pick threshold by maximizing precision subject to DEC-026 / DEC-032 guards.

    If no candidate satisfies the constraints, returns the historical fallback
    ``threshold=0.5`` with zeroed metrics and ``is_fallback=True`` (trainer parity).

    Parameters
    ----------
    y_true, y_score
        Strict binary labels (0 / 1 only) and scores; same length. NaNs or
        non-binary labels → fallback (no sklearn multiclass error).
    min_alert_count
        Coerced to at least ``1``.
    """
    prep = dec026_pr_alert_arrays(y_true, y_score)
    if prep is None:
        return Dec026ThresholdPick(0.5, 0.0, 0.0, 0.0, 0.0, True)

    pr_p, pr_r, pr_thresholds, alert_counts, _n = prep
    min_ac = max(1, int(min_alert_count))
    wh_eff, mah_eff = dec026_sanitize_per_hour_params(window_hours, min_alerts_per_hour)
    return pick_threshold_dec026_from_pr_arrays(
        pr_p,
        pr_r,
        pr_thresholds,
        alert_counts,
        recall_floor=recall_floor,
        min_alert_count=min_ac,
        min_alerts_per_hour=mah_eff,
        window_hours=wh_eff,
        fbeta_beta=fbeta_beta,
    )


# DEC-032 / PLAN naming alignment
select_threshold_dec026 = pick_threshold_dec026

# trainer vs walkaway_ml 安裝名共用同一模組實例，避免 sys.modules 出現兩份 threshold_selection 致使 unittest.mock.patch 顯得不生效。
_ts_mod = sys.modules[__name__]
sys.modules["trainer.training.threshold_selection"] = _ts_mod
sys.modules["walkaway_ml.training.threshold_selection"] = _ts_mod
