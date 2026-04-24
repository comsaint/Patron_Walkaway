"""trainer/labels.py
====================
Walkaway label construction — C1 extended pull, H1 right-censoring, G3 stable sort.

Public API
----------
``compute_labels(bets_df, window_end, extended_end) -> pd.DataFrame``
    Returns a copy of ``bets_df`` (sorted by G3 key) with two new columns:

    ``label`` (int8, 0 or 1)
        1 if a walkaway gap starts within ``ALERT_HORIZON_MIN`` minutes of
        this bet; 0 otherwise.  For censored rows the value is 0 but must
        NOT be used for training — filter ``censored == False`` first.

    ``censored`` (bool)
        True for the terminal (last-in-canonical_id) bet whose remaining
        observable window is too short to rule out a gap (H1 / TRN-06).
        These rows must be excluded from training AND evaluation.

Key design decisions
---------------------
* G3 (stable sort): internally sorted by
  ``(canonical_id, payout_complete_dtm, bet_id)`` using a stable algorithm
  so that same-millisecond bets (主注/旁注) are deterministically ordered
  in both trainer and scorer.
* H1 (terminal-bet censoring): the last bet of each canonical_id has no
  observed successor.  If ``payout_complete_dtm + WALKAWAY_GAP_MIN <=
  extended_end`` the gap is *determinable* (no bet for ≥ X minutes →
  walkaway).  Otherwise the bet is marked ``censored = True`` (TRN-06).
* Leakage prevention: columns ``next_bet_dtm`` and ``minutes_to_next_bet``
  are NEVER added to the output; an internal ``gap_start`` flag is also
  dropped before returning.
* ``extended_end`` must satisfy ``extended_end >= window_end +
  LABEL_LOOKAHEAD_MIN``; the extended zone ``(window_end, extended_end]``
  is used only for label computation — callers filter to
  ``payout_complete_dtm <= window_end`` for training.
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import pandas as pd

try:
    from config import ALERT_HORIZON_MIN, LABEL_LOOKAHEAD_MIN, WALKAWAY_GAP_MIN, HK_TZ  # type: ignore[import]
except ModuleNotFoundError:
    from trainer.config import ALERT_HORIZON_MIN, LABEL_LOOKAHEAD_MIN, WALKAWAY_GAP_MIN, HK_TZ  # type: ignore[import]

logger = logging.getLogger(__name__)

# Columns the caller must supply.  __etl_insert_Dtm is not required here
# (sorting uses bet_id as the stable tiebreaker within the same ms — G3).
_REQUIRED_BET_COLS: frozenset[str] = frozenset({
    "canonical_id",
    "bet_id",
    "payout_complete_dtm",
})


def compute_labels(
    bets_df: pd.DataFrame,
    window_end: datetime,
    extended_end: datetime,
) -> pd.DataFrame:
    """Compute walkaway labels for a batch of bets.

    Parameters
    ----------
    bets_df : DataFrame
        Bets ranging from ``window_start`` through ``extended_end``.
        Required columns: ``canonical_id``, ``bet_id``,
        ``payout_complete_dtm``.  Should come from ``t_bet FINAL``
        (caller responsibility); rows with null ``payout_complete_dtm``
        are dropped with a WARNING (E3 defensive guard).
    window_end : datetime
        End of the core training window.  Bets with
        ``payout_complete_dtm > window_end`` belong to the C1 extended
        zone and should be excluded from training after this call.
    extended_end : datetime
        End of the C1 extended pull — at least
        ``window_end + LABEL_LOOKAHEAD_MIN`` (recommended: window_end +
        1 day, see SSOT §7.2).  Must be >= ``window_end``.

    Returns
    -------
    DataFrame
        Sorted copy of ``bets_df`` with two new columns:
        ``label`` (int8) and ``censored`` (bool).
        No leakage columns (``next_bet_dtm``, ``minutes_to_next_bet``,
        internal ``gap_start``) appear in the result.

    Raises
    ------
    ValueError
        If required columns are missing or ``extended_end < window_end``.
    """
    # ------------------------------------------------------------------ #
    # Input validation
    # ------------------------------------------------------------------ #
    missing = _REQUIRED_BET_COLS - set(bets_df.columns)
    if missing:
        raise ValueError(
            f"bets_df is missing required columns: {sorted(missing)}"
        )

    window_end_ts = pd.Timestamp(window_end)
    extended_end_ts = pd.Timestamp(extended_end)
    # Defensive tz alignment (DEC-018 parity): callers may pass tz-aware
    # boundaries (e.g. from time_fold) while payout_complete_dtm is tz-naive.
    # Normalise both to tz-naive HK local time before any comparisons.
    if window_end_ts.tz is not None:
        window_end_ts = window_end_ts.tz_convert(HK_TZ).tz_localize(None)
    if extended_end_ts.tz is not None:
        extended_end_ts = extended_end_ts.tz_convert(HK_TZ).tz_localize(None)
    if extended_end_ts < window_end_ts:
        raise ValueError(
            f"extended_end ({extended_end}) must be >= window_end ({window_end})"
        )

    # R13: warn when the extended zone is narrower than LABEL_LOOKAHEAD_MIN.
    # We don't raise — a narrow window is still valid (e.g., last slice of data)
    # — but many terminal bets will become censored and the caller should know.
    _min_extended_end = window_end_ts + pd.Timedelta(minutes=LABEL_LOOKAHEAD_MIN)
    if extended_end_ts < _min_extended_end:
        logger.warning(
            "compute_labels: extended_end (%s) < window_end + LABEL_LOOKAHEAD_MIN (%s); "
            "terminal bets near the window boundary will be censored (TRN-06).",
            extended_end_ts,
            _min_extended_end,
        )

    # ------------------------------------------------------------------ #
    # E3 + R12: drop rows with null payout_complete_dtm or null canonical_id.
    # Compute the mask against the caller frame first so large Step 6 chunks do
    # not pay for an eager full-frame copy before we know which rows survive.
    # ------------------------------------------------------------------ #
    null_payout = bets_df["payout_complete_dtm"].isna()
    null_cid = bets_df["canonical_id"].isna()
    combined_null = null_payout | null_cid
    if combined_null.any():
        logger.warning(
            "compute_labels: dropped %d row(s) with null payout_complete_dtm (E3), %d with null canonical_id (R12)",
            null_payout.sum(),
            null_cid.sum(),
        )
        filtered = bets_df.loc[~combined_null]
    else:
        filtered = bets_df

    if filtered.empty:
        df = filtered.copy()
        df["label"] = pd.array([], dtype="int8")
        df["censored"] = pd.array([], dtype=bool)
        return df

    # ------------------------------------------------------------------ #
    # G3: stable sort — (canonical_id, payout_complete_dtm, bet_id)
    # ------------------------------------------------------------------ #
    df = (
        filtered.sort_values(
            ["canonical_id", "payout_complete_dtm", "bet_id"],
            ascending=True,
            kind="stable",       # preserves relative order for equal keys
        )
        .reset_index(drop=True)
    )

    # ------------------------------------------------------------------ #
    # Per-canonical_id: compute successor payout time
    # ------------------------------------------------------------------ #
    df["_next_payout"] = (
        df.groupby("canonical_id", sort=False)["payout_complete_dtm"]
        .shift(-1)
    )

    is_terminal = df["_next_payout"].isna()
    gap_duration_min = (
        (df["_next_payout"] - df["payout_complete_dtm"])
        .dt.total_seconds()
        .div(60)
    )

    # ------------------------------------------------------------------ #
    # H1: terminal-bet categorisation
    # ------------------------------------------------------------------ #
    walkaway_gap_delta = pd.Timedelta(minutes=WALKAWAY_GAP_MIN)

    # A terminal bet IS a gap start if coverage beyond it is sufficient:
    # payout_complete_dtm + WALKAWAY_GAP_MIN <= extended_end  →  we can
    # observe that at least WALKAWAY_GAP_MIN minutes passed with no bet.
    terminal_determinable = is_terminal & (
        df["payout_complete_dtm"] + walkaway_gap_delta <= extended_end_ts
    )

    # gap_start: either explicit (b_{i+1} - b_i >= X) or H1-determinable
    df["_gap_start"] = (
        (~is_terminal & (gap_duration_min >= WALKAWAY_GAP_MIN))
        | terminal_determinable
    )

    # censored: terminal bet where future coverage is insufficient (H1)
    df["censored"] = (is_terminal & ~terminal_determinable).astype(bool)

    # ------------------------------------------------------------------ #
    # Label computation (vectorized, O(n log n) per canonical_id group)
    # ------------------------------------------------------------------ #
    df["label"] = _compute_labels_vectorized(df)

    # ------------------------------------------------------------------ #
    # Drop internal / leakage columns before returning
    # ------------------------------------------------------------------ #
    df = df.drop(columns=["_next_payout", "_gap_start"])
    return df


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_labels_vectorized(df: pd.DataFrame) -> pd.Series:
    """Assign label=1 where a gap_start falls in [t, t + ALERT_HORIZON_MIN].

    Works on the sorted df produced by ``compute_labels``.  Uses
    ``np.searchsorted`` for O(n log n) complexity per group without
    Python-level per-bet loops.

    The df must already be sorted by (canonical_id, payout_complete_dtm,
    bet_id) and must contain ``_gap_start`` (bool) and
    ``payout_complete_dtm`` columns.
    """
    # Force to nanosecond int64 so the unit is explicit and consistent
    # regardless of whether the column dtype is datetime64[us] or [ns].
    horizon_ns = int(ALERT_HORIZON_MIN * 60 * 1e9)

    # Work entirely with int64 ns timestamps for speed
    times_all: np.ndarray = (
        df["payout_complete_dtm"].values.astype("datetime64[ns]").astype("int64")
    )
    gap_mask_all: np.ndarray = df["_gap_start"].values
    cid_all: np.ndarray = df["canonical_id"].values

    label_arr = np.zeros(len(df), dtype=np.int8)

    if len(cid_all) == 0:
        return pd.Series(label_arr, index=df.index, dtype="int8")

    # Find group boundaries in the sorted array (no Python-level groupby)
    change = np.empty(len(cid_all) + 1, dtype=bool)
    change[0] = True
    change[-1] = True
    change[1:-1] = cid_all[1:] != cid_all[:-1]
    boundaries = np.where(change)[0]  # shape: (n_groups + 1,)

    for i in range(len(boundaries) - 1):
        s, e = boundaries[i], boundaries[i + 1]
        times = times_all[s:e]
        gap_times = times[gap_mask_all[s:e]]

        if len(gap_times) == 0:
            continue

        # First gap_time >= t for each t in this group (gap_times is sorted)
        idxs = np.searchsorted(gap_times, times, side="left")
        valid = idxs < len(gap_times)
        in_horizon = np.zeros(e - s, dtype=bool)
        in_horizon[valid] = gap_times[idxs[valid]] <= times[valid] + horizon_ns

        label_arr[s:e] = in_horizon.astype(np.int8)

    return pd.Series(label_arr, index=df.index, dtype="int8")
