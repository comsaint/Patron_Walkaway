"""trainer/features.py
======================
Shared feature engineering — Train-Serve Parity core (TRN-05/07/08).

Architecture (DEC-022: Track Profile / Track LLM / Track Human)
--------------------------------------------------------------
**Track B — Vectorized hand-crafted features** (state-machine logic)
    compute_loss_streak()       LOSE→+1, WIN→reset, PUSH→conditional (F4)
    compute_run_boundary()      Gap ≥ RUN_BREAK_MIN → new run (B2)
    compute_table_hc()          Unique players per table in rolling window (S1)

**Feature screening** (unified across tracks)
    screen_features()           Mutual-info → correlation pruning → optional LGBM

All Track B functions are imported by BOTH trainer.py and scorer.py to
guarantee train-serve parity.  They must be kept stateless (no global mutable
state) and must only look backward in time from each observation's cutoff.

Sorting convention (G3)
-----------------------
Every Track B function sorts its input by
    (canonical_id | table_id, payout_complete_dtm, bet_id)
with ``kind='stable'`` before processing, matching the scorer's sort order.

H4 (numeric fillna)
-------------------
Numeric columns are filled with 0 before building the EntitySet so that
aggregations are not contaminated by NaN propagation.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd

try:
    from config import (  # type: ignore[import]
        BET_AVAIL_DELAY_MIN,
        LOSS_STREAK_PUSH_RESETS,
        PLACEHOLDER_PLAYER_ID,
        RUN_BREAK_MIN,
        SCREEN_FEATURES_TOP_K,
        TABLE_HC_WINDOW_MIN,
    )
except ModuleNotFoundError:
    from trainer.config import (  # type: ignore[import]
        BET_AVAIL_DELAY_MIN,
        LOSS_STREAK_PUSH_RESETS,
        PLACEHOLDER_PLAYER_ID,
        RUN_BREAK_MIN,
        SCREEN_FEATURES_TOP_K,
        TABLE_HC_WINDOW_MIN,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# player_profile — Phase 1 feature column list (PLAN Step 4 / DEC-011)
# ---------------------------------------------------------------------------

#: Phase 1 profile feature columns sourced from player_profile via
#: t_session aggregations.  These are attached to **rated** bets only via a
#: PIT/as-of join (snapshot_dtm <= bet_time).  Non-rated observations receive
#: 0 for all columns (handled by join_player_profile).
#:
#: Phase 2 additions (wager_mean_180d, wager_p50_180d from t_bet) are not
#: included here.  See doc/player_profile_spec.md §14.
PROFILE_FEATURE_COLS: List[str] = [
    # Recency
    "days_since_last_session",
    "days_since_first_session",
    # Frequency
    "sessions_7d",
    "sessions_30d",
    "sessions_90d",
    "sessions_180d",
    "sessions_365d",
    "active_days_30d",
    "active_days_90d",
    "active_days_365d",
    # Monetary
    "turnover_sum_7d",
    "turnover_sum_30d",
    "turnover_sum_90d",
    "turnover_sum_180d",
    "turnover_sum_365d",
    "player_win_sum_30d",
    "player_win_sum_90d",
    "player_win_sum_180d",
    "player_win_sum_365d",
    "theo_win_sum_30d",
    "theo_win_sum_180d",
    "num_bets_sum_30d",
    "num_bets_sum_180d",
    "num_games_with_wager_sum_30d",
    "num_games_with_wager_sum_180d",
    # Bet intensity
    "turnover_per_bet_mean_30d",
    "turnover_per_bet_mean_180d",
    # Win / Loss & RTP
    "win_session_rate_30d",
    "win_session_rate_180d",
    "actual_rtp_30d",
    "actual_rtp_180d",
    "actual_vs_theo_ratio_30d",
    # Short / Long Ratios
    "turnover_per_bet_30d_over_180d",
    "turnover_30d_over_180d",
    "sessions_30d_over_180d",
    # Session Duration
    "avg_session_duration_min_30d",
    "avg_session_duration_min_180d",
    # Venue Stickiness (30d/90d; 180d excluded per spec §3.1 — venue refits pollute 180d data)
    "distinct_table_cnt_30d",
    "distinct_table_cnt_90d",
    "distinct_pit_cnt_30d",
    "distinct_gaming_area_cnt_30d",
    "top_table_share_30d",
    "top_table_share_90d",
]

# Minimum lookback (days) required to compute each profile feature.
# Recency features (days_since_*) are always computable given ≥1 day of data.
# Ratio features (e.g. 30d_over_180d) require the longer of the two windows.
_PROFILE_FEATURE_MIN_DAYS: dict = {
    # Recency — computable from any non-empty session history
    "days_since_last_session": 1,
    "days_since_first_session": 1,
    # Frequency
    "sessions_7d": 7,
    "sessions_30d": 30,
    "sessions_90d": 90,
    "sessions_180d": 180,
    "sessions_365d": 365,
    "active_days_30d": 30,
    "active_days_90d": 90,
    "active_days_365d": 365,
    # Monetary
    "turnover_sum_7d": 7,
    "turnover_sum_30d": 30,
    "turnover_sum_90d": 90,
    "turnover_sum_180d": 180,
    "turnover_sum_365d": 365,
    "player_win_sum_30d": 30,
    "player_win_sum_90d": 90,
    "player_win_sum_180d": 180,
    "player_win_sum_365d": 365,
    "theo_win_sum_30d": 30,
    "theo_win_sum_180d": 180,
    "num_bets_sum_30d": 30,
    "num_bets_sum_180d": 180,
    "num_games_with_wager_sum_30d": 30,
    "num_games_with_wager_sum_180d": 180,
    # Bet intensity
    "turnover_per_bet_mean_30d": 30,
    "turnover_per_bet_mean_180d": 180,
    # Win / Loss & RTP
    "win_session_rate_30d": 30,
    "win_session_rate_180d": 180,
    "actual_rtp_30d": 30,
    "actual_rtp_180d": 180,
    "actual_vs_theo_ratio_30d": 30,
    # Short / Long Ratios — require the longer window (180d) for both numerator and denominator
    "turnover_per_bet_30d_over_180d": 180,
    "turnover_30d_over_180d": 180,
    "sessions_30d_over_180d": 180,
    # Session Duration
    "avg_session_duration_min_30d": 30,
    "avg_session_duration_min_180d": 180,
    # Venue Stickiness
    "distinct_table_cnt_30d": 30,
    "distinct_table_cnt_90d": 90,
    "distinct_pit_cnt_30d": 30,
    "distinct_gaming_area_cnt_30d": 30,
    "top_table_share_30d": 30,
    "top_table_share_90d": 90,
}

# R122: enforce at import time that _PROFILE_FEATURE_MIN_DAYS stays in sync with
# PROFILE_FEATURE_COLS.  Any missing or extra key means the dynamic-feature-layer
# logic (get_profile_feature_cols) will silently mis-classify features.
assert set(_PROFILE_FEATURE_MIN_DAYS) == set(PROFILE_FEATURE_COLS), (
    "_PROFILE_FEATURE_MIN_DAYS keys do not match PROFILE_FEATURE_COLS — "
    f"missing: {set(PROFILE_FEATURE_COLS) - set(_PROFILE_FEATURE_MIN_DAYS)}, "
    f"extra: {set(_PROFILE_FEATURE_MIN_DAYS) - set(PROFILE_FEATURE_COLS)}"
)


def get_profile_feature_cols(max_lookback_days: int = 365) -> List[str]:
    """Return the subset of PROFILE_FEATURE_COLS computable for *max_lookback_days* of data.

    Used by the DEC-017 Data-Horizon fast-mode: when only N days of session
    history are available, features whose lookback window exceeds N would be
    either identical to shorter-window features (wasteful) or entirely zero
    (misleading).  This function returns only the features whose minimum
    required lookback is ≤ ``max_lookback_days``.

    Parameters
    ----------
    max_lookback_days:
        Number of days of session history available.  Defaults to 365
        (full feature set, equivalent to ``PROFILE_FEATURE_COLS``).

    Returns
    -------
    List of column names, preserving the order of ``PROFILE_FEATURE_COLS``.

    Examples
    --------
    >>> get_profile_feature_cols(30)   # ~30 days of data
    ['days_since_last_session', 'days_since_first_session', 'sessions_7d',
     'sessions_30d', 'active_days_30d', 'turnover_sum_7d', 'turnover_sum_30d', ...]
    >>> get_profile_feature_cols(365)  # full history
    PROFILE_FEATURE_COLS  # all columns
    """
    return [
        col for col in PROFILE_FEATURE_COLS
        if _PROFILE_FEATURE_MIN_DAYS.get(col, 365) <= max_lookback_days
    ]


# ---------------------------------------------------------------------------
# Track B — Vectorized hand-crafted features
# ---------------------------------------------------------------------------

_REQUIRED_STREAK_COLS: frozenset[str] = frozenset(
    {"canonical_id", "bet_id", "payout_complete_dtm", "status"}
)
_REQUIRED_RUN_COLS: frozenset[str] = frozenset(
    {"canonical_id", "bet_id", "payout_complete_dtm"}
)
_REQUIRED_HC_COLS: frozenset[str] = frozenset(
    {"table_id", "bet_id", "payout_complete_dtm", "player_id"}
)


def compute_loss_streak(
    bets_df: pd.DataFrame,
    cutoff_time: Optional[datetime] = None,
) -> pd.Series:
    """Return the running LOSE streak for each bet.

    Streak semantics (SSOT §8.2-B, F4):
    - ``status == 'LOSE'``  → streak += 1
    - ``status == 'WIN'``   → streak resets to 0
    - ``status == 'PUSH'``  → resets if ``LOSS_STREAK_PUSH_RESETS`` else unchanged

    The streak value at row ``i`` is the streak **after** processing bet ``i``
    (inclusive of the current bet's outcome).

    G3: sorted by (canonical_id, payout_complete_dtm, bet_id) before computing.
    TRN-09 / E2: only bets with ``payout_complete_dtm <= cutoff_time`` are
    considered; if ``cutoff_time`` is None all bets in ``bets_df`` are used.

    Parameters
    ----------
    bets_df : DataFrame
        Required columns: canonical_id, bet_id, payout_complete_dtm, status.
    cutoff_time : datetime | None
        If set, bets after this time are excluded from streak computation.
        Must be **tz-naive** (DEC-018 contract); ``payout_complete_dtm`` is
        expected to be tz-naive after ``apply_dq`` normalisation.
        The returned Series preserves the original index — rows beyond the
        cutoff receive NaN (use them for context if needed, but not for
        training labels).

    Returns
    -------
    pd.Series[int]
        Same index as ``bets_df`` (subset if cutoff_time is given).
        Rows beyond cutoff_time are absent from the returned Series.
    """
    missing = _REQUIRED_STREAK_COLS - set(bets_df.columns)
    if missing:
        raise ValueError(f"compute_loss_streak: missing columns {sorted(missing)}")

    df = bets_df.copy()

    # TRN-09 / E2: respect cutoff
    if cutoff_time is not None:
        cutoff_ts = pd.Timestamp(cutoff_time)
        df = df[df["payout_complete_dtm"] <= cutoff_ts].copy()

    if df.empty:
        return pd.Series(dtype="int32")

    # G3: stable sort within each canonical_id
    df = df.sort_values(
        ["canonical_id", "payout_complete_dtm", "bet_id"],
        ascending=True,
        kind="stable",
    )

    # Vectorized streak using cumsum-of-resets approach:
    #   - A "reset" event starts a new group (WIN, or PUSH if LOSS_STREAK_PUSH_RESETS)
    #   - Within each (canonical_id × reset_group), cumsum of is_lose = streak
    #
    # Example:  LOSE PUSH LOSE WIN LOSE  (push_resets=False)
    #   is_reset:  F    F    F    T    F
    #   _reset_grp per cid: 0 0 0 1 1
    #   is_lose:   T    F    T    F    T
    #   cumsum per group: (0) 1 1 2 | (1) 0 1
    #   streak:    1    1    2    0    1   ✓
    df["_is_lose"] = (df["status"] == "LOSE").astype("int8")
    df["_is_reset"] = (
        (df["status"] == "WIN")
        | ((df["status"] == "PUSH") & LOSS_STREAK_PUSH_RESETS)
    ).astype("int8")

    # cumulative reset counter per canonical_id (group boundary)
    df["_reset_grp"] = df.groupby("canonical_id", sort=False)["_is_reset"].cumsum()

    # cumsum of losses within each (canonical_id, reset_group) → streak
    streak = (
        df.groupby(["canonical_id", "_reset_grp"], sort=False)["_is_lose"]
        .cumsum()
        .astype("int32")
    )

    return streak


def compute_run_boundary(
    bets_df: pd.DataFrame,
    cutoff_time: Optional[datetime] = None,
) -> pd.DataFrame:
    """Assign run_id and minutes_since_run_start for each bet.

    A new run starts at the first bet of each canonical_id, and again
    whenever the gap to the previous bet (within the same canonical_id)
    is >= ``RUN_BREAK_MIN`` minutes (B2 correction).

    G3: sorted by (canonical_id, payout_complete_dtm, bet_id) internally.

    Parameters
    ----------
    bets_df : DataFrame
        Required columns: canonical_id, bet_id, payout_complete_dtm.
    cutoff_time : datetime | None
        If set, bets with ``payout_complete_dtm > cutoff_time`` are excluded
        from the result (mirrors the API of ``compute_loss_streak`` —
        TRN-09 / E2 parity).  History bets before the cutoff are still used
        to compute the correct run start so that ``minutes_since_run_start``
        is accurate even for the first observation in a time window.
        Must be **tz-naive** (DEC-018 contract); ``payout_complete_dtm`` is
        expected to be tz-naive after ``apply_dq`` normalisation.

    Returns
    -------
    DataFrame
        Original columns (for bets ≤ cutoff_time) + two new columns:
        ``run_id`` (int, 0-based within each canonical_id) and
        ``minutes_since_run_start`` (float ≥ 0).
        Sorted by (canonical_id, payout_complete_dtm, bet_id).
    """
    missing = _REQUIRED_RUN_COLS - set(bets_df.columns)
    if missing:
        raise ValueError(f"compute_run_boundary: missing columns {sorted(missing)}")

    if bets_df.empty:
        result = bets_df.copy()
        result["run_id"] = pd.array([], dtype="int32")
        result["minutes_since_run_start"] = pd.array([], dtype="float64")
        return result

    # TRN-09 / E2: apply cutoff filter (compute run_id on full set first,
    # then slice — this ensures run starts are anchored to their true first bet
    # even when the caller only wants observations within a window).
    cutoff_ts = pd.Timestamp(cutoff_time) if cutoff_time is not None else None

    # G3 sort
    df = bets_df.sort_values(
        ["canonical_id", "payout_complete_dtm", "bet_id"],
        ascending=True,
        kind="stable",
    ).copy()

    # Gap to previous bet within canonical_id (NaT for the first bet)
    prev_payout = df.groupby("canonical_id", sort=False)["payout_complete_dtm"].shift(1)
    gap_min = (df["payout_complete_dtm"] - prev_payout).dt.total_seconds().div(60)

    # New run: first bet of cid (prev_payout is NaT) OR gap >= RUN_BREAK_MIN
    is_new_run = prev_payout.isna() | (gap_min >= RUN_BREAK_MIN)

    # run_id = cumsum of is_new_run, minus 1 so it starts at 0.
    # Use groupby().cumsum() (transform-style) to avoid multi-index issues
    # that arise with groupby().apply() when there is only one group.
    df["_is_new_run"] = is_new_run.astype("int8")
    df["run_id"] = (
        df.groupby("canonical_id", sort=False)["_is_new_run"]
        .cumsum()
        .sub(1)
        .astype("int32")
    )

    # Run start time: payout_complete_dtm at the first bet of each run,
    # forward-filled within canonical_id so all bets in a run share the same start.
    df["_run_start"] = df["payout_complete_dtm"].where(df["_is_new_run"].astype(bool))
    df["_run_start"] = df.groupby("canonical_id", sort=False)["_run_start"].ffill()

    df["minutes_since_run_start"] = (
        (df["payout_complete_dtm"] - df["_run_start"]).dt.total_seconds().div(60)
    )

    df = df.drop(columns=["_is_new_run", "_run_start"])

    # Apply cutoff_time filter after computing run_id / minutes_since_run_start
    # so that run start times are always anchored to their true first bet.
    if cutoff_ts is not None:
        df = df[df["payout_complete_dtm"] <= cutoff_ts].copy()

    return df


def compute_table_hc(
    bets_df: pd.DataFrame,
    cutoff_time: Optional[datetime],
) -> pd.Series:
    """Return head-count (unique players) at each bet's table in the lookback window.

    For each row with (table_id=T, payout_complete_dtm=t), counts unique
    ``player_id`` values in ``bets_df`` where:
        table_id == T
        AND payout_complete_dtm ∈
            [t - TABLE_HC_WINDOW_MIN - BET_AVAIL_DELAY_MIN,
             t - BET_AVAIL_DELAY_MIN]

    If ``cutoff_time`` is provided, the lookup pool is additionally restricted
    to ``payout_complete_dtm <= cutoff_time - BET_AVAIL_DELAY_MIN``.

    Rows where ``player_id == PLACEHOLDER_PLAYER_ID`` are excluded from the
    unique-player count (they are not real guests — E4/F1).

    Complexity: O(n × mean_window_density) per table group, using numpy
    searchsorted for window bounds (no Python per-row loops over all bets).
    The outer Python loop iterates once per distinct table_id (~700 in prod).

    Parameters
    ----------
    bets_df : DataFrame
        Required columns: table_id, bet_id, payout_complete_dtm, player_id.
    cutoff_time : datetime | None
        Global data availability cutoff.  Pass None for offline training
        where each row's own payout_complete_dtm acts as the cutoff.
        Must be **tz-naive** (DEC-018 contract); ``payout_complete_dtm`` is
        expected to be tz-naive after ``apply_dq`` normalisation.

    Returns
    -------
    pd.Series[int]
        Head count per row, same index as ``bets_df``.
    """
    missing = _REQUIRED_HC_COLS - set(bets_df.columns)
    if missing:
        raise ValueError(f"compute_table_hc: missing columns {sorted(missing)}")

    delay_td = pd.Timedelta(minutes=BET_AVAIL_DELAY_MIN)
    delay_ns = int(BET_AVAIL_DELAY_MIN * 60 * 1e9)
    window_ns = int(TABLE_HC_WINDOW_MIN * 60 * 1e9)

    # Pool: exclude sentinel player_ids and NaN player_ids (R18 — NaN != -1 passes
    # the sentinel check but must not be counted as a real player).
    pool = bets_df[
        (bets_df["player_id"] != PLACEHOLDER_PLAYER_ID)
        & bets_df["player_id"].notna()
    ].copy()
    if cutoff_time is not None:
        avail_limit = pd.Timestamp(cutoff_time) - delay_td
        pool = pool[pool["payout_complete_dtm"] <= avail_limit]

    result = pd.Series(0, index=bets_df.index, dtype="int32")

    for table_id, pool_grp in pool.groupby("table_id", sort=False):
        target_mask = bets_df["table_id"] == table_id
        if not target_mask.any():
            continue

        # Convert to ns-int64 for fast binary search (pool already sorted here)
        pool_times = (
            pool_grp["payout_complete_dtm"]
            .values.astype("datetime64[ns]")
            .astype("int64")
        )
        pool_pids = pool_grp["player_id"].values
        order = np.argsort(pool_times, kind="stable")
        pool_times = pool_times[order]
        pool_pids = pool_pids[order]

        target_idx = bets_df.index[target_mask]
        target_times = (
            bets_df.loc[target_mask, "payout_complete_dtm"]
            .values.astype("datetime64[ns]")
            .astype("int64")
        )

        # Window bounds per target bet
        hi_ends = target_times - delay_ns          # exclusive upper (avail cutoff)
        lo_starts = hi_ends - window_ns            # inclusive lower (window start)

        hi_idxs = np.searchsorted(pool_times, hi_ends, side="right")
        lo_idxs = np.searchsorted(pool_times, lo_starts, side="left")

        # Count unique player_ids per window — numpy-based inner loop
        # (O(window_density) per bet, ~10–50 for typical casino tables)
        counts = np.fromiter(
            (np.unique(pool_pids[lo:hi]).size for lo, hi in zip(lo_idxs, hi_idxs)),
            dtype=np.int32,
            count=len(target_idx),
        )
        result.loc[target_idx] = counts

    return result


# ---------------------------------------------------------------------------
# Feature screening
# ---------------------------------------------------------------------------

# Sentinel used to distinguish "caller did not pass top_k" from "caller passed None".
# When top_k is _SCREEN_TOP_K_UNSET, screen_features() falls back to
# SCREEN_FEATURES_TOP_K from config.py (DEC-020).
_SCREEN_TOP_K_UNSET = object()


def screen_features(
    feature_matrix: pd.DataFrame,
    labels: pd.Series,
    feature_names: List[str],
    corr_threshold: float = 0.95,
    top_k: object = _SCREEN_TOP_K_UNSET,
    use_lgbm: bool = False,
    random_state: int = 42,
) -> List[str]:
    """Two-stage feature screening (SSOT §8.2-D, DEC-020).

    Stage 1 — univariate + redundancy:
        a) Drop near-zero-variance features (std == 0).
        b) Rank by mutual information with ``labels`` (sklearn).
        c) Prune highly correlated pairs (Pearson |r| > corr_threshold),
           keeping the higher-MI feature in each pair.
        d) If ``use_lgbm=False``: apply ``top_k`` cap on MI-sorted survivors.

    Stage 2 — optional LightGBM importance (training-set only):
        If ``use_lgbm=True``, fit a lightweight LightGBM on the Stage-1
        survivors and re-rank by split importance.  Then apply ``top_k`` cap.
        CRITICAL: this stage must only be called on training data, never on
        valid/test, to comply with anti-leakage rules (SSOT §8.2-D / TRN-09).

    Parameters
    ----------
    feature_matrix : DataFrame
        Feature values (rows = observations, columns = feature candidates).
        Should be pre-filtered to ``feature_names`` columns.
    labels : Series
        Binary labels aligned with ``feature_matrix``.
    feature_names : list[str]
        Subset of ``feature_matrix.columns`` to consider.
    corr_threshold : float
        Pearson |r| above which a feature is considered redundant.
    top_k : int | None | (unset)
        Maximum number of features to return.  When not passed by the caller,
        falls back to ``SCREEN_FEATURES_TOP_K`` from config.py (DEC-020).
        ``None`` (either explicitly passed or from config) means no cap —
        all Stage-1 (or Stage-2) survivors are returned.
    use_lgbm : bool
        Enable Stage-2 LightGBM importance screening.
    random_state : int
        Random seed for reproducibility.

    Returns
    -------
    list[str]
        Screened feature names.  Ordered by mutual information descending
        (Stage-1 only) or by LightGBM split importance descending (Stage-2).
    """
    from sklearn.feature_selection import mutual_info_classif  # type: ignore[import]

    # DEC-020: resolve top_k from config when caller did not supply it.
    effective_top_k: Optional[int] = (
        SCREEN_FEATURES_TOP_K if top_k is _SCREEN_TOP_K_UNSET else top_k  # type: ignore[assignment]
    )

    # R905: top_k=0 would silently return an empty feature list, causing a hard-to-debug
    # downstream failure.  Fail early with a clear error instead.
    if effective_top_k is not None and effective_top_k < 1:
        raise ValueError(
            f"screen_features: top_k must be a positive integer or None, got {effective_top_k!r}"
        )

    X = feature_matrix[feature_names].copy()

    # Drop zero-variance columns
    std = X.std()
    nonzero = std[std > 0].index.tolist()
    dropped_zv = len(feature_names) - len(nonzero)
    if dropped_zv:
        logger.info("screen_features: dropped %d zero-variance features", dropped_zv)
    X = X[nonzero]
    if X.empty:
        logger.warning(
            "screen_features: all features are zero-variance/NaN — returning empty list"
        )
        return []

    # Fill NaN for sklearn compatibility — use a separate name (X_safe) to make
    # it clear the fill is for MI / correlation computation only, not permanent.
    X_safe = X.fillna(0)

    # Mutual information (Stage 1b)
    mi = mutual_info_classif(
        X_safe, labels, discrete_features=False, random_state=random_state
    )
    mi_df = pd.Series(mi, index=nonzero).sort_values(ascending=False)
    candidates = mi_df.index.tolist()
    logger.info("screen_features: %d candidates after MI ranking", len(candidates))

    # Correlation pruning (Stage 1c)
    if len(candidates) > 1:
        corr = X_safe[candidates].corr().abs()
        upper = corr.where(np.triu(np.ones(corr.shape, dtype=bool), k=1))
        to_drop = set()
        for col in upper.columns:
            if col in to_drop:
                continue
            # upper[col] gives correlation between EARLIER columns (higher MI, since
            # candidates are MI-sorted descending) and this column.
            # If col is highly correlated with any surviving higher-MI feature, drop col.
            highly_corr = upper.index[upper[col] > corr_threshold].tolist()
            if any(c not in to_drop for c in highly_corr):
                to_drop.add(col)
        candidates = [c for c in candidates if c not in to_drop]
        logger.info(
            "screen_features: %d features after correlation pruning (threshold=%.2f)",
            len(candidates), corr_threshold,
        )

    if not use_lgbm:
        # Stage 1 final: apply top_k cap on MI-sorted list (DEC-020).
        if effective_top_k is not None:
            candidates = candidates[: effective_top_k]
            logger.info("screen_features: capped to top_k=%d (Stage 1)", effective_top_k)
        return candidates

    # Stage 2 — LightGBM importance (TRAINING DATA ONLY — caller responsibility)
    try:
        import lightgbm as lgb  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "lightgbm is required for Stage-2 feature screening.  "
            "Install it with: pip install lightgbm"
        ) from exc

    dtrain = lgb.Dataset(X_safe[candidates], label=labels)
    params = {
        "objective": "binary",
        "verbosity": -1,
        "num_leaves": 31,
        "seed": random_state,
    }
    model = lgb.train(params, dtrain, num_boost_round=100)
    importance = pd.Series(
        model.feature_importance(importance_type="split"), index=candidates
    ).sort_values(ascending=False)

    # Stage 2 final: apply top_k cap on LGBM-ranked list (DEC-020).
    if effective_top_k is not None:
        candidates = importance.head(effective_top_k).index.tolist()
        logger.info("screen_features: %d features after LightGBM screening (top_k=%d)", len(candidates), effective_top_k)
    else:
        candidates = importance.index.tolist()
        logger.info("screen_features: %d features after LightGBM screening (no cap)", len(candidates))

    return candidates


# ---------------------------------------------------------------------------
# player_profile PIT / as-of join (PLAN Step 4 / SSOT §8.2, DEC-011)
# ---------------------------------------------------------------------------

def join_player_profile(
    bets_df: pd.DataFrame,
    profile_df: Optional[pd.DataFrame],
    feature_cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Attach player_profile features to bets via a PIT / as-of join.

    For each bet the most recent profile snapshot satisfying
    ``snapshot_dtm <= payout_complete_dtm`` (by ``canonical_id``) is joined.
    Only **rated** players appear in ``profile_df``; non-rated observations
    receive 0.0 for all profile columns.

    Parameters
    ----------
    bets_df:
        Bet-level DataFrame.  Must contain ``canonical_id`` and
        ``payout_complete_dtm`` (tz-naive HK local time).
    profile_df:
        player_profile snapshot table.  Must contain ``canonical_id``,
        ``snapshot_dtm`` (tz-naive), and any subset of ``feature_cols``.
        Pass ``None`` or an empty DataFrame to skip (all profile columns → 0).
    feature_cols:
        Profile columns to attach.  Defaults to ``PROFILE_FEATURE_COLS``.

    Returns
    -------
    pd.DataFrame
        ``bets_df`` (copy) with profile columns added.  Columns absent from
        ``profile_df`` are zero-filled.  The original row order and index are
        preserved.

    Notes
    -----
    Uses ``pd.merge_asof`` which requires both sides to be sorted by the
    merge key.  A temporary ``_orig_idx`` column restores original order
    afterwards.
    """
    if feature_cols is None:
        feature_cols = PROFILE_FEATURE_COLS

    result = bets_df.copy()

    # R74: initialise profile columns as NaN (not 0.0) so that LightGBM can use its
    # native NaN routing (default-child) to distinguish "no data" from "zero activity".
    for col in feature_cols:
        if col not in result.columns:
            result[col] = np.nan

    if profile_df is None or (isinstance(profile_df, pd.DataFrame) and profile_df.empty):
        logger.info("join_player_profile: profile_df absent/empty — profile features are NaN")
        return result

    available_cols = [c for c in feature_cols if c in profile_df.columns]
    if not available_cols:
        logger.warning(
            "join_player_profile: none of the requested profile columns found in profile_df; "
            "expected: %s",
            feature_cols[:5],
        )
        return result

    # Ensure tz-naive timestamps on both sides (apply_dq strips tz from bets;
    # profile_df may arrive tz-naive or tz-aware depending on data source).
    # R1702: convert to HK before stripping tz (DEC-018).
    bet_time = pd.to_datetime(result["payout_complete_dtm"])
    if bet_time.dt.tz is not None:
        bet_time = bet_time.dt.tz_convert("Asia/Hong_Kong").dt.tz_localize(None)

    snap_time = pd.to_datetime(profile_df["snapshot_dtm"])
    if snap_time.dt.tz is not None:
        snap_time = snap_time.dt.tz_convert("Asia/Hong_Kong").dt.tz_localize(None)

    # Build working copies with a stable integer position tracker.
    # R75: cast canonical_id to str on both sides — ClickHouse may return Int64
    # while identity mapping uses str, causing merge_asof to silently produce all NaN.
    bets_work = result[["canonical_id", "payout_complete_dtm"]].copy()
    bets_work["canonical_id"] = bets_work["canonical_id"].astype(str)
    bets_work["_bet_time"] = bet_time
    bets_work["_orig_idx"] = np.arange(len(bets_work))

    profile_work = profile_df[["canonical_id", "snapshot_dtm"] + available_cols].copy()
    profile_work["canonical_id"] = profile_work["canonical_id"].astype(str)
    profile_work = profile_work.assign(snapshot_dtm=snap_time)

    # merge_asof requires the same dtype on both time keys (e.g. [ms] vs [us] raises MergeError).
    bets_work["_bet_time"] = pd.to_datetime(bets_work["_bet_time"], utc=False).astype("datetime64[ns]")
    profile_work["snapshot_dtm"] = pd.to_datetime(profile_work["snapshot_dtm"], utc=False).astype("datetime64[ns]")

    # merge_asof requires the left key to be monotonically increasing and NaT-free.
    # apply_dq already filters payout_complete_dtm.notna(), so NaT should not appear;
    # the dropna here is a defensive guard that keeps NaT rows in `result` with their
    # NaN-initialised profile features rather than breaking the entire merge.
    _n_nat = int(bets_work["_bet_time"].isna().sum())
    if _n_nat:
        logger.warning(
            "join_player_profile: %d bet(s) with null payout_complete_dtm skipped; "
            "profile features will be NaN for those rows.",
            _n_nat,
        )
    bets_valid = bets_work.dropna(subset=["_bet_time"])

    # merge_asof requires the left_on / right_on keys to be globally sorted;
    # sorting by [canonical_id, time] only guarantees intra-group order.
    bets_sorted = bets_valid.sort_values("_bet_time").reset_index(drop=True)
    profile_sorted = profile_work.sort_values("snapshot_dtm").reset_index(drop=True)

    merged = pd.merge_asof(
        bets_sorted,
        profile_sorted,
        left_on="_bet_time",
        right_on="snapshot_dtm",
        by="canonical_id",
        direction="backward",
    )

    # Restore original row order.
    # R74: keep NaN for unmatched bets (non-rated or before first snapshot) —
    # LightGBM routes NaN to the trained default-child, which is semantically
    # correct; zero-fill would conflate "no data" with "zero activity".
    merged = merged.sort_values("_orig_idx").reset_index(drop=True)

    # R800: when NaT rows were dropped before merge_asof, merged has fewer rows
    # than result.  Use _orig_idx to scatter values back into the correct positions;
    # dropped rows retain their NaN-initialised values (set above).
    for col in available_cols:
        _vals = pd.Series(merged[col].values, index=merged["_orig_idx"].values)
        result[col] = _vals.reindex(np.arange(len(result))).values

    n_rated_with_profile = int(pd.notna(result[available_cols[0]]).sum())
    logger.info(
        "join_player_profile: attached %d profile cols; %d/%d bets have profile snapshot",
        len(available_cols),
        n_rated_with_profile,
        len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Track LLM — DuckDB + Feature Spec YAML (PLAN Step 4B / DEC-023 / DEC-024)
# ---------------------------------------------------------------------------

#: feature_id must be a valid SQL identifier: letter followed by letters/digits/underscores.
_FEATURE_ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

#: Synthetic sort-key column added to the DataFrame before DuckDB queries with
#: RANGE frames.  Encodes G3 (payout_complete_dtm, bet_id) tie-breaking as a
#: nanosecond offset so the single-column ORDER BY required by DuckDB RANGE INTERVAL
#: semantics still produces deterministic, stable results.
_RANGE_SORT_COL = "_range_sort_key"


def load_feature_spec(yaml_path) -> dict:
    """Load and statically validate the Feature Spec YAML (DEC-024).

    Static checks performed:
    - ``feature_id`` values across all tracks are unique.
    - Track LLM ``window_frame`` strings do not contain ``FOLLOWING``
      (prevents look-ahead leakage).
    - Track LLM ``expression`` strings do not contain SQL structural keywords
      (``SELECT``, ``FROM``, ``JOIN``, ``UNION``, ``WITH``).
    - ``derived`` features' ``depends_on`` lists form no circular dependency.

    Parameters
    ----------
    yaml_path:
        Path to the Feature Spec YAML file.  Accepts ``str`` or ``pathlib.Path``.

    Returns
    -------
    dict
        Parsed spec dictionary, unchanged from the YAML structure.

    Raises
    ------
    FileNotFoundError
        If ``yaml_path`` does not exist.
    ValueError
        If any static validation check fails.
    """
    import pathlib
    import yaml as _yaml  # type: ignore[import-untyped]

    path = pathlib.Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"Feature Spec YAML not found: {path}")

    with path.open(encoding="utf-8") as fh:
        spec = _yaml.safe_load(fh)

    _validate_feature_spec(spec)
    return spec


def _validate_feature_spec(spec: dict) -> None:
    """Raise ValueError describing *all* spec violations found (DEC-024 guardrails).

    Guardrail checks:
    - ``feature_id`` must match ``[a-zA-Z][a-zA-Z0-9_]*`` (R2000).
    - ``feature_id`` values must be unique across all tracks.
    - Track LLM ``window_frame`` must not contain ``FOLLOWING``.
    - Track LLM ``expression`` must not contain SQL structural keywords
      (word-boundary check, R2007) or semicolons (R2000).
    - Disallowed keywords are merged from Python defaults and YAML ``guardrails``
      section (R2004).
    - ``derived`` features must not have circular ``depends_on`` (DAG check).
    - ``None`` track sections are treated as empty (R2006).
    """
    errors: List[str] = []
    all_ids: List[str] = []

    # ── R2004: merge Python defaults with YAML guardrails ──────────────────
    yaml_guardrails = spec.get("guardrails") or {}
    yaml_kw_list = yaml_guardrails.get("disallow_sql_keywords_in_expressions") or []
    # R2106: extend blocklist with DDL/DML keywords that can mutate schema or data.
    # R3506: extend blocklist with DuckDB file-access and extension functions to
    # prevent YAML expressions from reading local files or loading untrusted extensions.
    disallowed_sql: set = {
        "SELECT", "FROM", "JOIN", "UNION", "WITH",
        "DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "TRUNCATE",
        "EXEC", "EXECUTE",
        "READ_PARQUET", "READ_CSV", "READ_CSV_AUTO", "READ_JSON",
        "READ_JSON_AUTO", "GLOB", "INSTALL_EXTENSION", "LOAD_EXTENSION",
        "COPY", "EXPORT", "IMPORT",
    } | {kw.upper() for kw in yaml_kw_list}

    for track_key in ("track_llm", "track_human", "track_profile"):
        # R2006: handle ``track_key: null`` in YAML without AttributeError
        track = spec.get(track_key) or {}
        for cand in track.get("candidates", []):
            fid = cand.get("feature_id", "")
            if not fid:
                errors.append(f"[{track_key}] candidate is missing 'feature_id'.")
            else:
                # R2000: feature_id must be a valid SQL identifier
                if not _FEATURE_ID_RE.match(fid):
                    errors.append(
                        f"[{track_key}] feature_id {fid!r} contains invalid characters "
                        f"(only [a-zA-Z][a-zA-Z0-9_]* allowed)."
                    )
                all_ids.append(fid)

            # Track LLM–specific checks
            if track_key == "track_llm":
                expr = cand.get("expression", "")
                wf = cand.get("window_frame", "")

                # No FOLLOWING in window_frame
                if "FOLLOWING" in wf.upper():
                    errors.append(
                        f"[track_llm] '{fid}': window_frame contains FOLLOWING "
                        f"(look-ahead leakage risk): {wf!r}"
                    )

                # R2111: no semicolons in window_frame (multi-statement injection vector)
                if ";" in wf:
                    errors.append(
                        f"[track_llm] '{fid}': window_frame contains semicolon "
                        f"(potential SQL injection): {wf!r}"
                    )

                # R2000: no semicolons in expression (multi-statement injection vector)
                if ";" in expr:
                    errors.append(
                        f"[track_llm] '{fid}': expression contains semicolon "
                        f"(potential SQL injection): {expr!r}"
                    )

                # R2007: word-boundary keyword check (avoids false positives like
                # 'joined_value' matching 'JOIN', or 'PERFORM' matching 'FROM').
                expr_upper = expr.upper()
                forbidden = [
                    kw for kw in disallowed_sql
                    if re.search(r"\b" + re.escape(kw) + r"\b", expr_upper)
                ]
                if forbidden:
                    errors.append(
                        f"[track_llm] '{fid}': expression contains disallowed SQL keyword(s) "
                        f"{forbidden}: {expr!r}"
                    )

    # ── Duplicate feature_id check ──────────────────────────────────────────
    seen: set = set()
    for fid in all_ids:
        if fid in seen:
            errors.append(f"Duplicate feature_id: '{fid}'")
        seen.add(fid)

    # ── Circular depends_on check (Track LLM derived features) ─────────────
    # R2006: use ``or {}`` so a null track_llm section doesn't crash here either.
    llm_track = spec.get("track_llm") or {}
    dep_map: dict = {}
    for cand in llm_track.get("candidates", []):
        if cand.get("type") == "derived":
            fid = cand.get("feature_id", "")
            deps = cand.get("depends_on", []) or []
            dep_map[fid] = list(deps)

    for start in dep_map:
        if _has_cycle(start, dep_map, set()):
            errors.append(f"[track_llm] Circular depends_on detected starting from '{start}'.")

    if errors:
        raise ValueError("Feature Spec validation failed:\n" + "\n".join(f"  • {e}" for e in errors))


def _has_cycle(node: str, dep_map: dict, visiting: set) -> bool:
    """Depth-first cycle detector for depends_on graphs."""
    if node in visiting:
        return True
    if node not in dep_map:
        return False
    visiting = visiting | {node}
    return any(_has_cycle(dep, dep_map, visiting) for dep in dep_map[node])


def _topo_sort_candidates(candidates: list) -> list:
    """Return candidates sorted so every ``depends_on`` predecessor comes before
    the feature that depends on it (R2005).

    Non-derived candidates have no dependencies and float to the front naturally.
    The sort is stable: relative order of independent candidates is preserved.
    """
    by_id = {c["feature_id"]: c for c in candidates}
    visited: set = set()
    result: list = []

    def _visit(fid: str) -> None:
        if fid in visited:
            return
        visited.add(fid)
        cand = by_id.get(fid)
        if cand is not None:
            for dep in (cand.get("depends_on") or []):
                _visit(dep)
            result.append(cand)

    for cand in candidates:
        _visit(cand["feature_id"])

    return result


def compute_track_llm_features(
    bets_df: pd.DataFrame,
    feature_spec: dict,
    cutoff_time: Optional[datetime] = None,
) -> pd.DataFrame:
    """Compute Track LLM features via DuckDB from a Feature Spec YAML definition.

    Only features listed under ``track_llm.candidates`` are computed.  Each
    candidate is translated into a DuckDB window-function ``SELECT`` expression
    and executed against an in-memory table built from ``bets_df``.

    All window frames are strictly backward-looking (``PRECEDING`` / ``CURRENT
    ROW``); ``cutoff_time`` enforces an additional row-level leakage guard.

    Parameters
    ----------
    bets_df:
        Bet-level DataFrame.  Must contain at minimum ``canonical_id``,
        ``payout_complete_dtm``, and ``bet_id``.  Additional columns referenced
        in feature expressions must also be present.
    feature_spec:
        Parsed spec dict as returned by :func:`load_feature_spec`.
    cutoff_time:
        When provided, only rows with ``payout_complete_dtm <= cutoff_time`` are
        used for feature calculation.  Rows after the cutoff are dropped from the
        output (leakage guard, TRN-08).

    Returns
    -------
    pd.DataFrame
        ``bets_df`` (filtered to ``<= cutoff_time`` if specified) with one new
        column per Track LLM candidate appended.  The original row count and
        order are preserved.

    Notes
    -----
    - ``NaN``/``NULL`` handling follows each candidate's ``postprocess.fill``
      strategy (``"zero"`` → 0; ``"ffill"`` → forward-fill; otherwise left NaN).
    - Numeric clipping is applied after fill if ``postprocess.clip`` is present.
    - DuckDB 1.x is required (``RANGE BETWEEN INTERVAL … PRECEDING`` syntax).
    """
    import duckdb

    try:
        from trainer.duckdb_schema import prepare_bets_for_duckdb
    except ModuleNotFoundError:
        from duckdb_schema import prepare_bets_for_duckdb  # type: ignore[import-not-found]

    # R2006: use ``or {}`` so a null track_llm section doesn't raise AttributeError.
    llm_track = feature_spec.get("track_llm") or {}
    candidates = llm_track.get("candidates", [])
    if not candidates:
        logger.warning("compute_track_llm_features: track_llm has no candidates — returning bets_df unchanged.")
        return bets_df.copy()

    # R2005: topological sort — derived features must follow their dependencies.
    candidates = _topo_sort_candidates(candidates)

    # ── Cutoff guard (R2009: restructured to avoid redundant .copy() calls) ─
    if cutoff_time is not None:
        ct = pd.Timestamp(cutoff_time)
        if ct.tzinfo is not None:
            ct = ct.tz_convert("Asia/Hong_Kong").tz_localize(None)
        ts_for_mask = pd.to_datetime(bets_df["payout_complete_dtm"])
        if ts_for_mask.dt.tz is not None:
            ts_for_mask = ts_for_mask.dt.tz_convert("Asia/Hong_Kong").dt.tz_localize(None)
        # R3508: 30-second tolerance prevents clock-skew from silently dropping
        # recently-arrived bets whose payout_complete_dtm is fractionally after
        # the scorer's now_hk.  Window frames are strictly backward-looking so
        # this tolerance does not introduce leakage.
        df = bets_df.loc[ts_for_mask <= ct + pd.Timedelta(seconds=30)].reset_index(drop=True)
    else:
        df = bets_df.copy()

    if df.empty:
        for cand in candidates:
            df[cand["feature_id"]] = pd.Series(dtype="float64")
        return df

    # ── Ensure payout_complete_dtm is tz-naive (DEC-018) ───────────────────
    ts_col = pd.to_datetime(df["payout_complete_dtm"])
    if ts_col.dt.tz is not None:
        ts_col = ts_col.dt.tz_convert("Asia/Hong_Kong").dt.tz_localize(None)
    df["payout_complete_dtm"] = ts_col.astype("datetime64[us]")

    # ── R2002: G3-preserving synthetic sort key for RANGE frames ────────────
    # DuckDB RANGE INTERVAL requires a single ORDER BY column.  We pre-sort by
    # the full G3 key (canonical_id, payout_complete_dtm, bet_id) and then encode
    # the tie-breaking ordinal as a nanosecond offset so the RANGE boundary
    # semantics are unaffected (nanoseconds << minute-level intervals).
    df = df.sort_values(
        ["canonical_id", "payout_complete_dtm", "bet_id"], kind="stable"
    ).reset_index(drop=True)
    df[_RANGE_SORT_COL] = df["payout_complete_dtm"] + pd.to_timedelta(
        df.groupby("canonical_id", sort=False).cumcount(), unit="ns"
    )

    # ── Build DuckDB window SELECT expressions ──────────────────────────────
    # R2008: all passthrough column names are double-quoted to handle spaces and
    # other special characters that would otherwise break the SQL parser.
    fixed_cols = {"canonical_id", "payout_complete_dtm", "bet_id", _RANGE_SORT_COL}
    passthrough_cols = [
        c for c in df.columns
        if c not in fixed_cols
        and not any(c == cand["feature_id"] for cand in candidates)
    ]
    select_exprs = [
        '"canonical_id"',
        '"payout_complete_dtm"',
        '"bet_id"',
    ]
    select_exprs.extend(f'"{c}"' for c in passthrough_cols)

    for cand in candidates:
        fid = cand["feature_id"]
        expr = cand.get("expression", "")
        ftype = cand.get("type", "window")
        wf = cand.get("window_frame", "")

        if ftype in ("window", "transform", "lag"):
            if wf:
                if wf.upper().startswith("RANGE"):
                    # R2002: use synthetic G3 sort key; DuckDB requires single ORDER BY
                    # for RANGE INTERVAL frames, but _RANGE_SORT_COL already encodes
                    # the bet_id tie-breaker as a nanosecond offset.
                    range_order = f'ORDER BY "{_RANGE_SORT_COL}" ASC'
                    sql_expr = (
                        f"{expr} OVER ("
                        f"PARTITION BY canonical_id "
                        f"{range_order} "
                        f"{wf}"
                        f') AS "{fid}"'
                    )
                else:
                    sql_expr = (
                        f"{expr} OVER ("
                        f"PARTITION BY canonical_id "
                        f"ORDER BY payout_complete_dtm ASC, bet_id ASC "
                        f"{wf}"
                        f') AS "{fid}"'
                    )
            else:
                sql_expr = (
                    f"{expr} OVER ("
                    f"PARTITION BY canonical_id "
                    f"ORDER BY payout_complete_dtm ASC, bet_id ASC"
                    f') AS "{fid}"'
                )
        else:
            # "derived": plain scalar expression; relies on lateral column
            # references to window features already computed earlier in SELECT
            # (topological order guaranteed by _topo_sort_candidates above).
            sql_expr = f'({expr}) AS "{fid}"'

        select_exprs.append(sql_expr)

    select_clause = ",\n    ".join(select_exprs)
    sql = (
        f"SELECT\n    {select_clause}\n"
        f"FROM bets\n"
        f"ORDER BY canonical_id, payout_complete_dtm, bet_id"
    )

    # R2003: connection is always closed via finally, even when the query raises.
    # Cast monetary columns to float64 so DuckDB sees DOUBLE and does not infer
    # narrow DECIMAL(9,4)/(10,4), which fails for values like wager 100000 or
    # casino_win -1900000 (schema/schema.txt uses Decimal(19,4)).
    df_for_duckdb = prepare_bets_for_duckdb(df)
    con = duckdb.connect(database=":memory:")
    try:
        con.register("bets", df_for_duckdb)
        result_df = con.execute(sql).df()
    except Exception as exc:  # pragma: no cover
        logger.error("compute_track_llm_features: DuckDB query failed: %s\nSQL:\n%s", exc, sql)
        raise
    finally:
        con.close()

    # Drop the internal synthetic sort key from the output.
    result_df = result_df.drop(columns=[_RANGE_SORT_COL], errors="ignore")

    # ── Postprocess: fill + clip ────────────────────────────────────────────
    for cand in candidates:
        fid = cand["feature_id"]
        pp = cand.get("postprocess", {}) or {}
        fill_spec = pp.get("fill", {}) or {}
        clip_spec = pp.get("clip", {}) or {}

        fill_strategy = fill_spec.get("strategy", "none")
        if fid in result_df.columns:
            if fill_strategy == "zero":
                result_df[fid] = result_df[fid].fillna(0)
            elif fill_strategy == "ffill":
                # R2001: grouped ffill prevents cross-canonical_id data leakage.
                result_df[fid] = (
                    result_df.groupby("canonical_id", sort=False)[fid].ffill()
                )

            if clip_spec:
                lo = clip_spec.get("min")
                hi = clip_spec.get("max")
                result_df[fid] = result_df[fid].clip(lower=lo, upper=hi)

    logger.info(
        "compute_track_llm_features: computed %d Track LLM features for %d bets",
        len(candidates),
        len(result_df),
    )
    return result_df
