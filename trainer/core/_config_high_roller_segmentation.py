"""Issue #8: high-roller segmented training parameters (train-side only).

All values are module-level constants (no environment variables). Set
``HIGH_ROLLER_SEGMENT_ENABLE`` to False only to opt into legacy single-model L2
training; when True, segmentation failures raise. Serving routing is out of scope
for this iteration.
"""

from __future__ import annotations

from typing import Literal

# Master switch: when False, L2 bundle uses legacy single rated model training only.
# Default True: segmented train is required; failures raise (no silent fallback).
HIGH_ROLLER_SEGMENT_ENABLE: bool = True

# Matrix column used as high/low segmentation proxy on rated rows (historic name: ``theo`` proxy).
# Section A player-run trial uses wager tallies instead of stripped theo fields.
HIGH_ROLLER_THEO_FEATURE: str = "player_run_wager_sum_180d"

# Top (1 - q) fraction by theo is "high" (e.g. 0.90 → top ~10%).
HIGH_ROLLER_QUANTILE: float = 0.90

# Minimum rated train rows per segment; below → fallback to single-model training.
HIGH_ROLLER_MIN_ROWS_HIGH: int = 500
HIGH_ROLLER_MIN_ROWS_LOW: int = 500

# Legacy name only (min-rows / empty-artifact paths now raise instead of falling back).
HIGH_ROLLER_FALLBACK_MODE: Literal["single_model"] = "single_model"

# Primary segment keys written to model.pkl for backward-compatible scorer fields.
HIGH_ROLLER_PRIMARY_SEGMENT_FOR_SERVING: Literal["low", "high"] = "low"
