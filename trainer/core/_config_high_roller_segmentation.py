"""Issue #8: high-roller segmented training parameters (train-side only).

All values are module-level constants (no environment variables). Toggle
``HIGH_ROLLER_SEGMENT_ENABLE`` for experiments; serving routing is out of scope
for this iteration.
"""

from __future__ import annotations

from typing import Literal

# Master switch: when False, pipeline uses legacy single rated model training.
HIGH_ROLLER_SEGMENT_ENABLE: bool = False

# Profile / matrix column used as total theo proxy (must exist on rated train rows).
HIGH_ROLLER_THEO_FEATURE: str = "theo_win_sum_30d"

# Top (1 - q) fraction by theo is "high" (e.g. 0.90 → top ~10%).
HIGH_ROLLER_QUANTILE: float = 0.90

# Minimum rated train rows per segment; below → fallback to single-model training.
HIGH_ROLLER_MIN_ROWS_HIGH: int = 500
HIGH_ROLLER_MIN_ROWS_LOW: int = 500

# When fallback triggers: train one model on full rated data (same as legacy).
HIGH_ROLLER_FALLBACK_MODE: Literal["single_model"] = "single_model"

# Primary segment keys written to model.pkl for backward-compatible scorer fields.
HIGH_ROLLER_PRIMARY_SEGMENT_FOR_SERVING: Literal["low", "high"] = "low"
