"""trainer/etl_player_profile.py — player_profile_daily daily snapshot ETL
===========================================================================
Builds one row per (canonical_id, snapshot_date) by aggregating cleaned
t_session records over 7 / 30 / 90 / 180 / 365-day lookback windows.

Usage
-----
# Run for today (HK time)
python etl_player_profile.py

# Run for a specific date
python etl_player_profile.py --snapshot-date 2026-02-28

# Backfill a range
python etl_player_profile.py --start-date 2026-01-01 --end-date 2026-02-28

# Dry-run: write to local Parquet instead of ClickHouse
python etl_player_profile.py --local-parquet

Pipeline
--------
1. Load clean t_session records via ClickHouse (or local Parquet dev path).
2. Join D2 canonical_id mapping (from identity.py).
3. Exclude FND-12 dummy players (total games_with_wager <= 1).
4. Apply session availability gate: COALESCE(session_end_dtm, lud_dtm) +
   SESSION_AVAIL_DELAY_MIN minutes <= snapshot_dtm.
5. For each canonical_id compute all Phase 1 profile columns
   (see doc/player_profile_daily_spec.md).
6. Derive ratio columns from pre-computed window aggregates.
7. Write result to player_profile_daily (ClickHouse INSERT or local Parquet).

Dependencies
------------
- identity.py: build_canonical_mapping()
- db_conn.py: get_clickhouse_client()
- config.py: SOURCE_DB, TPROFILE, SESSION_AVAIL_DELAY_MIN, HK_TZ
"""
from __future__ import annotations

import argparse
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

try:
    import config  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as config  # type: ignore[import, no-redef]

try:
    from db_conn import get_clickhouse_client  # type: ignore[import]
except ImportError:
    get_clickhouse_client = None  # type: ignore[assignment]

try:
    from identity import build_canonical_mapping  # type: ignore[import]
except ImportError:
    from trainer.identity import build_canonical_mapping  # type: ignore[import, attr-defined]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent
LOCAL_PARQUET_DIR = PROJECT_ROOT / "data"
LOCAL_PROFILE_PARQUET = LOCAL_PARQUET_DIR / "player_profile_daily.parquet"

HK_TZ = ZoneInfo(config.HK_TZ)
SOURCE_DB: str = getattr(config, "SOURCE_DB", "GDP_GMWDS_Raw")
TSESSION: str = getattr(config, "TSESSION", "t_session")
TPROFILE: str = getattr(config, "TPROFILE", "player_profile_daily")
SESSION_AVAIL_DELAY_MIN: int = getattr(config, "SESSION_AVAIL_DELAY_MIN", 7)
PROFILE_VERSION = "v1.0"

# Maximum lookback window to load for each snapshot run
MAX_LOOKBACK_DAYS = 365

# ---------------------------------------------------------------------------
# Session columns we need from t_session
# ---------------------------------------------------------------------------
_SESSION_COLS = [
    "session_id",
    "player_id",
    "session_start_dtm",
    "session_end_dtm",
    "lud_dtm",
    "gaming_day",
    "table_id",
    "pit_name",
    "gaming_area",
    "turnover",
    "player_win",
    "theo_win",
    "num_bets",
    "num_games_with_wager",
    "buyin",
    "is_manual",
    "is_deleted",
    "is_canceled",
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_sessions(snapshot_dtm: datetime, client) -> pd.DataFrame:
    """Load DQ-clean t_session rows covering MAX_LOOKBACK_DAYS before snapshot_dtm.

    Applies:
    - FND-01 dedup (ROW_NUMBER OVER PARTITION BY session_id)
    - FND-02: is_manual=0, is_deleted=0, is_canceled=0
    - FND-04: turnover > 0 OR num_games_with_wager > 0
    - Session availability gate (SSOT §4.2)
    """
    lo_dtm = snapshot_dtm - timedelta(days=MAX_LOOKBACK_DAYS + 30)  # buffer for lag
    avail_delay = SESSION_AVAIL_DELAY_MIN

    # R87: explicit column projection — avoids accidental schema drift
    _inner_cols = ", ".join(f"s.{c}" for c in _SESSION_COLS)
    _outer_cols = ", ".join(_SESSION_COLS)
    query = f"""
        WITH deduped AS (
            SELECT
                {_inner_cols},
                ROW_NUMBER() OVER (PARTITION BY s.session_id ORDER BY s.lud_dtm DESC) AS rn
            FROM {SOURCE_DB}.{TSESSION} AS s
            WHERE COALESCE(s.session_end_dtm, s.lud_dtm) >= %(lo_dtm)s
              AND COALESCE(s.session_end_dtm, s.lud_dtm)
                  + INTERVAL {avail_delay} MINUTE <= %(snap_dtm)s
        )
        SELECT {_outer_cols}
        FROM deduped
        WHERE rn = 1
          AND is_manual = 0
          AND is_deleted = 0
          AND is_canceled = 0
          AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
    """
    df = client.query_df(
        query, parameters={"lo_dtm": lo_dtm, "snap_dtm": snapshot_dtm}
    )
    logger.info("_load_sessions: %d rows loaded from ClickHouse", len(df))
    return df


def _load_sessions_local(snapshot_dtm: datetime) -> Optional[pd.DataFrame]:
    """Dev fallback: load t_session from a local Parquet export and apply DQ filters."""
    t_session_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    if not t_session_path.exists():
        return None
    try:
        lo_dtm = snapshot_dtm - timedelta(days=MAX_LOOKBACK_DAYS + 30)
        df = pd.read_parquet(t_session_path)

        def _naive(dt: datetime) -> pd.Timestamp:
            ts = pd.Timestamp(dt)
            return ts.tz_localize(None) if ts.tzinfo is None else ts.replace(tzinfo=None)

        snap_ts = _naive(snapshot_dtm)
        avail_delay = SESSION_AVAIL_DELAY_MIN
        # Compute available time
        sess_end = pd.to_datetime(df.get("session_end_dtm", pd.NaT))
        lud = pd.to_datetime(df.get("lud_dtm", pd.NaT))
        avail_time = sess_end.fillna(lud) + pd.Timedelta(minutes=avail_delay)
        if avail_time.dt.tz is not None:
            avail_time = avail_time.dt.tz_localize(None)

        mask = (
            (avail_time >= _naive(lo_dtm))
            & (avail_time <= snap_ts)
            & (df.get("is_manual", 0) == 0)
            & (df.get("is_deleted", 0) == 0)
            & (df.get("is_canceled", 0) == 0)
            & (
                (df.get("turnover", 0).fillna(0) > 0)
                | (df.get("num_games_with_wager", 0).fillna(0) > 0)
            )
        )
        df = df[mask].drop_duplicates(subset=["session_id"])
        logger.info("_load_sessions_local: %d rows from local Parquet", len(df))
        return df
    except Exception as exc:
        logger.warning("Local session Parquet load failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# FND-12 dummy player exclusion
# ---------------------------------------------------------------------------

def _exclude_fnd12_dummies(sessions: pd.DataFrame) -> pd.DataFrame:
    """Remove canonical_ids whose total num_games_with_wager <= 1 (FND-12)."""
    if "canonical_id" not in sessions.columns:
        return sessions
    # R89: vectorized — fillna on the column first, then groupby sum (avoids apply/lambda)
    games_total = (
        sessions["num_games_with_wager"].fillna(0)
        .groupby(sessions["canonical_id"])
        .sum()
    )
    valid_ids = games_total[games_total > 1].index
    before = len(sessions["canonical_id"].unique())
    sessions = sessions[sessions["canonical_id"].isin(valid_ids)]
    after = len(sessions["canonical_id"].unique())
    logger.info("FND-12 exclusion: %d → %d canonical_ids", before, after)
    return sessions


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------

def _compute_profile(sessions: pd.DataFrame, snapshot_dtm: datetime) -> pd.DataFrame:
    """Compute all Phase 1 player_profile_daily columns for one snapshot_dtm.

    Parameters
    ----------
    sessions:
        Clean t_session rows with canonical_id column, covering up to
        MAX_LOOKBACK_DAYS before snapshot_dtm.
    snapshot_dtm:
        The as-of cutoff time for all aggregations.

    Returns
    -------
    DataFrame with one row per canonical_id plus all Phase 1 feature columns.
    """
    snap_ts = pd.Timestamp(snapshot_dtm)
    if snap_ts.tzinfo is not None:
        snap_ts = snap_ts.replace(tzinfo=None)

    # Normalise timestamps
    sessions = sessions.copy()
    sess_end = pd.to_datetime(sessions.get("session_end_dtm", pd.NaT))
    lud = pd.to_datetime(sessions.get("lud_dtm", pd.NaT))
    if sess_end.dt.tz is not None:
        sess_end = sess_end.dt.tz_localize(None)
    if lud.dt.tz is not None:
        lud = lud.dt.tz_localize(None)

    # session_date = COALESCE(session_end_dtm, lud_dtm)::date
    session_ts = sess_end.fillna(lud)
    sessions["_session_ts"] = session_ts
    sessions["_session_date"] = session_ts.dt.date

    # session_start for duration
    sess_start = pd.to_datetime(sessions.get("session_start_dtm", pd.NaT))
    if sess_start.dt.tz is not None:
        sess_start = sess_start.dt.tz_localize(None)
    sessions["_session_start"] = sess_start

    # Duration in minutes (only for sessions with both start and end)
    has_end = sess_end.notna()
    sessions["_duration_min"] = np.where(
        has_end,
        (sess_end - sessions["_session_start"]).dt.total_seconds() / 60.0,
        np.nan,
    )

    # Numeric columns with safe defaults
    for col in ["turnover", "player_win", "theo_win", "num_bets", "buyin"]:
        sessions[col] = pd.to_numeric(sessions.get(col, 0), errors="coerce").fillna(0.0)
    sessions["num_games_with_wager"] = pd.to_numeric(
        sessions.get("num_games_with_wager", 0), errors="coerce"
    ).fillna(0.0)
    sessions["_win_flag"] = (sessions["player_win"] > 0).astype(float)

    # Pre-compute window membership flags using _session_ts (R86: timestamp boundary
    # avoids same-day boundary ambiguity introduced by date-level comparisons)
    for days in (7, 30, 90, 180, 365):
        lo_ts = snap_ts - pd.Timedelta(days=days)
        sessions[f"_in_{days}d"] = sessions["_session_ts"] >= lo_ts

    def _w(days: int) -> pd.Series:
        return sessions[f"_in_{days}d"]

    grp = sessions.groupby("canonical_id")

    def _agg_window(col: str, agg: str, window_days: int) -> pd.Series:
        """Aggregate `col` with `agg` over `window_days`-day window."""
        sub = sessions[_w(window_days)]
        if sub.empty:
            return pd.Series(dtype="float64", name=col)
        if agg == "sum":
            return sub.groupby("canonical_id")[col].sum()
        elif agg == "count":
            return sub.groupby("canonical_id")[col].count()
        elif agg == "nunique":
            return sub.groupby("canonical_id")[col].nunique()
        elif agg == "mean":
            return sub.groupby("canonical_id")[col].mean()
        raise ValueError(f"Unknown agg: {agg}")

    result_parts: dict = {}

    # ── Recency ─────────────────────────────────────────────────────────────
    last_sess = grp["_session_date"].max()
    first_sess = grp["_session_date"].min()
    result_parts["days_since_last_session"] = (
        pd.to_datetime(snapshot_date) - pd.to_datetime(last_sess)
    ).dt.days.astype("float64")
    result_parts["days_since_first_session"] = (
        pd.to_datetime(snapshot_date) - pd.to_datetime(first_sess)
    ).dt.days.astype("float64")

    # ── Frequency ────────────────────────────────────────────────────────────
    for days in (7, 30, 90, 180, 365):
        result_parts[f"sessions_{days}d"] = _agg_window("session_id", "count", days)
    result_parts["active_days_30d"] = _agg_window("_session_date", "nunique", 30)
    result_parts["active_days_90d"] = _agg_window("_session_date", "nunique", 90)
    result_parts["active_days_365d"] = _agg_window("_session_date", "nunique", 365)

    # ── Monetary ─────────────────────────────────────────────────────────────
    for days in (7, 30, 90, 180, 365):
        result_parts[f"turnover_sum_{days}d"] = _agg_window("turnover", "sum", days)
    for days in (30, 90, 180, 365):
        result_parts[f"player_win_sum_{days}d"] = _agg_window("player_win", "sum", days)
    for days in (30, 180):
        result_parts[f"theo_win_sum_{days}d"] = _agg_window("theo_win", "sum", days)
        result_parts[f"num_bets_sum_{days}d"] = _agg_window("num_bets", "sum", days)
        result_parts[f"num_games_with_wager_sum_{days}d"] = _agg_window(
            "num_games_with_wager", "sum", days
        )

    # ── Bet intensity ────────────────────────────────────────────────────────
    # turnover_per_bet_mean = turnover_sum / num_bets_sum (per window)
    for days in (30, 180):
        t_sum = result_parts[f"turnover_sum_{days}d"]
        n_sum = result_parts[f"num_bets_sum_{days}d"]
        result_parts[f"turnover_per_bet_mean_{days}d"] = t_sum / n_sum.replace(0, np.nan)

    # ── Win / Loss & RTP ─────────────────────────────────────────────────────
    for days in (30, 180):
        sub = sessions[_w(days)]
        if not sub.empty:
            result_parts[f"win_session_rate_{days}d"] = (
                sub.groupby("canonical_id")["_win_flag"].mean()
            )
        else:
            result_parts[f"win_session_rate_{days}d"] = pd.Series(dtype="float64")

        t_sum = result_parts[f"turnover_sum_{days}d"]
        p_sum = result_parts[f"player_win_sum_{days}d"]
        result_parts[f"actual_rtp_{days}d"] = 1.0 + p_sum / t_sum.replace(0, np.nan)

    t30 = result_parts["theo_win_sum_30d"]
    p30 = result_parts["player_win_sum_30d"]
    result_parts["actual_vs_theo_ratio_30d"] = p30 / t30.replace(0, np.nan)

    # ── Short / Long Ratios ──────────────────────────────────────────────────
    result_parts["turnover_per_bet_30d_over_180d"] = (
        result_parts["turnover_per_bet_mean_30d"]
        / result_parts["turnover_per_bet_mean_180d"].replace(0, np.nan)
    )
    result_parts["turnover_30d_over_180d"] = (
        result_parts["turnover_sum_30d"]
        / result_parts["turnover_sum_180d"].replace(0, np.nan)
    )
    result_parts["sessions_30d_over_180d"] = (
        result_parts["sessions_30d"]
        / result_parts["sessions_180d"].replace(0, np.nan)
    )

    # ── Session Duration ─────────────────────────────────────────────────────
    for days in (30, 180):
        sub = sessions[_w(days) & sessions["_session_ts"].notna()]
        if not sub.empty:
            # Only sessions with both start and end (session_end_dtm IS NOT NULL)
            sub_with_end = sub[sess_end[sub.index].notna()]
            if not sub_with_end.empty:
                result_parts[f"avg_session_duration_min_{days}d"] = (
                    sub_with_end.groupby("canonical_id")["_duration_min"].mean()
                )
            else:
                result_parts[f"avg_session_duration_min_{days}d"] = pd.Series(dtype="float64")
        else:
            result_parts[f"avg_session_duration_min_{days}d"] = pd.Series(dtype="float64")

    # ── Venue Stickiness ─────────────────────────────────────────────────────
    for days in (30, 90):
        result_parts[f"distinct_table_cnt_{days}d"] = _agg_window("table_id", "nunique", days)
    result_parts["distinct_pit_cnt_30d"] = _agg_window("pit_name", "nunique", 30)
    result_parts["distinct_gaming_area_cnt_30d"] = _agg_window("gaming_area", "nunique", 30)

    # top_table_share: two-level aggregation (spec §12)
    for days in (30, 90):
        sub = sessions[_w(days)]
        if not sub.empty and "table_id" in sub.columns:
            # Level 1: turnover per (canonical_id, table_id)
            per_table = (
                sub.groupby(["canonical_id", "table_id"])["turnover"].sum()
            ).reset_index()
            # Level 2: max table turnover per canonical_id
            max_tbl = per_table.groupby("canonical_id")["turnover"].max()
            total_tbl = result_parts[f"turnover_sum_{days}d"]
            result_parts[f"top_table_share_{days}d"] = max_tbl / total_tbl.replace(0, np.nan)
        else:
            result_parts[f"top_table_share_{days}d"] = pd.Series(dtype="float64")

    # ── Assemble output DataFrame ────────────────────────────────────────────
    # All canonical_ids from the full (not windowed) session set
    all_cids = sessions["canonical_id"].unique()
    out = pd.DataFrame(index=all_cids)
    out.index.name = "canonical_id"

    for col_name, series in result_parts.items():
        if isinstance(series, pd.Series) and not series.empty:
            out[col_name] = series.reindex(out.index)
        else:
            out[col_name] = np.nan

    out = out.reset_index()
    out["snapshot_date"] = snapshot_date
    out["snapshot_dtm"] = snapshot_dtm
    out["profile_version"] = PROFILE_VERSION

    logger.info(
        "compute_profile: %d canonical_ids, %d columns for snapshot %s",
        len(out),
        len(out.columns),
        snapshot_date,
    )
    return out


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _write_to_clickhouse(df: pd.DataFrame, client) -> None:
    """Write profile snapshot to player_profile_daily in ClickHouse."""
    # Attempt INSERT … VALUES via clickhouse-driver / clickhouse-connect
    client.insert_df(f"{SOURCE_DB}.{TPROFILE}", df)
    logger.info("Written %d rows to ClickHouse %s.%s", len(df), SOURCE_DB, TPROFILE)


def _write_to_local_parquet(df: pd.DataFrame) -> None:
    """Append (or create) local Parquet file with atomic write (R88).

    Uses a temp file + os.replace to avoid leaving a corrupt file if the
    process is killed mid-write.
    """
    LOCAL_PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    if LOCAL_PROFILE_PARQUET.exists():
        existing = pd.read_parquet(LOCAL_PROFILE_PARQUET)
        combined = pd.concat([existing, df], ignore_index=True)
        # Dedup by (canonical_id, snapshot_date) — keep latest
        combined = (
            combined.sort_values("snapshot_dtm")
            .drop_duplicates(subset=["canonical_id", "snapshot_date"], keep="last")
            .reset_index(drop=True)
        )
    else:
        combined = df
    # R88: atomic write — write to temp then os.replace to final path
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=LOCAL_PARQUET_DIR, suffix=".parquet.tmp"
    )
    try:
        os.close(tmp_fd)
        combined.to_parquet(tmp_path, index=False)
        os.replace(tmp_path, LOCAL_PROFILE_PARQUET)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info(
        "Local Parquet updated (atomic): %d rows total at %s",
        len(combined),
        LOCAL_PROFILE_PARQUET,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_player_profile_daily(
    snapshot_date: date,
    use_local_parquet: bool = False,
    canonical_map: Optional[pd.DataFrame] = None,  # R90: accept pre-built mapping (backfill reuse)
) -> Optional[pd.DataFrame]:
    """Compute player_profile_daily for one `snapshot_date` and persist the result.

    Parameters
    ----------
    snapshot_date:
        The date to snapshot (HK time).  `snapshot_dtm` is set to
        midnight HK (00:00:00) of the next day so that all sessions
        from that date are available.
    use_local_parquet:
        If True, read sessions from local Parquet (dev mode) and write
        result to LOCAL_PROFILE_PARQUET instead of ClickHouse.
    canonical_map:
        Pre-built D2 mapping DataFrame.  When provided (e.g. passed from
        ``backfill``), the mapping query is skipped (R90 reuse).

    Returns
    -------
    DataFrame of profile rows, or None on failure.
    """
    # snapshot_dtm = end of day in HK (all day's sessions flagged available by then)
    snapshot_dtm = datetime(
        snapshot_date.year,
        snapshot_date.month,
        snapshot_date.day,
        23, 59, 59,
    )
    logger.info("Building player_profile_daily for %s (snapshot_dtm=%s)", snapshot_date, snapshot_dtm)

    # 1. Load sessions
    sessions_raw: Optional[pd.DataFrame] = None
    if use_local_parquet:
        sessions_raw = _load_sessions_local(snapshot_dtm)
    if sessions_raw is None:
        if get_clickhouse_client is None:
            logger.error("ClickHouse client unavailable and no local Parquet; aborting")
            return None
        try:
            client = get_clickhouse_client()
            sessions_raw = _load_sessions(snapshot_dtm, client)
        except Exception as exc:
            logger.error("Session load failed: %s", exc)
            return None

    if sessions_raw is None or sessions_raw.empty:
        logger.warning("No session data for %s; skipping", snapshot_date)
        return None

    # 2. D2 canonical_id mapping — reuse pre-built mapping when available (R90)
    if canonical_map is None:
        try:
            if use_local_parquet:
                d2_path = LOCAL_PARQUET_DIR / "canonical_mapping.parquet"
                if d2_path.exists():
                    canonical_map = pd.read_parquet(d2_path)
                else:
                    logger.warning("No local canonical_mapping.parquet; cannot join canonical_id")
                    return None
            else:
                client = get_clickhouse_client()
                canonical_map = build_canonical_mapping(client, cutoff_dtm=snapshot_dtm)
        except Exception as exc:
            logger.error("Canonical mapping load failed: %s", exc)
            return None

    if canonical_map.empty:
        logger.warning("Empty canonical mapping; no rated players to profile")
        return None

    # 3. Join canonical_id onto sessions
    cmap = canonical_map[["player_id", "canonical_id"]].drop_duplicates()
    cmap["player_id"] = cmap["player_id"].astype(str)
    sessions_raw["player_id"] = sessions_raw["player_id"].astype(str)
    sessions_with_cid = sessions_raw.merge(cmap, on="player_id", how="inner")
    logger.info(
        "Sessions after D2 join: %d (of %d)", len(sessions_with_cid), len(sessions_raw)
    )

    if sessions_with_cid.empty:
        logger.warning("No sessions matched canonical_id mapping for %s", snapshot_date)
        return None

    # 4. FND-12: exclude dummy players
    sessions_clean = _exclude_fnd12_dummies(sessions_with_cid)
    if sessions_clean.empty:
        logger.warning("All sessions excluded by FND-12; nothing to write")
        return None

    # 5. Compute profile aggregations
    profile_df = _compute_profile(sessions_clean, snapshot_dtm)

    # 6. Persist
    if use_local_parquet:
        _write_to_local_parquet(profile_df)
    else:
        try:
            client = get_clickhouse_client()
            _write_to_clickhouse(profile_df, client)
        except Exception as exc:
            logger.error("ClickHouse write failed: %s; falling back to local Parquet", exc)
            _write_to_local_parquet(profile_df)

    return profile_df


def backfill(
    start_date: date,
    end_date: date,
    use_local_parquet: bool = False,
) -> None:
    """Backfill player_profile_daily for a range of dates.

    R90: canonical_map is built once and reused across all snapshot dates to
    avoid N redundant mapping queries during a long backfill run.
    """
    # R90: pre-build canonical_map once for the whole backfill range
    canonical_map: Optional[pd.DataFrame] = None
    if use_local_parquet:
        d2_path = LOCAL_PARQUET_DIR / "canonical_mapping.parquet"
        if d2_path.exists():
            canonical_map = pd.read_parquet(d2_path)
            logger.info("backfill: reusing local canonical_map (%d rows)", len(canonical_map))
    elif get_clickhouse_client is not None:
        try:
            client = get_clickhouse_client()
            end_cutoff = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59)
            canonical_map = build_canonical_mapping(client, cutoff_dtm=end_cutoff)
            logger.info("backfill: pre-built canonical_map (%d rows) for reuse", len(canonical_map))
        except Exception as exc:
            logger.warning(
                "backfill: canonical_map pre-build failed (%s); will build per-day", exc
            )

    current = start_date
    success = 0
    failed = 0
    while current <= end_date:
        try:
            result = build_player_profile_daily(
                current,
                use_local_parquet=use_local_parquet,
                canonical_map=canonical_map,
            )
            if result is not None:
                success += 1
            else:
                failed += 1
        except Exception as exc:
            logger.error("Failed for %s: %s", current, exc)
            failed += 1
        current += timedelta(days=1)
    logger.info("Backfill complete: %d succeeded, %d failed", success, failed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build player_profile_daily snapshot for one date or a date range."
    )
    p.add_argument(
        "--snapshot-date",
        type=date.fromisoformat,
        default=None,
        help="Single snapshot date (YYYY-MM-DD). Defaults to today (HK time).",
    )
    p.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=None,
        help="Start date for backfill (YYYY-MM-DD). Requires --end-date.",
    )
    p.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help="End date for backfill, inclusive (YYYY-MM-DD).",
    )
    p.add_argument(
        "--local-parquet",
        action="store_true",
        help="Read sessions from local Parquet and write output to local Parquet (dev mode).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    if args.start_date and args.end_date:
        backfill(args.start_date, args.end_date, use_local_parquet=args.local_parquet)
    else:
        snap_date = args.snapshot_date or datetime.now(HK_TZ).date()
        build_player_profile_daily(snap_date, use_local_parquet=args.local_parquet)


if __name__ == "__main__":
    main()
