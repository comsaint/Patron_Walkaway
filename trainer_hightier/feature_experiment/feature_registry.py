"""Constants for Wave-1 offline feature experimentation (trainer_hightier).

Baseline columns match :data:`trainer_hightier.05_lgbm_train.MODEL_FEATURE_COLUMNS`.
Experimental columns are prefixed with ``fe__`` and materialized by DuckDB windows
aligned with trial 1h semantics (``player_id``, ``payout_complete_dtm`` clocks).
"""

from __future__ import annotations

import importlib

_m = importlib.import_module("trainer_hightier.05_lgbm_train")

MODEL_FEATURE_COLUMNS: tuple[str, ...] = tuple(_m.MODEL_FEATURE_COLUMNS)

FEATURE_GROUP_TAGS: dict[str, tuple[str, ...]] = {
    "existing_trial_1h": (
        "bet__bets_cnt__w1h",
        "bet__wager_sum__w1h",
        "bet__back_bet_ratio__w1h",
        "bet__payout_odds_avg__w1h",
    ),
    "existing_slow_patron_180d_m1snap": (
        "patron__theo_win_sum__w180d_m1snap",
        "patron__gaming_days_cnt__w180d_m1snap",
        "patron__adt__w180d_m1snap",
    ),
    "group_a_velocity_ratios": (
        "fe__wager_sum__w15m",
        "fe__wager_sum__w1d",
        "fe__bets_cnt__w15m",
        "fe__bets_cnt__w1d",
        "fe__wager_sum__w15m_over_w1d",
        "fe__bets_cnt__w15m_over_w1d",
    ),
    "group_b_rfm": (
        "fe__time_since_last_bet_sec",
        "fe__bets_cnt__w7d",
        "fe__bets_cnt__w30d",
        "fe__wager_sum__w7d",
        "fe__wager_sum__w30d",
        "fe__wager_sum__w7d_over_w30d",
        "fe__bets_density_proxy_w30d",
    ),
    "group_c_burstiness": ("fe__interarrival_mean_sec_w7d", "fe__wager_cv_w7d"),
    "group_d_personal_z": ("fe__wager_z_prior_w30d", "fe__payout_odds_z_prior_w30d"),
}

EXPERIMENTAL_NUMERIC_COLUMNS: tuple[str, ...] = tuple(
    col
    for _g, cols in FEATURE_GROUP_TAGS.items()
    if _g.startswith("group_")
    for col in cols
)

FULL_CANDIDATE_FEATURE_COLUMNS: tuple[str, ...] = tuple(dict.fromkeys(MODEL_FEATURE_COLUMNS + EXPERIMENTAL_NUMERIC_COLUMNS))
