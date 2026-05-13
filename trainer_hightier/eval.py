"""Precision-floor thresholding on a high-tier subset (skeleton).

Given binary labels ``y_true`` and scores ``y_score`` on the **same** rows
(segment already applied by caller), choose a score threshold ``T`` such that
alerts are ``{ i : y_score[i] >= T }``. Among thresholds with
``precision >= min_precision``, pick the **largest** ``T`` (fewest alerts) and
report ``alert_rate = |alerts| / n``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PrecisionFloorReport:
    """Outcome of :func:`report_alert_rate_at_precision_floor`."""

    threshold: float
    precision: float
    alert_count: int
    n: int
    alert_rate: float
    feasible: bool


def _validate_binary_labels(y_true: np.ndarray) -> None:
    if y_true.ndim != 1:
        raise ValueError(f"y_true must be 1-D, got shape {y_true.shape}")
    if y_true.size == 0:
        raise ValueError("y_true must be non-empty")
    uniq = np.unique(y_true)
    if not np.isin(uniq, [0, 1]).all():
        raise ValueError(
            f"y_true must be binary {{0,1}}; got unique values {uniq.tolist()}"
        )


def _validate_scores(y_score: np.ndarray, n: int) -> None:
    if y_score.ndim != 1:
        raise ValueError(f"y_score must be 1-D, got shape {y_score.shape}")
    if y_score.shape[0] != n:
        raise ValueError(
            f"y_score length {y_score.shape[0]} != y_true length {n}"
        )
    if not np.isfinite(y_score.astype(float, copy=False)).all():
        raise ValueError("y_score must be finite (no NaN/inf)")


def report_alert_rate_at_precision_floor(
    y_true: np.ndarray,
    y_score: np.ndarray,
    min_precision: float,
) -> PrecisionFloorReport:
    """Return alert rate for the **highest** threshold meeting ``min_precision``.

    Tie semantics: among score levels ``T`` in ``unique(y_score)`` with
    ``precision(y_score >= T) >= min_precision``, choose the **maximum** ``T``
    (smallest alert set). If none qualify, ``feasible`` is ``False`` and the
    returned threshold is ``nan``.

    Args:
        y_true: Binary labels ``0/1`` (positive = condition counted in precision).
        y_score: Higher score means more likely to alert.
        min_precision: Required precision in ``(0, 1]``.

    Returns:
        :class:`PrecisionFloorReport`.
    """
    y_true = np.asarray(y_true).astype(np.int8, copy=False)
    y_score = np.asarray(y_score, dtype=np.float64)
    _validate_binary_labels(y_true)
    n = int(y_true.shape[0])
    _validate_scores(y_score, n)
    mp = float(min_precision)
    if not (0.0 < mp <= 1.0) or not np.isfinite(mp):
        raise ValueError(f"min_precision must be finite in (0,1], got {min_precision!r}")

    uniq = np.sort(np.unique(y_score))[::-1]
    best_thr: float | None = None
    best_prec = 0.0
    best_k = 0

    for thr in uniq:
        mask = y_score >= float(thr)
        k = int(mask.sum())
        if k == 0:
            continue
        prec = float(y_true[mask].mean())
        if prec >= mp:
            if best_thr is None or float(thr) > best_thr:
                best_thr = float(thr)
                best_prec = prec
                best_k = k

    if best_thr is None:
        return PrecisionFloorReport(
            threshold=float("nan"),
            precision=float("nan"),
            alert_count=0,
            n=n,
            alert_rate=0.0,
            feasible=False,
        )

    return PrecisionFloorReport(
        threshold=best_thr,
        precision=best_prec,
        alert_count=best_k,
        n=n,
        alert_rate=float(best_k) / float(n),
        feasible=True,
    )
