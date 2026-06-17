"""Walkaway label construction (trainer ``labels.compute_labels`` parity)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from trainer_hightier.config import (
    DEFAULT_WALKAWAY_LABEL_CONTRACT,
    HK_TZ,
    WalkawayLabelContract,
)

logger = logging.getLogger(__name__)

_HK_TZ_INFO: Final = ZoneInfo(HK_TZ)

_REQUIRED_BET_COLS: frozenset[str] = frozenset({"canonical_id", "bet_id", "payout_complete_dtm"})


def compute_labels(
    bets_df: pd.DataFrame,
    window_end: datetime,
    extended_end: datetime,
    *,
    label_contract: WalkawayLabelContract | None = None,
) -> pd.DataFrame:
    """Compute walkaway labels (label + censored columns). Mirrors legacy trainer semantics."""
    contract = label_contract or DEFAULT_WALKAWAY_LABEL_CONTRACT
    walkaway_gap_min = int(contract.walkaway_gap_min)
    alert_horizon_min = int(contract.alert_horizon_min)
    label_lookahead_min = int(contract.label_lookahead_min)

    missing = _REQUIRED_BET_COLS - set(bets_df.columns)
    if missing:
        raise ValueError(f"bets_df is missing required columns: {sorted(missing)}")

    window_end_ts = pd.Timestamp(window_end)
    extended_end_ts = pd.Timestamp(extended_end)

    if window_end_ts.tz is not None:
        window_end_ts = window_end_ts.tz_convert(_HK_TZ_INFO).tz_localize(None)
    if extended_end_ts.tz is not None:
        extended_end_ts = extended_end_ts.tz_convert(_HK_TZ_INFO).tz_localize(None)
    if extended_end_ts < window_end_ts:
        raise ValueError(f"extended_end ({extended_end}) must be >= window_end ({window_end})")

    _min_extended_end = window_end_ts + pd.Timedelta(minutes=float(label_lookahead_min))
    if extended_end_ts < _min_extended_end:
        logger.warning(
            "compute_labels: extended_end (%s) < window_end + label_lookahead_min (%s); "
            "terminal bets near boundary will be censored.",
            extended_end_ts,
            _min_extended_end,
        )

    null_payout = bets_df["payout_complete_dtm"].isna()
    null_cid = bets_df["canonical_id"].isna()
    combined_null = null_payout | null_cid
    if combined_null.any():
        logger.warning(
            "compute_labels: dropped %d row(s) with null payout_complete_dtm, "
            "%d with null canonical_id",
            int(null_payout.sum()),
            int(null_cid.sum()),
        )
        filtered = bets_df.loc[~combined_null]
    else:
        filtered = bets_df

    if filtered.empty:
        df = filtered.copy()
        df["label"] = pd.array([], dtype="int8")
        df["censored"] = pd.array([], dtype=bool)
        return df

    df = (
        filtered.sort_values(
            ["canonical_id", "payout_complete_dtm", "bet_id"],
            ascending=True,
            kind="stable",
        )
        .reset_index(drop=True)
    )

    df["_next_payout"] = df.groupby("canonical_id", sort=False)["payout_complete_dtm"].shift(-1)

    is_terminal = df["_next_payout"].isna()
    gap_duration_min = (df["_next_payout"] - df["payout_complete_dtm"]).dt.total_seconds().div(60)

    walkaway_gap_delta = pd.Timedelta(minutes=float(walkaway_gap_min))

    terminal_determinable = is_terminal & (
        df["payout_complete_dtm"] + walkaway_gap_delta <= extended_end_ts
    )
    df["_gap_start"] = (
        (~is_terminal & (gap_duration_min >= float(walkaway_gap_min))) | terminal_determinable
    )
    df["censored"] = (is_terminal & ~terminal_determinable).astype(bool)

    df["label"] = _compute_labels_vectorized(df, alert_horizon_min=alert_horizon_min)
    df = df.drop(columns=["_next_payout", "_gap_start"])
    return df


def _compute_labels_vectorized(df: pd.DataFrame, *, alert_horizon_min: int) -> pd.Series:
    """Vectorized horizon labels per canonical_id group."""

    horizon_ns = int(float(alert_horizon_min) * 60 * 1e9)
    times_all: np.ndarray = (
        df["payout_complete_dtm"].values.astype("datetime64[ns]").astype("int64")
    )
    gap_mask_all: np.ndarray = df["_gap_start"].values
    cid_all: np.ndarray = df["canonical_id"].values

    label_arr = np.zeros(len(df), dtype=np.int8)
    if len(cid_all) == 0:
        return pd.Series(label_arr, index=df.index, dtype="int8")

    change = np.empty(len(cid_all) + 1, dtype=bool)
    change[0] = True
    change[-1] = True
    change[1:-1] = cid_all[1:] != cid_all[:-1]
    boundaries = np.where(change)[0]

    for i in range(len(boundaries) - 1):
        s, e = boundaries[i], boundaries[i + 1]
        times = times_all[s:e]
        gap_times = times[gap_mask_all[s:e]]

        if len(gap_times) == 0:
            continue

        idxs = np.searchsorted(gap_times, times, side="left")
        valid = idxs < len(gap_times)
        in_horizon = np.zeros(e - s, dtype=bool)
        in_horizon[valid] = gap_times[idxs[valid]] <= times[valid] + horizon_ns
        label_arr[s:e] = in_horizon.astype(np.int8)

    return pd.Series(label_arr, index=df.index, dtype="int8")
