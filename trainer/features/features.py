"""trainer/features/features.py
=============================
Shared feature engineering — Train-Serve Parity core (TRN-05/07/08).

Architecture (DEC-022: Track Profile / Track LLM / Track Human)
--------------------------------------------------------------
**Track Human — Vectorized hand-crafted features** (state-machine logic)
    compute_loss_streak()       LOSE→+1, WIN→reset, PUSH→conditional (F4)
    compute_run_boundary()      Gap ≥ RUN_BREAK_MIN → new run (B2)
    compute_table_hc()          Unique players per table in rolling window (S1)

**Feature screening** (unified across tracks)
    screen_features()           Mutual-info → correlation pruning → optional LGBM

All Track Human functions are imported by BOTH trainer.py and scorer.py to
guarantee train-serve parity.  They must be kept stateless (no global mutable
state) and must only look backward in time from each observation's cutoff.

Sorting convention (G3)
-----------------------
Every Track Human function sorts its input by
    (canonical_id | table_id, payout_complete_dtm, bet_id)
with ``kind='stable'`` before processing, matching the scorer's sort order.

H4 (numeric fillna)
-------------------
Numeric columns are filled with 0 before building the EntitySet so that
aggregations are not contaminated by NaN propagation.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd
import pathlib
import yaml as _yaml  # type: ignore[import-untyped]

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

# Lookback bounds (STATUS Code Review compute_run_boundary 2026-03-11 §2): single SSOT for both
# compute_loss_streak and compute_run_boundary to avoid int64/pd.Timedelta overflow.
_LOOKBACK_MAX_HOURS = 1000
_LOOKBACK_MAX_DELTA_NS = _LOOKBACK_MAX_HOURS * 3600 * 10**9
_LOOKBACK_BOUNDS_MSG = (
    "lookback_hours must be positive and not exceed 1000 hours for lookback computation"
)
# Run break upper bound for lookback (STATUS Code Review run_boundary #2): avoid int64 overflow in numba.
_RUN_BREAK_MAX_MIN = 10000
_RUN_BREAK_MAX_NS = _RUN_BREAK_MAX_MIN * 60 * 10**9
_RUN_BREAK_BOUNDS_MSG = (
    "RUN_BREAK_MIN must be in [0, 10000] minutes for lookback computation"
)


def _datetime_to_ns_int64(series: pd.Series) -> np.ndarray:
    """Convert datetime series to int64 nanoseconds for numba lookback kernels.

    Uses .view('int64') on datetime64[ns] so the kernel receives nanoseconds (same
    unit as delta_ns / run_break_min_ns). Avoids platform/astype(int64) giving us.
    """
    arr = pd.to_datetime(series, utc=False).values
    if arr.dtype != np.dtype("datetime64[ns]"):
        arr = arr.astype("datetime64[ns]")
    return arr.view("int64").copy()  # copy so kernel gets contiguous int64


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

# Deploy: prefer MODEL_DIR/feature_spec.yaml when present (frozen train–serve spec).
# If MODEL_DIR is set but that file is missing (e.g. local .env + empty out/models),
# fall back to repo SSOT with a warning. When MODEL_DIR is unset, load repo SSOT only.
_repo_candidates_yaml = pathlib.Path(__file__).resolve().parent.parent / "feature_spec" / "features_candidates.yaml"
_model_dir_env = os.environ.get("MODEL_DIR")
_deploy_yaml = pathlib.Path(_model_dir_env) / "feature_spec.yaml" if _model_dir_env else None

if _deploy_yaml is not None and _deploy_yaml.is_file():
    _yaml_path = _deploy_yaml
elif _deploy_yaml is not None:
    logger.warning(
        "MODEL_DIR is set but feature spec not found at %s — loading repo candidates from %s. "
        "For strict deploy, include feature_spec.yaml under MODEL_DIR.",
        _deploy_yaml,
        _repo_candidates_yaml,
    )
    _yaml_path = _repo_candidates_yaml
else:
    _yaml_path = _repo_candidates_yaml

try:
    with open(_yaml_path, "r", encoding="utf-8") as _f:
        _TEMPLATE_SPEC = _yaml.safe_load(_f) or {}
except FileNotFoundError:
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "Feature Spec YAML not found at %s — PROFILE_FEATURE_COLS will be empty. "
        "Ensure features_candidates.yaml (repo spec) exists before training.",
        _yaml_path,
    )
    _TEMPLATE_SPEC = {}

PROFILE_FEATURE_COLS: List[str] = [
    c["feature_id"]
    for c in _TEMPLATE_SPEC.get("track_profile", {}).get("candidates", [])
    if c.get("feature_id")
]

# Minimum lookback (days) required to compute each profile feature.
_PROFILE_FEATURE_MIN_DAYS: dict = {
    c["feature_id"]: c.get("min_lookback_days", 365)
    for c in _TEMPLATE_SPEC.get("track_profile", {}).get("candidates", [])
    if c.get("feature_id")
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
# Feature Spec YAML helpers (PLAN: 特徵整合 Step 2)
# ---------------------------------------------------------------------------

def _is_screening_ineligible(val) -> bool:
    """Return True if *val* explicitly marks a candidate as not screening-eligible.

    Handles the following representations that all mean "not eligible":
    - Python ``False`` (canonical YAML ``false``)
    - Integer ``0``
    - String ``"false"`` or ``"False"`` (defensive guard against YAML authored as string)

    Anything else (``True``, ``1``, ``"true"``, ``None`` / missing) is treated as eligible.
    """
    if val is False:
        return True
    if val is True or val is None:
        return False
    if isinstance(val, int) and val == 0:
        return True
    if isinstance(val, str) and val.strip().lower() == "false":
        return True
    return False


def get_candidate_feature_ids(
    spec: dict,
    track: str,
    screening_only: bool = False,
) -> List[str]:
    """從 YAML spec 讀取某一軌的 candidate feature_id 列表。

    track 為 "track_llm" | "track_human" | "track_profile"。
    screening_only=True 時排除 dtype='str' 或 screening_eligible 為 false/0/"false"
    的候選（中間變數不參與篩選）。
    """
    # R404 Review #4: candidates may be non-list (e.g. dict); treat as no candidates.
    _raw = ((spec.get(track) or {}).get("candidates"))
    candidates = _raw if isinstance(_raw, list) else []
    out: List[str] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        fid = c.get("feature_id")
        if not fid:
            continue
        if screening_only:
            if _is_screening_ineligible(c.get("screening_eligible")):
                continue
            if c.get("dtype") == "str":
                continue
        out.append(fid)
    return out


def get_all_candidate_feature_ids(
    spec: dict,
    screening_only: bool = False,
) -> List[str]:
    """三軌候選 feature_id 合併去重（order: track_llm, track_human, track_profile）。"""
    ids_llm = get_candidate_feature_ids(spec, "track_llm", screening_only)
    ids_human = get_candidate_feature_ids(spec, "track_human", screening_only)
    ids_profile = get_candidate_feature_ids(spec, "track_profile", screening_only)
    return list(dict.fromkeys(ids_llm + ids_human + ids_profile))


def get_profile_min_lookback(spec: dict) -> dict:
    """從 track_profile.candidates 讀取 { feature_id: min_lookback_days }。

    若候選無 min_lookback_days 則預設 365。
    """
    candidates = ((spec.get("track_profile") or {}).get("candidates") or [])
    return {
        c["feature_id"]: c.get("min_lookback_days", 365)
        for c in candidates
        if c.get("feature_id")
    }


def coerce_feature_dtypes(
    df: pd.DataFrame,
    feature_cols: List[str],
) -> pd.DataFrame:
    """將指定欄位強制為數值；非數值 → NaN（訓練與推論共用，train-serve parity）。"""
    for col in feature_cols:
        if col in df.columns and not pd.api.types.is_numeric_dtype(df[col]):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Track Human — Vectorized hand-crafted features
# ---------------------------------------------------------------------------

_REQUIRED_STREAK_COLS: frozenset[str] = frozenset(
    {"canonical_id", "bet_id", "payout_complete_dtm", "status"}
)
_REQUIRED_RUN_COLS: frozenset[str] = frozenset(
    {"canonical_id", "bet_id", "payout_complete_dtm"}
)

# Optional numba kernel for lookback streak (PLAN § Phase 2 Track Human Lookback 向量化)
try:
    from numba import jit as numba_jit

    @numba_jit(nopython=True, cache=True)
    def _streak_lookback_numba(times, status, push_resets, delta_ns, out):
        # times: int64 array (nanoseconds), status: int8 (1=LOSE,2=WIN,3=PUSH), out: int32
        n = times.shape[0]
        lo = 0
        for i in range(n):
            t_i = times[i]
            lo_bound = t_i - delta_ns
            while lo < i and times[lo] <= lo_bound:
                lo += 1
            streak = 0
            for j in range(lo, i + 1):
                s = status[j]
                if s == 2:  # WIN
                    streak = 0
                elif s == 3:  # PUSH
                    if push_resets:
                        streak = 0
                elif s == 1:  # LOSE
                    streak += 1
            out[i] = streak
except Exception:
    _streak_lookback_numba = None

# Optional numba kernel for run_boundary lookback (PLAN § Phase 2 Track Human Lookback 向量化)
try:
    from numba import jit as _numba_jit_run

    @_numba_jit_run(nopython=True, cache=True)
    def _run_boundary_lookback_numba(
        times_ns,
        wager,
        casino_win,
        run_break_min_ns,
        delta_ns,
        out_run_id,
        out_min_since,
        out_bets_in_run,
        out_wager_sum,
        out_net_win,
    ):
        # times_ns: int64 (nanoseconds), wager: float64, run_break_min_ns/delta_ns: int64
        # outputs: int32, float64, int32, float64
        n = times_ns.shape[0]
        lo = 0
        for i in range(n):
            t_i = times_ns[i]
            lo_bound = t_i - delta_ns
            while lo < i and times_ns[lo] <= lo_bound:
                lo += 1
            run_id = 0
            run_start_ns = times_ns[lo]
            bets_in_run_cur = 0
            wager_sum_cur = 0.0
            net_win_cur = 0.0
            for j in range(lo, i + 1):
                gap_ns = times_ns[j] - times_ns[j - 1] if j > lo else 0
                is_new_run = (j == lo) or (gap_ns >= run_break_min_ns)
                if is_new_run:
                    if j > lo:
                        run_id += 1
                    run_start_ns = times_ns[j]
                    bets_in_run_cur = 1
                    wager_sum_cur = wager[j]
                    net_win_cur = -casino_win[j]
                else:
                    bets_in_run_cur += 1
                    wager_sum_cur += wager[j]
                    net_win_cur += -casino_win[j]
            out_run_id[i] = run_id
            out_min_since[i] = (times_ns[i] - run_start_ns) / (60.0 * 1e9)
            out_bets_in_run[i] = bets_in_run_cur
            out_wager_sum[i] = wager_sum_cur
            out_net_win[i] = net_win_cur
except Exception:
    _run_boundary_lookback_numba = None

_REQUIRED_HC_COLS: frozenset[str] = frozenset(
    {"table_id", "bet_id", "payout_complete_dtm", "player_id"}
)


def compute_loss_streak(
    bets_df: pd.DataFrame,
    cutoff_time: Optional[datetime] = None,
    lookback_hours: Optional[float] = None,
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
    When ``lookback_hours`` is set (e.g. SCORER_LOOKBACK_HOURS for train–serve
    parity), streak at row i uses only bets in (t_i - lookback_hours, t_i].

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
    lookback_hours : float | None
        If set, for each row only bets with payout_complete_dtm in
        (row_time - lookback_hours, row_time] are used (train–serve parity
        with scorer's fetch window). None = use all bets (current behavior).

    Returns
    -------
    pd.Series[int]
        Same index as ``bets_df`` (subset if cutoff_time is given).
        Rows beyond cutoff_time are absent from the returned Series.
    """
    missing = _REQUIRED_STREAK_COLS - set(bets_df.columns)
    if missing:
        raise ValueError(f"compute_loss_streak: missing columns {sorted(missing)}")
    if lookback_hours is not None and lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive when set")

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

    if lookback_hours is not None and lookback_hours > 0:
        # Per-row context: for each row only use bets in (t - lookback_hours, t].
        # Phase 2 (PLAN § Track Human Lookback 向量化): numba two-pointer when available.
        delta_ns = int(float(lookback_hours) * 1e9 * 3600)
        if delta_ns <= 0 or delta_ns > _LOOKBACK_MAX_DELTA_NS:
            raise ValueError(_LOOKBACK_BOUNDS_MSG)
        delta = pd.Timedelta(hours=float(lookback_hours))
        push_resets_int = 1 if LOSS_STREAK_PUSH_RESETS else 0
        out_list: List[tuple] = []
        use_numba = _streak_lookback_numba is not None
        if use_numba:
            try:
                for _cid, grp in df.groupby("canonical_id", sort=False):
                    # Review #1: groups with NaT use Python path to match fallback semantics (STATUS.md).
                    if grp["payout_complete_dtm"].isna().any():
                        times = pd.to_datetime(grp["payout_complete_dtm"], utc=False)
                        for idx, t in zip(grp.index, times):
                            lo = t - delta
                            sub = grp.loc[(times > lo) & (times <= t)]
                            if sub.empty:
                                out_list.append((idx, 0))
                                continue
                            sub = sub.sort_values(["payout_complete_dtm", "bet_id"], kind="stable")
                            _is_lose = (sub["status"] == "LOSE").astype("int8")
                            _is_reset = (
                                (sub["status"] == "WIN")
                                | ((sub["status"] == "PUSH") & LOSS_STREAK_PUSH_RESETS)
                            ).astype("int8")
                            _reset_grp = _is_reset.cumsum()
                            streak_sub = _is_lose.groupby(_reset_grp.values, sort=False).cumsum().astype("int32")
                            out_list.append((idx, int(streak_sub.iloc[-1])))
                        continue
                    times_ns = _datetime_to_ns_int64(grp["payout_complete_dtm"])
                    status_arr = (
                        grp["status"]
                        .map({"LOSE": 1, "WIN": 2, "PUSH": 3})
                        .fillna(0)
                        .astype(np.int8)
                    )
                    out_arr = np.zeros(len(grp), dtype=np.int32)
                    _streak_lookback_numba(
                        times_ns,
                        status_arr.values,
                        np.int8(push_resets_int),
                        np.int64(delta_ns),
                        out_arr,
                    )
                    for idx, val in zip(grp.index, out_arr):
                        out_list.append((idx, int(val)))
            except Exception as e:
                logger.warning(
                    "compute_loss_streak: numba lookback failed (%s), falling back to Python path",
                    e,
                )
                use_numba = False
        if not use_numba:
            if len(df) > 100_000:
                logger.warning(
                    "compute_loss_streak: lookback without numba on %d rows may be slow (7h+ at 25M)",
                    len(df),
                )
            out_list = []
            for cid, grp in df.groupby("canonical_id", sort=False):
                times = pd.to_datetime(grp["payout_complete_dtm"], utc=False)
                for idx, t in zip(grp.index, times):
                    lo = t - delta
                    sub = grp.loc[(times > lo) & (times <= t)]
                    if sub.empty:
                        out_list.append((idx, 0))
                        continue
                    sub = sub.sort_values(["payout_complete_dtm", "bet_id"], kind="stable")
                    _is_lose = (sub["status"] == "LOSE").astype("int8")
                    _is_reset = (
                        (sub["status"] == "WIN")
                        | ((sub["status"] == "PUSH") & LOSS_STREAK_PUSH_RESETS)
                    ).astype("int8")
                    _reset_grp = _is_reset.cumsum()
                    streak_sub = _is_lose.groupby(_reset_grp.values, sort=False).cumsum().astype("int32")
                    out_list.append((idx, int(streak_sub.iloc[-1])))
        streak = pd.Series(
            {idx: v for idx, v in out_list},
            dtype="int32",
        ).reindex(df.index, fill_value=0)
        return streak

    # Vectorized streak using cumsum-of-resets approach (no lookback):
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
    lookback_hours: Optional[float] = None,
) -> pd.DataFrame:
    """Assign run_id and minutes_since_run_start for each bet.

    A new run starts at the first bet of each canonical_id, and again
    whenever the gap to the previous bet (within the same canonical_id)
    is >= ``RUN_BREAK_MIN`` minutes (B2 correction).

    G3: sorted by (canonical_id, payout_complete_dtm, bet_id) internally.
    When ``lookback_hours`` is set (e.g. SCORER_LOOKBACK_HOURS for train–serve
    parity), run at row i is computed only from bets in (t_i - lookback_hours, t_i].

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
    lookback_hours : float | None
        If set, for each row only bets with payout_complete_dtm in
        (row_time - lookback_hours, row_time] are used (train–serve parity).
        None = use all bets (current behavior).

    Returns
    -------
    DataFrame
        Original columns (for bets ≤ cutoff_time) + four new columns:
        ``run_id`` (int, 0-based within each canonical_id),
        ``minutes_since_run_start`` (float ≥ 0),
        ``bets_in_run_so_far`` (int, 1-based count within run),
        ``wager_sum_in_run_so_far`` (float, cumulative wager in run; 0 if wager missing),
        ``net_win_in_run_so_far`` (float, cumulative player net win in run; 0 if casino_win missing),
        ``net_win_per_bet_in_run`` (float, average player net win per bet in run).
        Sorted by (canonical_id, payout_complete_dtm, bet_id).
    """
    missing = _REQUIRED_RUN_COLS - set(bets_df.columns)
    if missing:
        raise ValueError(f"compute_run_boundary: missing columns {sorted(missing)}")
    if lookback_hours is not None and lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive when set")

    if bets_df.empty:
        result = bets_df.copy()
        result["run_id"] = pd.array([], dtype="int32")
        result["minutes_since_run_start"] = pd.array([], dtype="float64")
        result["bets_in_run_so_far"] = pd.array([], dtype="int32")
        result["wager_sum_in_run_so_far"] = pd.array([], dtype="float64")
        result["net_win_in_run_so_far"] = pd.array([], dtype="float64")
        result["net_win_per_bet_in_run"] = pd.array([], dtype="float64")
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

    if lookback_hours is not None and lookback_hours > 0:
        # Per-row context: for each row only use bets in (t - lookback_hours, t].
        # Phase 2 (PLAN § Track Human Lookback 向量化): numba two-pointer when available.
        delta_ns = int(float(lookback_hours) * 1e9 * 3600)
        if delta_ns <= 0 or delta_ns > _LOOKBACK_MAX_DELTA_NS:
            raise ValueError(_LOOKBACK_BOUNDS_MSG)
        run_break_min_ns = int(float(RUN_BREAK_MIN) * 60 * 1e9)
        if run_break_min_ns < 0 or run_break_min_ns > _RUN_BREAK_MAX_NS:
            raise ValueError(_RUN_BREAK_BOUNDS_MSG)
        delta = pd.Timedelta(hours=float(lookback_hours))
        run_id_list: List[tuple] = []
        min_since_list: List[tuple] = []
        bets_in_run_list: List[tuple] = []
        wager_sum_list: List[tuple] = []
        net_win_list: List[tuple] = []
        use_numba = _run_boundary_lookback_numba is not None

        def _run_boundary_python_loop(grp: pd.DataFrame, times: pd.Series) -> None:
            # Review #1 (STATUS Code Review 2026-03-11): when group has NaT, 0 for NaT rows.
            has_nat = times.isna().any()
            for idx, t in zip(grp.index, times):
                if has_nat and pd.isna(t):
                    run_id_list.append((idx, 0))
                    min_since_list.append((idx, 0.0))
                    bets_in_run_list.append((idx, 0))
                    wager_sum_list.append((idx, 0.0))
                    net_win_list.append((idx, 0.0))
                    continue
                lo = t - delta
                mask = (times.notna()) & (times > lo) & (times <= t) if has_nat else (times > lo) & (times <= t)
                sub = grp.loc[mask]
                if sub.empty:
                    run_id_list.append((idx, 0))
                    min_since_list.append((idx, 0.0))
                    bets_in_run_list.append((idx, 0))
                    wager_sum_list.append((idx, 0.0))
                    net_win_list.append((idx, 0.0))
                    continue
                sub = sub.sort_values(["payout_complete_dtm", "bet_id"], kind="stable")
                prev = sub["payout_complete_dtm"].shift(1)
                gap_min = (sub["payout_complete_dtm"] - prev).dt.total_seconds().div(60)
                is_new = prev.isna() | (gap_min >= RUN_BREAK_MIN)
                run_id_sub = is_new.astype("int8").cumsum().sub(1).astype("int32")
                run_start = sub["payout_complete_dtm"].where(is_new).ffill()
                min_since = (sub["payout_complete_dtm"] - run_start).dt.total_seconds().div(60)
                bets_in_run = run_id_sub.groupby(run_id_sub).cumcount() + 1
                wager_sub = (
                    sub["wager"].fillna(0.0).groupby(run_id_sub, sort=False).cumsum()
                    if "wager" in sub.columns
                    else pd.Series(0.0, index=sub.index)
                )
                net_win_sub = (
                    (-sub["casino_win"].fillna(0.0)).groupby(run_id_sub, sort=False).cumsum()
                    if "casino_win" in sub.columns
                    else pd.Series(0.0, index=sub.index)
                )
                run_id_list.append((idx, int(run_id_sub.iloc[-1])))
                min_since_list.append((idx, float(min_since.iloc[-1])))
                bets_in_run_list.append((idx, int(bets_in_run.iloc[-1])))
                wager_sum_list.append((idx, float(wager_sub.iloc[-1])))
                net_win_list.append((idx, float(net_win_sub.iloc[-1])))

        for cid, grp in df.groupby("canonical_id", sort=False):
            times = pd.to_datetime(grp["payout_complete_dtm"], utc=False)
            if use_numba:
                try:
                    if times.isna().any():
                        _run_boundary_python_loop(grp, times)
                        continue
                    times_ns = _datetime_to_ns_int64(grp["payout_complete_dtm"])
                    wager_arr = (
                        grp["wager"].fillna(0.0).to_numpy(dtype=np.float64, copy=True)
                        if "wager" in grp.columns
                        else np.zeros(len(grp), dtype=np.float64)
                    )
                    casino_win_arr = (
                        grp["casino_win"].fillna(0.0).to_numpy(dtype=np.float64, copy=True)
                        if "casino_win" in grp.columns
                        else np.zeros(len(grp), dtype=np.float64)
                    )
                    out_run_id = np.zeros(len(grp), dtype=np.int32)
                    out_min_since = np.zeros(len(grp), dtype=np.float64)
                    out_bets_in_run = np.zeros(len(grp), dtype=np.int32)
                    out_wager_sum = np.zeros(len(grp), dtype=np.float64)
                    out_net_win = np.zeros(len(grp), dtype=np.float64)
                    _run_boundary_lookback_numba(
                        times_ns,
                        wager_arr,
                        casino_win_arr,
                        np.int64(run_break_min_ns),
                        np.int64(delta_ns),
                        out_run_id,
                        out_min_since,
                        out_bets_in_run,
                        out_wager_sum,
                        out_net_win,
                    )
                    for k, idx in enumerate(grp.index):
                        run_id_list.append((idx, int(out_run_id[k])))
                        min_since_list.append((idx, float(out_min_since[k])))
                        bets_in_run_list.append((idx, int(out_bets_in_run[k])))
                        wager_sum_list.append((idx, float(out_wager_sum[k])))
                        net_win_list.append((idx, float(out_net_win[k])))
                except Exception as e:
                    logger.warning(
                        "compute_run_boundary: numba lookback failed (%s), falling back to Python path",
                        e,
                    )
                    use_numba = False
                    _run_boundary_python_loop(grp, times)
            else:
                _run_boundary_python_loop(grp, times)
        if not use_numba and len(df) > 100_000:
            logger.warning(
                "compute_run_boundary: lookback without numba on %d rows may be slow (7h+ at 25M)",
                len(df),
            )
        df["run_id"] = pd.Series({i: v for i, v in run_id_list}, dtype="int32").reindex(df.index, fill_value=0).values
        df["minutes_since_run_start"] = pd.Series({i: v for i, v in min_since_list}, dtype="float64").reindex(df.index, fill_value=0.0).values
        df["bets_in_run_so_far"] = pd.Series({i: v for i, v in bets_in_run_list}, dtype="int32").reindex(df.index, fill_value=0).values
        df["wager_sum_in_run_so_far"] = pd.Series({i: v for i, v in wager_sum_list}, dtype="float64").reindex(df.index, fill_value=0.0).values
        df["net_win_in_run_so_far"] = pd.Series({i: v for i, v in net_win_list}, dtype="float64").reindex(df.index, fill_value=0.0).values
    else:
        # Gap to previous bet within canonical_id (NaT for the first bet)
        prev_payout = df.groupby("canonical_id", sort=False)["payout_complete_dtm"].shift(1)
        gap_min = (df["payout_complete_dtm"] - prev_payout).dt.total_seconds().div(60)

        # New run: first bet of cid (prev_payout is NaT) OR gap >= RUN_BREAK_MIN
        is_new_run = prev_payout.isna() | (gap_min >= RUN_BREAK_MIN)

        # run_id = cumsum of is_new_run, minus 1 so it starts at 0.
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

        # Run-level cumulative features (bets in run so far, wager sum in run so far)
        df["bets_in_run_so_far"] = (
            df.groupby(["canonical_id", "run_id"], sort=False).cumcount() + 1
        ).astype("int32")
        if "wager" in df.columns:
            df["wager_sum_in_run_so_far"] = (
                df.groupby(["canonical_id", "run_id"], sort=False)["wager"].cumsum()
            )
        else:
            df["wager_sum_in_run_so_far"] = 0.0
        if "casino_win" in df.columns:
            df["net_win_in_run_so_far"] = (
                df.groupby(["canonical_id", "run_id"], sort=False)["casino_win"]
                .transform(lambda s: (-s.fillna(0.0)).cumsum())
            )
        else:
            df["net_win_in_run_so_far"] = 0.0

        df = df.drop(columns=["_is_new_run", "_run_start"])

    # Run-level per-bet net win (contract: denominator 0 -> 0.0).
    # bets_in_run_so_far is 1-based by construction, so zero should only occur on defensive paths.
    _den = pd.to_numeric(df["bets_in_run_so_far"], errors="coerce").fillna(0)
    df["net_win_per_bet_in_run"] = np.where(
        _den > 0,
        pd.to_numeric(df["net_win_in_run_so_far"], errors="coerce").fillna(0.0) / _den,
        0.0,
    )

    # Apply cutoff_time filter after computing run_id / minutes_since_run_start
    if cutoff_ts is not None:
        df = df[df["payout_complete_dtm"] <= cutoff_ts].copy()

    return df


def compute_run_boundary_features(
    bets_df: pd.DataFrame,
    cutoff_time: Optional[datetime] = None,
    lookback_hours: Optional[float] = None,
) -> pd.DataFrame:
    """Compatibility wrapper for YAML ``function_name`` resolution.

    Feature specs reference ``compute_run_boundary_features`` for Track Human
    run-boundary families.  The canonical implementation lives in
    :func:`compute_run_boundary`; this wrapper preserves that contract and keeps
    train/serve/backtest parity when function dispatch is name-based.
    """
    return compute_run_boundary(
        bets_df=bets_df,
        cutoff_time=cutoff_time,
        lookback_hours=lookback_hours,
    )


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

# Step 8 DuckDB std (PLAN: Step 8 Feature Screening DuckDB 算統計量)
def _duckdb_quote_identifier(name: str) -> str:
    """Escape identifier for DuckDB SQL (double-quote and double any internal ")."""
    return '"' + name.replace('"', '""') + '"'


def compute_column_std_duckdb(
    columns: List[str],
    *,
    path: Optional[Path] = None,
    df: Optional[pd.DataFrame] = None,
) -> pd.Series:
    """Compute population std (stddev_pop) per column via DuckDB; avoids full-df .std() OOM.

    Exactly one of path or df must be set. Returns a Series with index = columns and
    values = std (0.0 where column is missing or non-numeric). Uses stddev_pop (ddof=0).
    Only numeric columns are passed to stddev_pop (PLAN § 注意事項: 字串/類別欄跳過).
    """
    if (path is None) == (df is None):
        raise ValueError("compute_column_std_duckdb: exactly one of path or df must be provided")
    if not columns:
        return pd.Series(dtype=float)

    import duckdb

    # Only numeric columns: avoid stddev_pop(VARCHAR) BinderException (PLAN §7).
    if df is not None:
        numeric_cols = [
            c for c in columns
            if c in df.columns and pd.api.types.is_numeric_dtype(df[c])
        ]
    else:
        assert path is not None
        path_escaped = str(path).replace("'", "''")
        con_schema = duckdb.connect(":memory:")
        try:
            con_schema.execute(f"SELECT * FROM read_parquet('{path_escaped}') LIMIT 0")
            empty = con_schema.fetchdf()
            numeric_cols = [
                c for c in columns
                if c in empty.columns and pd.api.types.is_numeric_dtype(empty[c])
            ]
        finally:
            con_schema.close()

    if not numeric_cols:
        return pd.Series(0.0, index=columns)

    quoted = [_duckdb_quote_identifier(c) for c in numeric_cols]
    select_list = ", ".join(f"stddev_pop({q}) AS {q}" for q in quoted)
    con = duckdb.connect(":memory:")
    try:
        if path is not None:
            path_escaped = str(path).replace("'", "''")
            con.execute(f"SELECT {select_list} FROM read_parquet('{path_escaped}')")
        else:
            assert df is not None
            con.register("_screen_std_src", df[numeric_cols])
            con.execute("SELECT " + select_list + " FROM _screen_std_src")
        row = con.fetchone()
        if row is None:
            out = pd.Series(index=columns, dtype=float)
        else:
            out = pd.Series(dict(zip(numeric_cols, list(row))), dtype=float)
            out = out.reindex(columns, fill_value=0.0)
        out = out.fillna(0.0)
        return out
    finally:
        con.close()


def compute_correlation_matrix_duckdb(
    columns: List[str],
    *,
    path: Optional[Path] = None,
    df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """Compute K×K absolute correlation matrix via DuckDB (PLAN Step 8 Phase 2).

    Exactly one of path or df must be set. Returns a DataFrame with index and
    columns = requested columns; only numeric columns are used for CORR, missing
    or non-numeric get 0.0. Uses DuckDB corr(); result is symmetric with 1.0 on
    diagonal (abs). For 0 or 1 column returns empty or 1×1 [[1.0]].

    path should only come from controlled pipeline output (e.g. step7_train_path);
    do not pass unvalidated user input.
    """
    if (path is None) == (df is None):
        raise ValueError(
            "compute_correlation_matrix_duckdb: exactly one of path or df must be provided"
        )
    if not columns:
        return pd.DataFrame()

    import duckdb

    # Only numeric columns (same as compute_column_std_duckdb).
    if df is not None:
        numeric_cols = [
            c for c in columns
            if c in df.columns and pd.api.types.is_numeric_dtype(df[c])
        ]
    else:
        assert path is not None
        path_escaped = str(path).replace("'", "''")
        con_schema = duckdb.connect(":memory:")
        try:
            con_schema.execute(f"SELECT * FROM read_parquet('{path_escaped}') LIMIT 0")
            empty = con_schema.fetchdf()
            numeric_cols = [
                c for c in columns
                if c in empty.columns and pd.api.types.is_numeric_dtype(empty[c])
            ]
        finally:
            con_schema.close()

    if not numeric_cols:
        return pd.DataFrame(0.0, index=columns, columns=columns)
    if len(numeric_cols) == 1:
        out = pd.DataFrame([[1.0]], index=numeric_cols, columns=numeric_cols)
        out = out.reindex(index=columns, columns=columns, fill_value=0.0)
        return out.astype(float)

    quoted = [_duckdb_quote_identifier(c) for c in numeric_cols]
    K = len(numeric_cols)
    select_parts = []
    for i in range(K):
        for j in range(i, K):
            alias = f"_c{i}_{j}"
            select_parts.append(f"corr({quoted[i]}, {quoted[j]}) AS {alias}")
    select_sql = ", ".join(select_parts)
    con = duckdb.connect(":memory:")
    try:
        if path is not None:
            path_escaped = str(path).replace("'", "''")
            con.execute(f"SELECT {select_sql} FROM read_parquet('{path_escaped}')")
        else:
            assert df is not None
            con.register("_corr_src", df[numeric_cols])
            con.execute("SELECT " + select_sql + " FROM _corr_src")
        row = con.fetchone()
    finally:
        con.close()

    if row is None:
        mat = pd.DataFrame(0.0, index=numeric_cols, columns=numeric_cols)
        mat = mat.reindex(index=columns, columns=columns, fill_value=0.0)
        return mat

    expected_len = K * (K + 1) // 2
    if len(row) != expected_len:
        logger.warning(
            "compute_correlation_matrix_duckdb: DuckDB row length %d != expected %d; returning diagonal matrix",
            len(row),
            expected_len,
        )
        diag_mat = np.eye(K, dtype=float)
        mat = pd.DataFrame(diag_mat, index=numeric_cols, columns=numeric_cols)
        mat = mat.reindex(index=columns, columns=columns, fill_value=0.0)
        return mat.astype(float)

    # Build upper triangle from row, then symmetrize (row has K*(K+1)/2 values).
    idx = 0
    corr_abs = np.zeros((K, K))
    for i in range(K):
        for j in range(i, K):
            v = row[idx]
            idx += 1
            val = 0.0 if (v is None or (isinstance(v, float) and np.isnan(v))) else float(np.abs(v))
            if i == j:
                corr_abs[i, j] = 1.0
            else:
                corr_abs[i, j] = val
                corr_abs[j, i] = val
    out = pd.DataFrame(corr_abs, index=numeric_cols, columns=numeric_cols)
    out = out.reindex(index=columns, columns=columns, fill_value=0.0)
    return out.astype(float)


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
    screen_method: str = "lgbm",
    random_state: int = 42,
    train_path: Optional[Path] = None,
    train_df: Optional[pd.DataFrame] = None,
) -> List[str]:
    """Feature screening (SSOT §8.2-D, DEC-020; PLAN screen-lgbm-default).

    Three modes via ``screen_method``:
        "lgbm"  — zv → correlation pruning → LGBM rank → top_k (default, no MI).
        "mi"    — zv → MI rank → correlation pruning → top_k (original path).
        "mi_then_lgbm" — zv → MI → correlation → LGBM re-rank → top_k (original use_lgbm=True).

    MI is only run when ``screen_method`` is "mi" or "mi_then_lgbm". LGBM stage must
    only be called on training data (anti-leakage, SSOT §8.2-D / TRN-09).

    When ``train_path`` or ``train_df`` is provided, zero-variance (zv) is computed via
    DuckDB (stddev_pop) on the full train to avoid pandas X.std() OOM; ``feature_matrix``
    is then used only for correlation/MI/LGBM (typically a sample).

    Parameters
    ----------
    feature_matrix : DataFrame
        Feature values (rows = observations, columns = feature candidates).
    labels : Series
        Binary labels aligned with ``feature_matrix``.
    feature_names : list[str]
        Subset of ``feature_matrix.columns`` to consider.
    corr_threshold : float
        Pearson |r| above which a feature is considered redundant.
    top_k : int | None | (unset)
        Max features to return; unset falls back to ``SCREEN_FEATURES_TOP_K``.
    use_lgbm : bool
        [Backward compat] If True and screen_method=="lgbm", treated as "mi_then_lgbm".
    screen_method : str
        "lgbm" | "mi" | "mi_then_lgbm". Default from config ``SCREEN_FEATURES_METHOD``.
    random_state : int
        Random seed for reproducibility.
    train_path : Path or None
        If set, path to train Parquet; used for DuckDB std (zv) on full data.
    train_df : DataFrame or None
        If set (and train_path not set), full train DataFrame for DuckDB std (zv).

    Returns
    -------
    list[str]
        Screened feature names (MI- or LGBM-ordered depending on method).
    """
    # Backward compat: legacy use_lgbm=True with default method → mi_then_lgbm
    if use_lgbm and screen_method == "lgbm":
        screen_method = "mi_then_lgbm"
    if screen_method not in ("lgbm", "mi", "mi_then_lgbm"):
        raise ValueError(
            f"screen_features: screen_method must be one of 'lgbm', 'mi', 'mi_then_lgbm', got {screen_method!r}"
        )

    # DEC-020: resolve top_k from config when caller did not supply it.
    effective_top_k: Optional[int] = (
        SCREEN_FEATURES_TOP_K if top_k is _SCREEN_TOP_K_UNSET else top_k  # type: ignore[assignment]
    )
    if effective_top_k is not None:
        if not isinstance(effective_top_k, (int, float)):
            try:
                effective_top_k = int(effective_top_k)
            except (TypeError, ValueError):
                # Config or caller may pass a mock/sentinel; treat as no limit (None).
                effective_top_k = None
        if effective_top_k is not None and effective_top_k < 1:
            raise ValueError(
                f"screen_features: top_k must be a positive integer or None, got {effective_top_k!r}"
            )

    # Zero-variance: use DuckDB on full train when provided to avoid X.std() OOM (PLAN Step 8 DuckDB 算統計量).
    use_duckdb_std = (train_path is not None or train_df is not None) and len(feature_names) > 0
    if use_duckdb_std:
        try:
            if train_path is not None:
                cols_std = feature_names
                if cols_std:
                    std = compute_column_std_duckdb(cols_std, path=train_path)
                    nonzero = std[std > 0].index.tolist()
                else:
                    std = pd.Series(dtype=float)
                    nonzero = []
            else:
                assert train_df is not None
                cols_std = [c for c in feature_names if c in train_df.columns]
                if cols_std:
                    std = compute_column_std_duckdb(cols_std, df=train_df[cols_std])
                    nonzero = std[std > 0].index.tolist()
                else:
                    std = pd.Series(dtype=float)
                    nonzero = []
            nonzero = [c for c in nonzero if c in feature_matrix.columns]
            if cols_std:
                logger.info(
                    "screen_features: std via DuckDB (path=%s, df=%s); %d nonzero-variance",
                    train_path is not None,
                    train_df is not None,
                    len(nonzero),
                )
        except Exception as exc:
            logger.warning(
                "screen_features: DuckDB std failed, falling back to pandas on feature_matrix: %s",
                exc,
            )
            use_duckdb_std = False

    if not use_duckdb_std:
        X = feature_matrix[feature_names].copy()
        coerce_feature_dtypes(X, list(X.columns))
        std = X.std()
        nonzero = std[std > 0].index.tolist()

    dropped_zv = len(feature_names) - len(nonzero)
    if dropped_zv:
        logger.info("screen_features: dropped %d zero-variance features", dropped_zv)
    X = feature_matrix[[c for c in nonzero if c in feature_matrix.columns]].copy()
    if not X.empty:
        coerce_feature_dtypes(X, list(X.columns))
    if X.empty:
        logger.warning(
            "screen_features: all features are zero-variance/NaN — returning empty list"
        )
        return []
    X_safe = X.fillna(0)

    # Optional DuckDB correlation matrix (PLAN Step 8 Phase 2: wire CORR into screen_features).
    # When train_path/train_df is set we compute corr once for nonzero; use submatrix for MI path.
    # len(nonzero) <= 1: skip DuckDB corr; _correlation_prune returns immediately.
    corr_matrix_duckdb: Optional[pd.DataFrame] = None
    if use_duckdb_std and len(nonzero) > 1:
        try:
            import duckdb
            _corr_exc_types: tuple = (ValueError, OSError, duckdb.Error)
        except ImportError:
            _corr_exc_types = (ValueError, OSError)
        try:
            if train_path is not None:
                corr_matrix_duckdb = compute_correlation_matrix_duckdb(nonzero, path=train_path)
            else:
                assert train_df is not None
                cols_corr = [c for c in nonzero if c in train_df.columns]
                if cols_corr:
                    corr_matrix_duckdb = compute_correlation_matrix_duckdb(
                        cols_corr, df=train_df[cols_corr]
                    )
            if corr_matrix_duckdb is not None and not corr_matrix_duckdb.empty:
                logger.info(
                    "screen_features: correlation via DuckDB (path=%s, df=%s); %d×%d matrix",
                    train_path is not None,
                    train_df is not None,
                    len(corr_matrix_duckdb.index),
                    len(corr_matrix_duckdb.columns),
                )
        except _corr_exc_types as exc:
            logger.warning(
                "screen_features: DuckDB correlation failed, falling back to pandas: %s",
                exc,
            )
            corr_matrix_duckdb = None

    def _correlation_prune(
        ordered_names: List[str],
        x: pd.DataFrame,
        corr_matrix: Optional[pd.DataFrame] = None,
    ) -> List[str]:
        """Drop features that are highly correlated (|r| > corr_threshold) with one already kept.

        When corr_matrix is provided (e.g. from DuckDB), it may cover only a subset of
        ordered_names (e.g. df mode when train_df is missing some columns); if any
        ordered_names are missing from corr_matrix, we fall back to x[ordered_names].corr().abs().
        Caller must ensure ordered_names is a subset of x.columns when the fallback path is used.
        """
        if len(ordered_names) <= 1:
            return ordered_names
        if corr_matrix is not None and not corr_matrix.empty:
            # Use precomputed matrix (e.g. from DuckDB); take submatrix for ordered_names.
            # When corr_matrix index/columns do not cover ordered_names, fall back to pandas.
            missing = [c for c in ordered_names if c not in corr_matrix.index or c not in corr_matrix.columns]
            if missing:
                corr = x[ordered_names].corr().abs()
            else:
                # Missing cells filled with 0.0 (no correlation). Pruning uses upper triangle only.
                corr = corr_matrix.reindex(index=ordered_names, columns=ordered_names, fill_value=0.0).astype(float)
        else:
            corr = x[ordered_names].corr().abs()
        upper = corr.where(np.triu(np.ones(corr.shape, dtype=bool), k=1))
        to_drop = set()
        for col in upper.columns:
            if col in to_drop:
                continue
            highly_corr = upper.index[upper[col] > corr_threshold].tolist()
            if any(c not in to_drop for c in highly_corr):
                to_drop.add(col)
        out = [c for c in ordered_names if c not in to_drop]
        logger.info(
            "screen_features: %d features after correlation pruning (threshold=%.2f)",
            len(out), corr_threshold,
        )
        return out

    def _lgbm_rank_and_cap(names: List[str]) -> List[str]:
        try:
            import lightgbm as lgb  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "lightgbm is required for LGBM feature screening. pip install lightgbm"
            ) from exc
        dtrain = lgb.Dataset(X_safe[names], label=labels)
        # Step 8 screening: CPU-only (GPU plan Phase A — avoid GPU context churn on small matrices).
        params = {
            "objective": "binary",
            "verbosity": -1,
            "num_leaves": 31,
            "seed": random_state,
            "device_type": "cpu",
            "force_col_wise": True,
            "n_jobs": -1,
        }
        model = lgb.train(params, dtrain, num_boost_round=100)
        importance = pd.Series(
            model.feature_importance(importance_type="split"), index=names
        ).sort_values(ascending=False)
        if effective_top_k is not None:
            out = importance.head(effective_top_k).index.tolist()
            logger.info("screen_features: %d features after LightGBM screening (top_k=%d)", len(out), effective_top_k)
        else:
            out = importance.index.tolist()
            logger.info("screen_features: %d features after LightGBM screening (no cap)", len(out))
        return out

    if screen_method == "lgbm":
        candidates = _correlation_prune(nonzero, X_safe, corr_matrix=corr_matrix_duckdb)
        return _lgbm_rank_and_cap(candidates)

    # "mi" or "mi_then_lgbm": run mutual information
    from sklearn.feature_selection import mutual_info_classif  # type: ignore[import]

    mi = mutual_info_classif(
        X_safe, labels, discrete_features=False, random_state=random_state
    )
    mi_df = pd.Series(mi, index=nonzero).sort_values(ascending=False)
    candidates = mi_df.index.tolist()
    logger.info("screen_features: %d candidates after MI ranking", len(candidates))
    # Submatrix of DuckDB corr for MI-ranked candidates (all are in nonzero).
    corr_sub = None
    if corr_matrix_duckdb is not None and not corr_matrix_duckdb.empty:
        if set(candidates).issubset(corr_matrix_duckdb.index) and set(candidates).issubset(corr_matrix_duckdb.columns):
            corr_sub = corr_matrix_duckdb.loc[candidates, candidates].copy()
    candidates = _correlation_prune(candidates, X_safe, corr_matrix=corr_sub)

    if screen_method == "mi":
        if effective_top_k is not None:
            candidates = candidates[:effective_top_k]
            logger.info("screen_features: capped to top_k=%d (Stage 1)", effective_top_k)
        return candidates

    # mi_then_lgbm
    return _lgbm_rank_and_cap(candidates)


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
        logger.debug("join_player_profile: profile_df absent/empty — profile features are NaN")
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

    # R74: keep NaN for unmatched bets (non-rated or before first snapshot) —
    # LightGBM routes NaN to the trained default-child, which is semantically
    # correct; zero-fill would conflate "no data" with "zero activity".
    # Do not sort merged by _orig_idx here: the scatter below uses _orig_idx as
    # index and reindex(), so row order of merged is irrelevant. Skipping the
    # sort avoids a ~10 GiB allocation on large chunks (e.g. 90-day training)
    # and prevents ArrayMemoryError in join_player_profile.

    # R800: when NaT rows were dropped before merge_asof, merged has fewer rows
    # than result.  Use _orig_idx to scatter values back into the correct positions;
    # dropped rows retain their NaN-initialised values (set above).
    for col in available_cols:
        _vals = pd.Series(merged[col].values, index=merged["_orig_idx"].values)
        result[col] = _vals.reindex(np.arange(len(result))).values

    n_rated_with_profile = int(pd.notna(result[available_cols[0]]).sum())
    logger.debug(
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

    # R112-2: Build allowed function whitelist (aggregate + window).  Applied only to
    # window/transform/lag type candidates; derived and passthrough are exempt.
    _DEFAULT_ALLOWED_AGG: set = {"COUNT", "SUM", "AVG", "MIN", "MAX", "STDDEV_SAMP"}
    _DEFAULT_ALLOWED_WIN: set = {"LAG"}
    yaml_agg = {f.upper() for f in (yaml_guardrails.get("allowed_aggregate_functions") or [])}
    yaml_win = {f.upper() for f in (yaml_guardrails.get("allowed_window_functions") or [])}
    yaml_scalar = {f.upper() for f in (yaml_guardrails.get("allowed_scalar_functions") or [])}
    allowed_funcs: set = _DEFAULT_ALLOWED_AGG | _DEFAULT_ALLOWED_WIN | yaml_agg | yaml_win | yaml_scalar
    _FUNC_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

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
                ftype = cand.get("type", "window")

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

                # R112-2: For window/transform/lag types with a non-empty expression,
                # every function call must appear in the allowed-functions whitelist.
                # Derived and passthrough types are exempt (derived references column
                # names produced by preceding window expressions; passthrough has no
                # expression).
                if ftype in ("window", "transform", "lag") and expr:
                    found_funcs = {
                        m.upper() for m in _FUNC_CALL_RE.findall(expr)
                    }
                    unknown = found_funcs - allowed_funcs
                    if unknown:
                        errors.append(
                            f"[track_llm] '{fid}': expression uses function(s) not in "
                            f"allowed_aggregate_functions / allowed_window_functions: "
                            f"{sorted(unknown)} in {expr!r}"
                        )

                # postprocess.clip min/max must be int or float (avoids str vs float
                # in pandas clip() which raises "'<' not supported between str and float").
                clip_spec = (cand.get("postprocess") or {}).get("clip") or {}
                for bound_key in ("min", "max"):
                    if bound_key not in clip_spec:
                        continue
                    val = clip_spec[bound_key]
                    if val is None:
                        continue
                    if not isinstance(val, (int, float)):
                        errors.append(
                            f"[track_llm] '{fid}': postprocess.clip.{bound_key} must be "
                            f"int or float, got {type(val).__name__}: {val!r}"
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
    - **DEC-031**: each candidate feature column is stored as ``float32`` after
      postprocess to cut peak RAM on downstream ``merge`` / training paths.
    - DuckDB 1.x is required (``RANGE BETWEEN INTERVAL … PRECEDING`` syntax).
    """
    import duckdb

    try:
        from trainer.duckdb_schema import prepare_bets_for_duckdb
    except ModuleNotFoundError:
        from duckdb_schema import prepare_bets_for_duckdb  # type: ignore[import-not-found,no-redef]

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
        # Coerce to datetime so we never compare str vs float (Parquet/object mixed types).
        ts_for_mask = pd.to_datetime(bets_df["payout_complete_dtm"], errors="coerce")
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
            df[cand["feature_id"]] = pd.Series(dtype="float32")
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
    # Use uniform string keys for sort to avoid "'<' not supported between
    # instances of 'str' and 'float'" when columns have mixed types from Parquet.
    _cid = df["canonical_id"].astype(str)
    _bid = df["bet_id"].astype(str)
    df = df.assign(_sort_cid=_cid, _sort_bid=_bid)
    df = df.sort_values(
        ["_sort_cid", "payout_complete_dtm", "_sort_bid"], kind="stable"
    ).drop(columns=["_sort_cid", "_sort_bid"]).reset_index(drop=True)
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
        elif ftype == "passthrough":
            # R112-1: raw column pass-through — select the column directly without
            # any computation.  The column must already exist in bets_df so that
            # DuckDB can resolve it from the registered "bets" table.
            sql_expr = f'"{fid}" AS "{fid}"'
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
    # Diagnostic: log dtype and type distribution for ORDER BY columns (str vs float).
    for col in ("canonical_id", "bet_id"):
        if col in df_for_duckdb.columns:
            dtype = df_for_duckdb[col].dtype
            type_counts = df_for_duckdb[col].apply(type).value_counts().to_dict()
            type_counts_str = {k.__name__: int(v) for k, v in type_counts.items()}
            logger.debug(
                "compute_track_llm_features: %s dtype=%s type_dist=%s",
                col,
                dtype,
                type_counts_str,
            )
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

    # DEC-031: float32 for all candidate feature columns (numeric only).
    for cand in candidates:
        fid = cand["feature_id"]
        if fid not in result_df.columns:
            continue
        ser = result_df[fid]
        if pd.api.types.is_numeric_dtype(ser):
            result_df[fid] = ser.astype(np.float32)

    logger.debug(
        "compute_track_llm_features: computed %d Track LLM features for %d bets",
        len(candidates),
        len(result_df),
    )
    return result_df
