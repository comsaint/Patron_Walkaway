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

# Which segment's booster is also written at the top-level ``rated_art`` keys
# (``model`` / ``threshold`` / ``metrics``). The other segment lives only under
# ``high_roller_segmentation`` with an ``artifact`` sub-tree.
# - ``low_value_model``: complement of the upper-tail split (below cutoff).
# - ``tail``: upper tail at ``HIGH_ROLLER_QUANTILE`` (key is ``p{N}_model``, e.g. ``p10_model`` when q=0.90).
HIGH_ROLLER_ROOT_SEGMENT_FOR_SERVING: Literal["low_value_model", "tail"] = "low_value_model"
