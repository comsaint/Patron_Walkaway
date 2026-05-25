"""Production Feast online constants for scorer v2 (no ``feature_experiment`` import)."""

from __future__ import annotations

from typing import Final

FEAST_MID_ANCHOR_COLUMN: Final[str] = "anchor_gaming_day"

PRODUCTION_MID_TERM_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "fe__bets_cnt__w1d",
    "fe__wager_sum__w1d",
    "fe__bets_cnt__w7d",
    "fe__wager_sum__w7d",
    "fe__bets_cnt__w30d",
    "fe__wager_sum__w30d",
    "fe__prior_wager_mean_w30d",
    "fe__prior_wager_std_w30d",
    "fe__prior_odds_mean_w30d",
    "fe__prior_odds_std_w30d",
    "fe__std_wager_w7d",
    "fe__avg_abs_wager_w7d",
    "fe__interarrival_avg_w7d",
    "fe__interarrival_std_w7d",
    "fe__max_pcd_w7d",
    "fe__min_pcd_w7d",
    "fe__payout_odds_avg_w7d",
    "fe__payout_odds_std_w7d",
)

PRODUCTION_LONG_TERM_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "patron__theo_win_sum__w180d_m1snap",
    "patron__gaming_days_cnt__w180d_m1snap",
    "patron__adt__w180d_m1snap",
)

MID_TERM_FEATURE_VIEW_NAME: Final[str] = "mid_term_daily_spike_features"
MID_TERM_FEATURE_SERVICE_NAME: Final[str] = "walkaway_canonical_mid_term_spike_v1"
LONG_TERM_FEATURE_VIEW_NAME: Final[str] = "long_term_slow_spike_features"
LONG_TERM_FEATURE_SERVICE_NAME: Final[str] = "walkaway_canonical_long_term_spike_v1"

MID_TERM_ONLINE_FEATURE_REFS: Final[tuple[str, ...]] = tuple(
    f"{MID_TERM_FEATURE_VIEW_NAME}:{c}"
    for c in (FEAST_MID_ANCHOR_COLUMN, *PRODUCTION_MID_TERM_FEATURE_COLUMNS)
)
LONG_TERM_ONLINE_FEATURE_REFS: Final[tuple[str, ...]] = tuple(
    f"{LONG_TERM_FEATURE_VIEW_NAME}:{c}" for c in PRODUCTION_LONG_TERM_FEATURE_COLUMNS
)

# Backward-compatible aliases used across serving modules.
SPIKE_MID_TERM_FEATURE_COLUMNS = PRODUCTION_MID_TERM_FEATURE_COLUMNS
SPIKE_LONG_TERM_FEATURE_COLUMNS = PRODUCTION_LONG_TERM_FEATURE_COLUMNS
MID_SPIKE_FEATURE_VIEW_NAME = MID_TERM_FEATURE_VIEW_NAME
LONG_SPIKE_FEATURE_VIEW_NAME = LONG_TERM_FEATURE_VIEW_NAME
MID_SPIKE_FEATURE_SERVICE_NAME = MID_TERM_FEATURE_SERVICE_NAME
MID_SPIKE_ONLINE_FEATURE_REFS = MID_TERM_ONLINE_FEATURE_REFS
LONG_SPIKE_ONLINE_FEATURE_REFS = LONG_TERM_ONLINE_FEATURE_REFS


def feast_entity_rows(canonical_ids: list[str]) -> dict[str, list[str]]:
    """Build ``entity_rows`` for Feast batch ``get_online_features``."""
    if not canonical_ids:
        raise ValueError("canonical_ids must be non-empty")
    return {"canonical_id": [str(x) for x in canonical_ids]}
