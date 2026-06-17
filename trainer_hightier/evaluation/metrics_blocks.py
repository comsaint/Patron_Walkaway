"""Shared evaluation metric blocks for Step 5 training and offline backtest."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score


def metrics_at_threshold(
    y_true: np.ndarray,
    scores: np.ndarray,
    threshold: float,
) -> tuple[float, float, float, int]:
    """Return precision, recall, f1, alert_count for ``scores >= threshold``."""

    y = np.asarray(y_true, dtype=np.int8).reshape(-1)
    s = np.asarray(scores, dtype=np.float64).reshape(-1)
    if not math.isfinite(float(threshold)):
        return 0.0, 0.0, 0.0, 0
    pred = (s >= float(threshold)).astype(np.int8)
    tp = int(np.sum((pred == 1) & (y == 1)))
    fp = int(np.sum((pred == 1) & (y == 0)))
    fn = int(np.sum((pred == 0) & (y == 1)))
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    alerts = int(np.sum(pred == 1))
    return float(prec), float(rec), float(f1), alerts


def split_metrics_block(
    split: str,
    y_true: np.ndarray,
    scores: np.ndarray,
    threshold: float,
    *,
    window_hours: float | None,
) -> dict[str, Any]:
    """Build flat metrics keys aligned with high-tier trainer density naming."""

    y = np.asarray(y_true, dtype=np.int8).reshape(-1)
    s = np.asarray(scores, dtype=np.float64).reshape(-1)
    n = int(len(y))
    n_pos = int(np.sum(y == 1))
    n_neg = int(np.sum(y == 0))
    has_both = n_pos >= 1 and n_neg >= 1 and np.isfinite(s).all()
    ap = float(average_precision_score(y, s)) if has_both else 0.0
    prec, rec, f1, alerts = metrics_at_threshold(y, s, threshold)
    out: dict[str, Any] = {
        f"{split}_ap": ap,
        f"{split}_precision": prec,
        f"{split}_recall": rec,
        f"{split}_f1": f1,
        f"{split}_samples": n,
        f"{split}_positives": n_pos,
        f"{split}_alerts": alerts,
        f"{split}_window_hours": float(window_hours) if window_hours is not None else None,
        f"{split}_alerts_per_hour": None,
        f"{split}_true_labels_per_hour": None,
    }
    if window_hours is not None and math.isfinite(float(window_hours)) and float(window_hours) > 0:
        wh = float(window_hours)
        out[f"{split}_alerts_per_hour"] = float(alerts) / wh
        out[f"{split}_true_labels_per_hour"] = float(n_pos) / wh
    return out
