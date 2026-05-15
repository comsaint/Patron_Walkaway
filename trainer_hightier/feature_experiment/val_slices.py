"""Validation ``gaming_day`` slicing for coarse robustness summaries (median / P25)."""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score


def contiguous_val_day_masks(val_gaming_day: Sequence[object], *, k: int = 4) -> list[np.ndarray]:
    """Partition rows into ``k_eff`` contiguous **calendar-day** buckets (sorted unique).

    ``k_eff = min(k, n_unique_days)`` then clamp so ``k_eff >= 2`` when at least two
    distinct normalized days exist. If fewer than two distinct calendar days → a
    single all-``True`` mask (caller treats P25 as not applicable).
    """

    n_all = len(val_gaming_day)
    if n_all == 0:
        return []
    series = pd.to_datetime(pd.Series(val_gaming_day), errors="coerce")
    normed = series.dt.normalize()
    uniq = pd.Series(pd.unique(normed.dropna())).sort_values()
    uniq_np = uniq.to_numpy(dtype="datetime64[ns]")
    n_u = int(len(uniq_np))
    if n_u <= 1:
        return [np.ones(n_all, dtype=bool)]
    k_eff = min(int(k), n_u)
    if k_eff < 2:
        k_eff = 2 if n_u >= 2 else k_eff
    edges = np.array_split(np.arange(n_u), k_eff)
    dn = normed.to_numpy(dtype="datetime64[ns]")
    masks: list[np.ndarray] = []
    for idx_block in edges:
        day_bucket = uniq_np[idx_block]
        masks.append(np.isin(dn, day_bucket))
    return masks


def per_slice_average_precision(y_true: np.ndarray, scores: np.ndarray, masks: list[np.ndarray]) -> list[float]:
    """Average precision within each mask; NaN slices when undefined."""

    yy = np.asarray(y_true).reshape(-1)
    ss = np.asarray(scores, dtype=float).reshape(-1)
    out: list[float] = []
    for m in masks:
        mm = np.asarray(m, dtype=bool).reshape(-1)
        yt = yy[mm]
        sc = ss[mm]
        pos = int(np.sum(yt == 1))
        neg = int(np.sum(yt == 0))
        if pos < 1 or neg < 1 or sc.size == 0 or not np.isfinite(sc).all():
            out.append(float("nan"))
        else:
            out.append(float(average_precision_score(yt, sc)))
    return out


def per_slice_recall_at_threshold(
    y_true: np.ndarray,
    scores: np.ndarray,
    threshold: float,
    masks: list[np.ndarray],
) -> list[float]:
    """Recall when ``scores >= threshold`` within each boolean mask.

    Returns NaN for slices with no positive labels or empty / non-finite scores.
    """

    yy = np.asarray(y_true).reshape(-1)
    ss = np.asarray(scores, dtype=float).reshape(-1)
    thr = float(threshold)
    out: list[float] = []
    for m in masks:
        mm = np.asarray(m, dtype=bool).reshape(-1)
        yt = yy[mm]
        sc = ss[mm]
        pos = int(np.sum(yt == 1))
        if pos < 1 or sc.size == 0 or not np.isfinite(sc).all():
            out.append(float("nan"))
            continue
        pred = sc >= thr
        tp = int(np.sum(pred & (yt == 1)))
        out.append(float(tp) / float(pos))
    return out


def median_p25(vals: Sequence[float]) -> tuple[float | None, float | None]:
    """Return (median, p25) ignoring NaNs; empty → (None, None)."""

    raw = np.asarray(list(vals), dtype=float)
    a = raw[np.isfinite(raw)]
    if a.size == 0:
        return None, None
    return float(np.median(a)), float(np.percentile(a, 25))
