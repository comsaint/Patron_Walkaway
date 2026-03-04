"""trainer/features.py
======================
Shared feature engineering — Train-Serve Parity core (TRN-05/07/08).

Architecture
------------
**Track A — Featuretools DFS** (systematic aggregation exploration)
    build_entity_set()          Build EntitySet: t_bet → t_session → player
    run_dfs_exploration()       Phase-1: DFS on sampled data → feature_defs
    save_feature_defs()         Persist feature definitions (JSON/pickle via ft)
    load_feature_defs()         Load persisted feature definitions
    compute_feature_matrix()    Phase-2: Apply saved defs to full data

**Track B — Vectorized hand-crafted features** (state-machine logic that
    Featuretools cannot express without O(n²) cost)
    compute_loss_streak()       LOSE→+1, WIN→reset, PUSH→conditional (F4)
    compute_run_boundary()      Gap ≥ RUN_BREAK_MIN → new run (B2)
    compute_table_hc()          Unique players per table in rolling window (S1)

**Feature screening** (filters DFS output before training)
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
Featuretools aggregations are not contaminated by NaN propagation.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    import featuretools as ft

try:
    from config import (  # type: ignore[import]
        BET_AVAIL_DELAY_MIN,
        HIST_AVG_BET_CAP,
        LOSS_STREAK_PUSH_RESETS,
        PLACEHOLDER_PLAYER_ID,
        RUN_BREAK_MIN,
        TABLE_HC_WINDOW_MIN,
    )
except ModuleNotFoundError:
    from trainer.config import (  # type: ignore[import]
        BET_AVAIL_DELAY_MIN,
        HIST_AVG_BET_CAP,
        LOSS_STREAK_PUSH_RESETS,
        PLACEHOLDER_PLAYER_ID,
        RUN_BREAK_MIN,
        TABLE_HC_WINDOW_MIN,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# player_profile_daily — Phase 1 feature column list (PLAN Step 4 / DEC-011)
# ---------------------------------------------------------------------------

#: Phase 1 profile feature columns sourced from player_profile_daily via
#: t_session aggregations.  These are attached to **rated** bets only via a
#: PIT/as-of join (snapshot_dtm <= bet_time).  Non-rated observations receive
#: 0 for all columns (handled by join_player_profile_daily).
#:
#: Phase 2 additions (wager_mean_180d, wager_p50_180d from t_bet) are not
#: included here.  See doc/player_profile_daily_spec.md §14.
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

# ---------------------------------------------------------------------------
# Track A — Featuretools EntitySet helpers
# ---------------------------------------------------------------------------

# Featuretools is an optional heavy dependency; import lazily so that the
# Track B functions remain usable in scorer environments where ft may not be
# installed, or in unit tests that only exercise Track B.

def _ft():
    """Lazy import of featuretools — raises ImportError with a clear message."""
    try:
        import featuretools as ft_mod
        return ft_mod
    except ImportError as exc:
        raise ImportError(
            "featuretools is required for Track A features.  "
            "Install it with: pip install featuretools"
        ) from exc


# Numeric columns that receive H4 fillna(0) before EntitySet construction.
_NUMERIC_BET_COLS = ["wager", "payout", "player_win", "num_games_with_wager"]
_NUMERIC_SESSION_COLS = ["turnover", "player_win", "num_games_with_wager"]


def build_entity_set(
    bets_df: pd.DataFrame,
    sessions_df: pd.DataFrame,
    canonical_map: pd.DataFrame,
    session_time_col: str = "session_avail_dtm",
) -> "ft.EntitySet":
    """Build a Featuretools EntitySet with three entities.

    Entity hierarchy (DEC-007):
        t_bet  →  t_session  →  player
        (many-to-one on session_id)  (many-to-one on canonical_id)

    Parameters
    ----------
    bets_df : DataFrame
        t_bet rows with at least ``bet_id``, ``session_id``,
        ``payout_complete_dtm``, and numeric bet-level columns.
        Rows with PLACEHOLDER_PLAYER_ID are expected to have been filtered
        upstream; ``wager`` and other numeric columns receive fillna(0) here
        (H4) before EntitySet construction.
    sessions_df : DataFrame
        t_session rows after FND-01 dedup (one row per session_id).
        Must contain ``session_id``, ``canonical_id``, and a time column
        (default ``session_avail_dtm`` = COALESCE(session_end_dtm, lud_dtm)
        + SESSION_AVAIL_DELAY_MIN; see FND-13).
    canonical_map : DataFrame
        Output of ``identity.build_canonical_mapping*``.
        Columns: [player_id, canonical_id].  Used to build the ``player``
        entity (de-duplicated by canonical_id).
    session_time_col : str
        Column in ``sessions_df`` used as the EntitySet time_index for the
        t_session entity.  Defaults to ``session_avail_dtm``.

    Returns
    -------
    ft.EntitySet
        EntitySet named ``"walkaway"`` ready for DFS or
        ``calculate_feature_matrix``.
    """
    ft_mod = _ft()

    # H4: numeric fillna(0) before EntitySet
    # F2 (SSOT §8.2-E): winsorize wager-like columns before aggregation to
    # prevent extreme outliers from distorting Featuretools sum/mean/max primitives.
    bets = bets_df.copy()
    sessions = sessions_df.copy()
    for col in _NUMERIC_BET_COLS:
        if col in bets.columns:
            bets[col] = bets[col].fillna(0).clip(upper=HIST_AVG_BET_CAP)
    for col in _NUMERIC_SESSION_COLS:
        if col in sessions.columns:
            sessions[col] = sessions[col].fillna(0).clip(upper=HIST_AVG_BET_CAP)

    # Player entity: one row per canonical_id
    players = (
        canonical_map[["canonical_id"]]
        .drop_duplicates(subset=["canonical_id"])
        .copy()
    )

    es = ft_mod.EntitySet(id="walkaway")

    es = es.add_dataframe(
        dataframe_name="t_bet",
        dataframe=bets,
        index="bet_id",
        time_index="payout_complete_dtm",
    )
    es = es.add_dataframe(
        dataframe_name="t_session",
        dataframe=sessions,
        index="session_id",
        time_index=session_time_col,
    )
    es = es.add_dataframe(
        dataframe_name="player",
        dataframe=players,
        index="canonical_id",
    )

    # Relationships (DEC-007)
    es = es.add_relationship("t_session", "session_id", "t_bet", "session_id")
    es = es.add_relationship("player", "canonical_id", "t_session", "canonical_id")

    return es


def run_dfs_exploration(
    es: "ft.EntitySet",
    cutoff_df: pd.DataFrame,
    max_depth: int = 2,
    agg_primitives: Optional[List[str]] = None,
    trans_primitives: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, list]:
    """Phase-1 DFS: explore features on sampled data.

    Parameters
    ----------
    es : ft.EntitySet
        Built by ``build_entity_set``.
    cutoff_df : DataFrame
        Columns: [bet_id, cutoff_time].  Determines the temporal cutoff
        for each observation; Featuretools will not look past cutoff_time.
    max_depth : int
        DFS depth.  depth=2 is recommended for Phase 1 to keep the search
        space manageable.
    agg_primitives : list[str] | None
        Override default aggregation primitives.
    trans_primitives : list[str] | None
        Override default transform primitives.

    Returns
    -------
    (feature_matrix, feature_defs)
        ``feature_matrix`` is indexed by bet_id; ``feature_defs`` is the
        list of FeatureBase objects to pass to ``save_feature_defs``.
    """
    ft_mod = _ft()

    _agg = agg_primitives or [
        "count", "sum", "mean", "max", "min", "trend",
        "num_unique", "time_since_last",
    ]
    _trans = trans_primitives or ["time_since_previous", "cum_sum", "cum_mean"]

    feature_matrix, feature_defs = ft_mod.dfs(
        entityset=es,
        target_dataframe_name="t_bet",
        cutoff_time=cutoff_df,
        agg_primitives=_agg,
        trans_primitives=_trans,
        max_depth=max_depth,
        verbose=False,
    )
    return feature_matrix, feature_defs


def save_feature_defs(feature_defs: list, path: Path) -> None:
    """Persist feature definitions using featuretools serialisation."""
    ft_mod = _ft()
    ft_mod.save_features(feature_defs, str(path))
    logger.info("Saved %d feature definitions to %s", len(feature_defs), path)


def load_feature_defs(path: Path) -> list:
    """Load persisted feature definitions."""
    ft_mod = _ft()
    feature_defs = ft_mod.load_features(str(path))
    logger.info("Loaded %d feature definitions from %s", len(feature_defs), path)
    return feature_defs


def compute_feature_matrix(
    es: "ft.EntitySet",
    saved_feature_defs: list,
    cutoff_df: pd.DataFrame,
) -> pd.DataFrame:
    """Phase-2: apply saved feature definitions to full data.

    Uses ``featuretools.calculate_feature_matrix`` — no re-exploration,
    same feature definitions as Phase-1 (eliminates train-serve parity risk,
    see DEC-002).
    """
    ft_mod = _ft()
    return ft_mod.calculate_feature_matrix(
        features=saved_feature_defs,
        entityset=es,
        cutoff_time=cutoff_df,
        verbose=False,
    )


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

def screen_features(
    feature_matrix: pd.DataFrame,
    labels: pd.Series,
    feature_names: List[str],
    corr_threshold: float = 0.95,
    mi_top_k: Optional[int] = None,
    use_lgbm: bool = False,
    lgbm_top_k: Optional[int] = None,
    random_state: int = 42,
) -> List[str]:
    """Two-stage feature screening (SSOT §8.2-D).

    Stage 1 — univariate + redundancy:
        a) Drop near-zero-variance features (std == 0).
        b) Rank by mutual information with ``labels`` (sklearn).
        c) Prune highly correlated pairs (Pearson |r| > corr_threshold),
           keeping the higher-MI feature in each pair.

    Stage 2 — optional LightGBM importance (training-set only):
        If ``use_lgbm=True``, fit a lightweight LightGBM on ``feature_matrix``
        and keep the top ``lgbm_top_k`` features by split importance.
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
    mi_top_k : int | None
        Keep only the top-k features by mutual information.  None → keep all.
    use_lgbm : bool
        Enable Stage-2 LightGBM importance screening.
    lgbm_top_k : int | None
        Number of features to retain after LightGBM screening.  None → half.
    random_state : int
        Random seed for reproducibility.

    Returns
    -------
    list[str]
        Screened feature names (ordered by mutual information descending).
    """
    from sklearn.feature_selection import mutual_info_classif  # type: ignore[import]

    X = feature_matrix[feature_names].copy()

    # Drop zero-variance columns
    std = X.std()
    nonzero = std[std > 0].index.tolist()
    dropped_zv = len(feature_names) - len(nonzero)
    if dropped_zv:
        logger.info("screen_features: dropped %d zero-variance features", dropped_zv)
    X = X[nonzero]

    # Fill NaN for sklearn compatibility
    X_filled = X.fillna(0)

    # Mutual information (Stage 1b)
    mi = mutual_info_classif(
        X_filled, labels, discrete_features=False, random_state=random_state
    )
    mi_df = pd.Series(mi, index=nonzero).sort_values(ascending=False)
    if mi_top_k is not None:
        mi_df = mi_df.head(mi_top_k)
    candidates = mi_df.index.tolist()
    logger.info("screen_features: %d candidates after MI filter", len(candidates))

    # Correlation pruning (Stage 1c)
    if len(candidates) > 1:
        corr = X_filled[candidates].corr().abs()
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
        return candidates

    # Stage 2 — LightGBM importance (TRAINING DATA ONLY — caller responsibility)
    try:
        import lightgbm as lgb  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "lightgbm is required for Stage-2 feature screening.  "
            "Install it with: pip install lightgbm"
        ) from exc

    _k = lgbm_top_k or max(1, len(candidates) // 2)
    dtrain = lgb.Dataset(X_filled[candidates], label=labels)
    params = {
        "objective": "binary",
        "verbosity": -1,
        "n_estimators": 100,
        "num_leaves": 31,
        "seed": random_state,
    }
    model = lgb.train(params, dtrain, num_boost_round=100)
    importance = pd.Series(
        model.feature_importance(importance_type="split"), index=candidates
    ).sort_values(ascending=False)
    candidates = importance.head(_k).index.tolist()
    logger.info("screen_features: %d features after LightGBM screening", len(candidates))

    return candidates


# ---------------------------------------------------------------------------
# player_profile_daily PIT / as-of join (PLAN Step 4 / SSOT §8.2, DEC-011)
# ---------------------------------------------------------------------------

def join_player_profile_daily(
    bets_df: pd.DataFrame,
    profile_df: Optional[pd.DataFrame],
    feature_cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Attach player_profile_daily features to bets via a PIT / as-of join.

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
        player_profile_daily snapshot table.  Must contain ``canonical_id``,
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
        logger.info("join_player_profile_daily: profile_df absent/empty — profile features are NaN")
        return result

    available_cols = [c for c in feature_cols if c in profile_df.columns]
    if not available_cols:
        logger.warning(
            "join_player_profile_daily: none of the requested profile columns found in profile_df; "
            "expected: %s",
            feature_cols[:5],
        )
        return result

    # Ensure tz-naive timestamps on both sides (apply_dq strips tz from bets;
    # profile_df may arrive tz-naive or tz-aware depending on data source).
    bet_time = pd.to_datetime(result["payout_complete_dtm"])
    if bet_time.dt.tz is not None:
        bet_time = bet_time.dt.tz_localize(None)

    snap_time = pd.to_datetime(profile_df["snapshot_dtm"])
    if snap_time.dt.tz is not None:
        snap_time = snap_time.dt.tz_localize(None)

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

    # merge_asof requires ascending sort by the time key (and the by= grouping
    # key must also be sorted, but merge_asof does NOT sort — we must do it).
    bets_sorted = bets_work.sort_values(["canonical_id", "_bet_time"]).reset_index(drop=True)
    profile_sorted = profile_work.sort_values(["canonical_id", "snapshot_dtm"]).reset_index(drop=True)

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

    for col in available_cols:
        result[col] = merged[col].values

    n_rated_with_profile = int(pd.notna(result[available_cols[0]]).sum())
    logger.info(
        "join_player_profile_daily: attached %d profile cols; %d/%d bets have profile snapshot",
        len(available_cols),
        n_rated_with_profile,
        len(result),
    )
    return result
