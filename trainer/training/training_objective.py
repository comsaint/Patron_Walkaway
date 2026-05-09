"""Primary-fit vs Issue #8 objective helpers (L2 single path, GitHub #16).

Call sites should use these helpers so bakeoff / segmentation policy does not
implicitly depend on bundle cache hits or parquet entry mode.
"""

from __future__ import annotations


def primary_rated_gbm_bakeoff_enabled(pipeline_gbm_bakeoff: bool) -> bool:
    """Return whether the primary rated GBM path may run the multi-backend bakeoff."""
    return bool(pipeline_gbm_bakeoff)


def issue8_segment_fits_use_gbm_bakeoff() -> bool:
    """Issue #8 segment fits are LightGBM-only (no bakeoff), regardless of pipeline flag."""
    return False
