"""Shared helpers for A4 two-stage scoring.

The A4 MVP uses a fixed fusion rule:

    final_score = stage1_score * stage2_score

Stage-2 is only evaluated on a Stage-1 candidate pool to keep latency and memory
bounded on laptop-class hardware.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

A4_FUSION_MODE_PRODUCT = "product"


def validate_fusion_mode(raw_mode: Optional[str]) -> str:
    """Return normalized fusion mode; fallback to ``product`` when invalid."""
    mode = str(raw_mode or A4_FUSION_MODE_PRODUCT).strip().lower()
    if mode != A4_FUSION_MODE_PRODUCT:
        return A4_FUSION_MODE_PRODUCT
    return mode


def candidate_cutoff_from_threshold(
    stage1_threshold: float,
    candidate_multiplier: float,
) -> float:
    """Map Stage-1 operating threshold to a Stage-2 candidate cutoff."""
    thr = float(stage1_threshold)
    mult = float(candidate_multiplier)
    if not np.isfinite(thr):
        thr = 0.5
    if not np.isfinite(mult):
        mult = 1.0
    thr = min(0.99, max(0.01, thr))
    mult = min(2.0, max(0.1, mult))
    return float(min(0.99, max(0.01, thr * mult)))


def candidate_mask_from_scores(
    stage1_scores: np.ndarray,
    *,
    cutoff: float,
) -> np.ndarray:
    """Return candidate mask where Stage-1 score >= cutoff."""
    arr = np.asarray(stage1_scores, dtype=np.float64).reshape(-1)
    return arr >= float(cutoff)


def fuse_product_scores(
    stage1_scores: np.ndarray,
    stage2_scores: np.ndarray,
) -> np.ndarray:
    """Return product-fused probabilities with clipping to [0, 1]."""
    s1 = np.asarray(stage1_scores, dtype=np.float64).reshape(-1)
    s2 = np.asarray(stage2_scores, dtype=np.float64).reshape(-1)
    n = min(len(s1), len(s2))
    if n == 0:
        return np.asarray([], dtype=np.float64)
    out = s1[:n] * s2[:n]
    return np.clip(out, 0.0, 1.0)

