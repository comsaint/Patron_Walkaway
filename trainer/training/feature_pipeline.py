"""trainer/training/feature_pipeline.py
========================================
Feature-pipeline coordination helpers extracted from
``trainer/training/trainer.py`` (Issue #12 PR-12.3).

Scope
-----
This module owns the *coordination layer* between raw bet/session input and
the per-row feature columns expected by the trainer/backtester/scorer:

* ``apply_dq`` — FND-01 / FND-02 / FND-04 + R23 / DEC-018 timezone & DQ guards.
* ``add_run_state_machine_features`` — run_state_machine run-boundary features
  wired onto the bets DataFrame (bet-level streak / table HC are spec-disabled).

This was originally a pure refactor extraction with **zero behavior change**;
engine selection for run boundaries (pandas vs optional DuckDB) and Phase 1
performance tweaks are controlled from ``trainer.core._config_training_memory``.

Notes on configuration
----------------------
``PLACEHOLDER_PLAYER_ID`` and the HK timezone (``HK_TZ``) are resolved from
``trainer.config`` (with the legacy top-level ``config`` fallback used by
``trainer.py``). Feature primitive ``compute_run_boundary`` is imported from
``trainer.features`` so this module remains a thin coordinator with no
duplicated state-machine logic.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import config as _cfg  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

try:
    from trainer.features import (  # type: ignore[import]
        compute_run_boundary,
        compute_run_boundary_duckdb,
        run_boundary_frames_close,
    )
except ModuleNotFoundError:
    from features import (  # type: ignore[import]
        compute_run_boundary,
        compute_run_boundary_duckdb,
        run_boundary_frames_close,
    )

logger = logging.getLogger(__name__)

PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
HK_TZ = ZoneInfo(getattr(_cfg, "HK_TZ", "Asia/Hong_Kong"))


def apply_dq(
    bets: pd.DataFrame,
    sessions: pd.DataFrame,
    window_start: datetime,
    extended_end: datetime,
    bets_history_start: Optional[datetime] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Apply data quality filters.  Returns (bets_clean, sessions_clean).

    Parameters
    ----------
    bets_history_start:
        If provided, bets are kept from this point (< window_start) to give
        Track Human state machines cross-chunk context.  Defaults to window_start.

    Notes
    -----
    When ``bets`` is empty (e.g. sessions-only DQ path used when building the
    canonical mapping), the bets processing block is skipped entirely and only
    session DQ filters are applied.  This avoids a ``KeyError`` on
    ``payout_complete_dtm`` when a caller passes a stub DataFrame.
    """
    # --- sessions (FND-01 / FND-02 / FND-04) — applied first so that the
    # bets.empty early-return path still yields clean session data.
    session_dt_cols: Dict[str, pd.Series] = {}
    for dt_col in ("session_start_dtm", "session_end_dtm", "lud_dtm"):
        if dt_col in sessions.columns:
            session_dt_cols[dt_col] = pd.to_datetime(
                sessions[dt_col], utc=False, errors="coerce"
            )

    session_id_num = pd.to_numeric(
        sessions["session_id"] if "session_id" in sessions.columns else pd.Series(np.nan, index=sessions.index),
        errors="coerce",
    )
    player_id_num = pd.to_numeric(
        sessions["player_id"] if "player_id" in sessions.columns else pd.Series(np.nan, index=sessions.index),
        errors="coerce",
    )
    _valid_session_id_mask = session_id_num.notna()
    sessions = sessions.loc[_valid_session_id_mask].copy()
    for dt_col, normalized in session_dt_cols.items():
        sessions[dt_col] = normalized.loc[_valid_session_id_mask].to_numpy()
    sessions["session_id"] = session_id_num.loc[_valid_session_id_mask].to_numpy()
    sessions["player_id"] = player_id_num.loc[_valid_session_id_mask].to_numpy()

    # FND-01 dedup: keep latest record per session_id (lud_dtm DESC, then
    # __etl_insert_Dtm DESC as tiebreaker — mirrors identity._fnd01_dedup_pandas) (R39)
    sort_keys = [k for k in ("lud_dtm", "__etl_insert_Dtm") if k in sessions.columns]
    if sort_keys:
        sessions = sessions.sort_values(sort_keys, ascending=False)
    sessions = sessions.drop_duplicates(subset=["session_id"], keep="first")

    # Ensure sentinel columns exist before filtering
    if "num_games_with_wager" not in sessions.columns:
        sessions["num_games_with_wager"] = 0
    for flag in ("is_manual", "is_deleted", "is_canceled"):
        if flag not in sessions.columns:
            sessions[flag] = 0

    # FND-02 + FND-04 (A10): single combined mask then one .copy().
    # FND-02: exclude manual adjustment sessions and soft-deleted rows.
    dq_mask = (
        (sessions["is_manual"] == 0)
        & (sessions["is_deleted"] == 0)
        & (sessions["is_canceled"] == 0)
    )
    # FND-04: exclude ghost sessions with no real wager activity (SSOT §5).
    if "turnover" in sessions.columns or "num_games_with_wager" in sessions.columns:
        _turnover = sessions.get(
            "turnover", pd.Series(0.0, index=sessions.index)
        ).fillna(0)
        _games = sessions["num_games_with_wager"].fillna(0)
        dq_mask = dq_mask & ((_turnover > 0) | (_games > 0))
    sessions = sessions.loc[dq_mask].copy()

    if bets.empty:
        # Sessions-only path — return clean sessions, skip bets processing entirely.
        # This avoids a KeyError on payout_complete_dtm when called with a stub DataFrame.
        return bets, sessions

    # --- bets ---
    payout_complete_dtm = pd.to_datetime(bets["payout_complete_dtm"], utc=False)

    # R23: Timezone normalisation — tz_localize naive, tz_convert aware to HK,
    # then strip tz so downstream callers (labels, features) receive tz-naive
    # HK local time and no naive/aware TypeError can occur at the boundary.
    if payout_complete_dtm.dt.tz is None:
        payout_complete_dtm = payout_complete_dtm.dt.tz_localize(
            HK_TZ, nonexistent="shift_forward", ambiguous="NaT"
        )
    else:
        payout_complete_dtm = payout_complete_dtm.dt.tz_convert(HK_TZ)
    # Strip tz after normalization — downstream (compute_labels, features) is tz-naive.
    payout_complete_dtm = payout_complete_dtm.dt.tz_localize(None)
    # DEC-018: unify datetime resolution to ns so merge_asof / comparisons always see
    # the same dtype regardless of Parquet file's stored precision ([ms] vs [us]).
    payout_complete_dtm = payout_complete_dtm.astype("datetime64[ns]")

    # Boundary comparison — both sides are tz-naive after DEC-018 process_chunk strip.
    # The explicit .replace(tzinfo=None) guards here are kept as a defensive fallback
    # for callers that bypass process_chunk (e.g. backtester, tests).
    _lo = bets_history_start if bets_history_start is not None else window_start
    _lo = _lo.replace(tzinfo=None) if getattr(_lo, "tzinfo", None) else _lo
    _hi = extended_end.replace(tzinfo=None) if getattr(extended_end, "tzinfo", None) else extended_end

    # Key numeric only; table_id is categorical after normalizer (PLAN § apply_dq 配合修改).
    numeric_key_cols: Dict[str, pd.Series] = {}
    for col in ("bet_id", "session_id", "player_id"):
        if col in bets.columns:
            numeric_key_cols[col] = pd.to_numeric(bets.get(col), errors="coerce")

    # Build the keep-mask from normalized Series first, then copy only surviving rows.
    # This avoids an eager full-frame copy of the Step 6 bets chunk before we know
    # which rows survive DQ.
    _dq_mask = (
        payout_complete_dtm.between(_lo, _hi, inclusive="left")
        & payout_complete_dtm.notna()
        & numeric_key_cols["bet_id"].notna()
        & numeric_key_cols["session_id"].notna()
    )
    if "wager" in bets.columns:
        # Defense-in-depth wager guard (R1602): applied inside the combined mask.
        _dq_mask &= bets["wager"].fillna(0).gt(0)
    bets = bets.loc[_dq_mask].copy().reset_index(drop=True)
    bets["payout_complete_dtm"] = payout_complete_dtm.loc[_dq_mask].to_numpy()
    for col, coerced in numeric_key_cols.items():
        bets[col] = coerced.loc[_dq_mask].to_numpy()

    # G2: recover invalid/missing player_id from session player_id before the
    # E4/F1 drop (SSOT §5 G2 — COALESCE t_bet.player_id, t_session.player_id).
    if "player_id" in bets.columns and "session_id" in bets.columns:
        invalid_mask = bets["player_id"].isna() | (bets["player_id"] == PLACEHOLDER_PLAYER_ID)
        if invalid_mask.any():
            _valid_sess = sessions[
                sessions["player_id"].notna()
                & (sessions["player_id"] != PLACEHOLDER_PLAYER_ID)
            ].drop_duplicates(subset=["session_id"])
            _sess_pid = _valid_sess.set_index("session_id")["player_id"].to_dict()
            _recovered = bets.loc[invalid_mask, "session_id"].map(_sess_pid)
            _good = _recovered.notna() & (_recovered != PLACEHOLDER_PLAYER_ID)
            if _good.any():
                bets.loc[_good[_good].index, "player_id"] = _recovered[_good]

    # E4/F1: drop remaining invalid player_id rows as final defense-in-depth guard (R37/R1100)
    if "player_id" in bets.columns:
        bets = bets[
            bets["player_id"].notna()
            & (bets["player_id"] != PLACEHOLDER_PLAYER_ID)
        ].reset_index(drop=True)

    # gaming_day: t_bet contract — must come from source; no derivation/fallback.
    if "gaming_day" not in bets.columns:
        raise ValueError(
            "apply_dq: missing required column 'gaming_day' (expected from t_bet; no fallback)"
        )
    if not bets.empty and bets["gaming_day"].isna().any():
        raise ValueError(
            f"apply_dq: gaming_day must be non-null on all bets "
            f"(found {int(bets['gaming_day'].isna().sum())} null rows)"
        )

    # Ensure status column exists (for loss_streak)
    if "status" not in bets.columns:
        bets["status"] = None

    # Numeric guard for legacy features; skip columns already categorical (PLAN § apply_dq 配合修改).
    for col in ("wager", "payout_odds", "base_ha", "is_back_bet", "position_idx", "casino_win"):
        if col not in bets.columns:
            continue
        if isinstance(bets[col].dtype, pd.CategoricalDtype):
            continue
        bets[col] = pd.to_numeric(bets[col], errors="coerce").fillna(0)

    # DEC-018 / R23 contract assertion: payout_complete_dtm must leave apply_dq tz-naive.
    if not bets.empty and "payout_complete_dtm" in bets.columns:
        assert bets["payout_complete_dtm"].dt.tz is None, \
            "R23 violation: payout_complete_dtm must be tz-naive after DQ"

    return bets, sessions


def _run_index_assignable(df: pd.DataFrame) -> bool:
    """Return True when each row is uniquely addressed by RangeIndex(0..n-1)."""
    if not isinstance(df.index, pd.RangeIndex):
        return False
    if df.index.step != 1 or int(df.index.start) != 0:
        return False
    if df.index.has_duplicates:
        return False
    return int(df.index.stop) == len(df)


def _assign_run_feature_columns(df: pd.DataFrame, run_df: pd.DataFrame) -> None:
    """Project *run_df* run-boundary columns back onto *df* row order.

    Uses numpy scatter when both frames use the default RangeIndex layout after DQ;
    falls back to pandas ``reindex`` for exotic indices.
    """
    layout = (
        ("run_id", np.int32, 0),
        ("minutes_since_run_start", np.float64, 0.0),
        ("bets_in_run_so_far", np.int32, 0),
        ("wager_sum_in_run_so_far", np.float64, 0.0),
        ("net_win_in_run_so_far", np.float64, 0.0),
        ("net_win_per_bet_in_run", np.float64, 0.0),
    )
    idx = run_df.index.to_numpy()
    if (
        _run_index_assignable(df)
        and idx.size > 0
        and int(idx.min()) >= 0
        and int(idx.max()) < len(df)
    ):
        for name, np_dt, fill in layout:
            base = np.full(len(df), fill, dtype=np_dt)
            raw = run_df[name].to_numpy(copy=False)
            base[idx] = raw.astype(np_dt, copy=False)
            df[name] = base
        return
    for name, _, fill in layout:
        df[name] = run_df[name].reindex(df.index, fill_value=fill).to_numpy()


def _dispatch_compute_run_boundary(bets: pd.DataFrame, window_end: datetime) -> pd.DataFrame:
    """Resolve RUN_BOUNDARY_ENGINE (pandas vs duckdb) with optional parity + pandas fallback."""
    eng = str(getattr(_cfg, "RUN_BOUNDARY_ENGINE", "pandas")).strip().lower()
    parity = bool(getattr(_cfg, "RUN_BOUNDARY_PARITY_CHECK", False))
    max_rows = int(getattr(_cfg, "RUN_BOUNDARY_PARITY_MAX_ROWS", 500_000))

    if eng == "duckdb":
        try:
            duck = compute_run_boundary_duckdb(bets, cutoff_time=window_end, lookback_hours=None)
            if parity and len(bets) <= max_rows:
                pan = compute_run_boundary(bets, cutoff_time=window_end, lookback_hours=None)
                if not run_boundary_frames_close(duck, pan):
                    logger.error(
                        "RUN_BOUNDARY duckdb parity mismatch vs pandas (rows=%d); using pandas",
                        len(bets),
                    )
                    return pan
            return duck
        except Exception as exc:
            logger.warning("RUN_BOUNDARY duckdb failed (%s); using pandas fallback", exc)
            return compute_run_boundary(bets, cutoff_time=window_end, lookback_hours=None)
    if eng != "pandas":
        logger.warning("RUN_BOUNDARY_ENGINE=%r invalid; using pandas", eng)
    return compute_run_boundary(bets, cutoff_time=window_end, lookback_hours=None)


def add_run_state_machine_features(
    bets: pd.DataFrame,
    canonical_map: pd.DataFrame,
    window_end: datetime,
    lookback_hours: Optional[float] = None,
) -> pd.DataFrame:
    """Return a copy of *bets* with run_state_machine feature columns attached.

    A copy is taken so the caller's DataFrame is not mutated.

    Bet-level streak / ``table_hc`` are no longer computed here (feature-spec
    disabled); columns are zero-filled for backward compatibility.  Run-boundary
    primitives use :func:`compute_run_boundary` (gap + ``gaming_day``; no
    lookback window). Engine selection is controlled by ``RUN_BOUNDARY_ENGINE``
    in ``trainer.core._config_training_memory`` (pandas default, duckdb optional).
    """
    del canonical_map
    if lookback_hours is not None:
        raise ValueError(
            "add_run_state_machine_features: lookback_hours is no longer supported (must be None)"
        )
    df = bets.copy()

    if "canonical_id" not in df.columns:
        logger.warning("canonical_id missing; Track Human features will be zeros")
        df["loss_streak"] = 0
        df["consecutive_non_win_cnt"] = 0
        df["run_id"] = 0
        df["minutes_since_run_start"] = 0.0
        df["bets_in_run_so_far"] = 0
        df["wager_sum_in_run_so_far"] = 0.0
        df["net_win_in_run_so_far"] = 0.0
        df["net_win_per_bet_in_run"] = 0.0
        df["table_hc"] = np.int32(0)
        return df

    df["loss_streak"] = np.int32(0)
    df["consecutive_non_win_cnt"] = np.int32(0)
    df["table_hc"] = np.int32(0)

    run_df = _dispatch_compute_run_boundary(df, window_end)
    _assign_run_feature_columns(df, run_df)

    return df


