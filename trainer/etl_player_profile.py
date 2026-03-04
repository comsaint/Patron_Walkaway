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
import hashlib
import inspect
import json
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
LOCAL_PROFILE_SCHEMA_HASH = LOCAL_PARQUET_DIR / "player_profile_daily.schema_hash"

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
# Helpers for reading raw-data metadata (used inside schema fingerprint)
# ---------------------------------------------------------------------------

def _read_session_min_date(session_path: Path) -> Optional[str]:
    """Return the ISO-format minimum session date from *session_path* Parquet metadata.

    Uses pyarrow row-group statistics — zero data scan.  Returns ``None``
    when the file is absent, the stats are unavailable, or any error occurs.

    We try several candidate columns in priority order so the function is
    robust against schema variants (e.g. files written without ``gaming_day``).

    Note: date coercion is inlined here so this module has no private helper
    duplicating ``trainer.py:_parse_obj_to_date`` (R100).
    """
    if not session_path.exists():
        return None
    # Pre-flight: verify Parquet magic bytes before handing the path to pyarrow.
    # pyarrow can leave file handles open on Windows when it fails to open an
    # invalid file, which causes PermissionError during TemporaryDirectory cleanup.
    try:
        with session_path.open("rb") as _fh:
            if _fh.read(4) != b"PAR1":
                return None
    except OSError:
        return None
    try:
        import pyarrow.parquet as pq  # optional runtime dep

        pf = pq.ParquetFile(session_path)
        col_names = pf.schema_arrow.names
        for col in ("session_start_dtm", "gaming_day", "lud_dtm", "session_end_dtm"):
            if col not in col_names:
                continue
            col_idx = col_names.index(col)
            mins: list = []
            for rg in range(pf.metadata.num_row_groups):
                stats = pf.metadata.row_group(rg).column(col_idx).statistics
                if stats is None or not getattr(stats, "has_min_max", False):
                    continue
                # Inline coercion — shared logic lives in trainer._parse_obj_to_date (R100)
                v = stats.min
                d: Optional[date] = None
                if isinstance(v, datetime):
                    d = v.date()
                elif isinstance(v, date):
                    d = v
                else:
                    s = str(v).strip()
                    try:
                        d = datetime.fromisoformat(s.replace("Z", "+00:00")).date()
                    except (ValueError, AttributeError):
                        try:
                            d = date.fromisoformat(s[:10])
                        except ValueError:
                            pass
                if d is not None:
                    mins.append(d)
            if mins:
                return min(mins).isoformat()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read session parquet min date from %s: %s", session_path, exc)
    return None


# ---------------------------------------------------------------------------
# Profile schema fingerprint
# ---------------------------------------------------------------------------

def compute_profile_schema_hash(session_parquet: Optional[Path] = None) -> str:
    """Return an MD5 fingerprint of everything that determines profile correctness.

    The fingerprint captures four independent drift signals:

    1. **PROFILE_VERSION** — manually bumped string for intentional full-rebuilds.
    2. **PROFILE_FEATURE_COLS + _SESSION_COLS** — column-list drift (features.py).
    3. **compute_source_hash** — MD5 of ``_compute_profile`` source; catches
       aggregation-logic changes that don't touch column names.
    4. **session_min_date** — earliest date found in the raw session parquet
       metadata (zero data scan).  When a developer replaces a 3-month local
       session file with a 1-year file, ``session_min_date`` shifts earlier,
       the hash changes, and the cached profile is automatically invalidated so
       that previously-truncated 365-day windows are recomputed correctly.

    Parameters
    ----------
    session_parquet:
        Path to ``gmwds_t_session.parquet``.  Defaults to
        ``LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"``.

    The sidecar file ``player_profile_daily.schema_hash`` stores the
    fingerprint written when the parquet was last built.
    ``ensure_player_profile_daily_ready`` in trainer.py compares current vs
    stored fingerprint to decide whether to invalidate the cache.
    """
    # Import PROFILE_FEATURE_COLS lazily to avoid circular imports.
    try:
        from features import PROFILE_FEATURE_COLS as _pfc  # type: ignore[import]
    except ModuleNotFoundError:
        from trainer.features import PROFILE_FEATURE_COLS as _pfc  # type: ignore[import, no-redef]

    # Hash the source of _compute_profile so that changes to aggregation logic
    # (not just column names or version string) also invalidate the cache.
    # R98: normalize CRLF -> LF before hashing so the fingerprint is identical
    # regardless of whether the file was checked out on Windows or Linux.
    _src = inspect.getsource(_compute_profile).replace("\r\n", "\n").replace("\r", "\n")
    compute_source_hash = hashlib.md5(_src.encode("utf-8")).hexdigest()[:8]

    # Read raw-data provenance: earliest session date in the source file.
    # When the local session parquet is replaced with a more complete historical
    # dataset, session_min_date shifts earlier -> hash changes -> full rebuild.
    _sess_path = session_parquet if session_parquet is not None else (
        LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    )
    session_min_date = _read_session_min_date(_sess_path)

    payload = json.dumps(
        {
            "profile_version": PROFILE_VERSION,
            "feature_cols": sorted(_pfc),
            "session_cols": sorted(_SESSION_COLS),
            "compute_source_hash": compute_source_hash,
            "session_min_date": session_min_date,
        },
        sort_keys=True,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


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


def _filter_ts_etl(dt: datetime, parquet_path: Path, col: str) -> "pd.Timestamp":
    """Return a Timestamp compatible with the Parquet column's tz schema.

    Reads the column schema once (cheap: no data rows) to determine whether
    the column is tz-aware or tz-naive, then returns a matching Timestamp.
    Mirrors the ``_filter_ts`` helper in trainer.py (R28 fix) so that
    PyArrow pushdown filters never raise ArrowNotImplementedError due to
    mismatched timezone awareness.
    """
    import pyarrow.parquet as pq  # local import: optional runtime dependency

    ts = pd.Timestamp(dt)
    try:
        schema = pq.read_schema(parquet_path)
        field = schema.field(col)
        col_tz = getattr(field.type, "tz", None)
    except Exception:
        col_tz = None
    if col_tz:
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    else:
        return ts.tz_localize(None) if ts.tzinfo is None else ts.replace(tzinfo=None)


def _load_sessions_local(snapshot_dtm: datetime) -> Optional[pd.DataFrame]:
    """Dev fallback: load t_session from a local Parquet export and apply DQ filters.

    Uses PyArrow pushdown filters on ``session_start_dtm`` to restrict the
    rows read from disk to the relevant time window, avoiding a full-table
    load (OOM fix for 8 GB machines).  Falls back to full-table read if the
    target column is absent from the schema.
    """
    t_session_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    if not t_session_path.exists():
        return None
    try:
        lo_dtm = snapshot_dtm - timedelta(days=MAX_LOOKBACK_DAYS + 30)

        # Build pushdown filter on session_start_dtm to avoid full-table read.
        # We use session_start_dtm as the filter column because it is always
        # present and indexed in the Parquet row-group statistics.  The actual
        # availability gate (session_end_dtm / lud_dtm + SESSION_AVAIL_DELAY_MIN)
        # is applied below in pandas after the read; using session_start_dtm as
        # a coarse lower bound is safe: a session that started before lo_dtm
        # could not have ended (and therefore become "available") later than
        # snapshot_dtm + MAX_LOOKBACK_DAYS, so no valid rows are excluded.
        import pyarrow.parquet as pq  # local import: optional runtime dependency

        _filter_col = "session_start_dtm"
        try:
            _schema_cols = pq.read_schema(t_session_path).names
            _has_filter_col = _filter_col in _schema_cols
        except Exception:
            _has_filter_col = False

        if _has_filter_col:
            _lo_ts = _filter_ts_etl(lo_dtm, t_session_path, _filter_col)
            # Upper bound: snapshot_dtm + SESSION_AVAIL_DELAY_MIN to capture
            # sessions whose end time (used for avail_time) falls at or before
            # snapshot_dtm after the delay is applied.
            _hi_ts = _filter_ts_etl(
                snapshot_dtm + timedelta(minutes=SESSION_AVAIL_DELAY_MIN),
                t_session_path,
                _filter_col,
            )
            parquet_filters = [
                (_filter_col, ">=", _lo_ts),
                (_filter_col, "<=", _hi_ts),
            ]
        else:
            logger.warning(
                "_load_sessions_local: %s not found in schema; "
                "falling back to full-table read (potential OOM on large files)",
                _filter_col,
            )
            parquet_filters = None

        # R99: explicit column projection avoids loading unused columns.
        df = pd.read_parquet(
            t_session_path,
            columns=_SESSION_COLS,
            filters=parquet_filters,
        )

        # R103: guard against parquet files that are missing required DQ columns.
        for _dq_col in ("is_manual", "is_deleted", "is_canceled"):
            if _dq_col not in df.columns:
                logger.warning(
                    "Missing DQ column %s in local session parquet; "
                    "rows will NOT be filtered on this column",
                    _dq_col,
                )

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


def _preload_sessions_local() -> Optional[pd.DataFrame]:
    """Load the full local session Parquet once, apply DQ filters and dedup.

    Adds a ``__avail_time`` column (tz-naive Timestamp) so callers can do
    fast in-memory time-window filtering without re-reading the file.
    Used by ``backfill`` fast-mode to avoid N × Parquet I/O.
    """
    t_session_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    if not t_session_path.exists():
        return None
    try:
        df = pd.read_parquet(t_session_path, columns=_SESSION_COLS)
        for _dq_col in ("is_manual", "is_deleted", "is_canceled"):
            if _dq_col not in df.columns:
                logger.warning(
                    "Missing DQ column %s in local session parquet; "
                    "rows will NOT be filtered on this column",
                    _dq_col,
                )

        sess_end = pd.to_datetime(df.get("session_end_dtm", pd.NaT))
        lud = pd.to_datetime(df.get("lud_dtm", pd.NaT))
        avail_time = sess_end.fillna(lud) + pd.Timedelta(minutes=SESSION_AVAIL_DELAY_MIN)
        if avail_time.dt.tz is not None:
            avail_time = avail_time.dt.tz_localize(None)

        dq_mask = (
            (df.get("is_manual", pd.Series(0, index=df.index)) == 0)
            & (df.get("is_deleted", pd.Series(0, index=df.index)) == 0)
            & (df.get("is_canceled", pd.Series(0, index=df.index)) == 0)
            & (
                (df.get("turnover", pd.Series(0.0, index=df.index)).fillna(0) > 0)
                | (df.get("num_games_with_wager", pd.Series(0, index=df.index)).fillna(0) > 0)
            )
        )
        df = df[dq_mask].drop_duplicates(subset=["session_id"]).copy()
        df["__avail_time"] = avail_time[df.index].values
        logger.info(
            "_preload_sessions_local: %d rows loaded (DQ applied, avail_time cached)", len(df)
        )
        return df
    except Exception as exc:
        logger.warning("_preload_sessions_local failed: %s", exc)
        return None


def _filter_preloaded_sessions(
    preloaded: "pd.DataFrame", snapshot_dtm: datetime
) -> Optional["pd.DataFrame"]:
    """Filter a preloaded sessions cache for a given snapshot_dtm time window.

    Expects ``preloaded`` to have a ``__avail_time`` column (tz-naive) as
    produced by ``_preload_sessions_local``.
    """
    def _naive(dt: datetime) -> "pd.Timestamp":
        ts = pd.Timestamp(dt)
        return ts.tz_localize(None) if ts.tzinfo is None else ts.replace(tzinfo=None)

    snap_ts = _naive(snapshot_dtm)
    lo_ts = _naive(snapshot_dtm - timedelta(days=MAX_LOOKBACK_DAYS + 30))
    avail = pd.to_datetime(preloaded["__avail_time"])
    mask = (avail >= lo_ts) & (avail <= snap_ts)
    result = preloaded[mask].drop(columns=["__avail_time"], errors="ignore")
    if result.empty:
        return None
    logger.info(
        "_filter_preloaded_sessions: %d rows for snapshot_dtm=%s", len(result), snapshot_dtm
    )
    return result


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
    logger.info("FND-12 exclusion: %d -> %d canonical_ids", before, after)
    return sessions


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------

def _compute_profile(
    sessions: pd.DataFrame,
    snapshot_dtm: datetime,
    max_lookback_days: int = 365,
) -> pd.DataFrame:
    """Compute player_profile_daily columns for one snapshot_dtm.

    Parameters
    ----------
    sessions:
        Clean t_session rows with canonical_id column, covering up to
        MAX_LOOKBACK_DAYS before snapshot_dtm.
    snapshot_dtm:
        The as-of cutoff time for all aggregations.
    max_lookback_days:
        DEC-017 Data-Horizon: only compute features whose minimum required
        lookback window (per ``_PROFILE_FEATURE_MIN_DAYS`` in features.py) is
        ≤ this value.  Columns for longer windows are emitted as NaN so that
        the output schema stays constant regardless of the horizon.  Default
        365 = full feature set (normal mode).

    Returns
    -------
    DataFrame with one row per canonical_id plus all Phase 1 feature columns.
    """
    snapshot_date = (  # type: date
        snapshot_dtm.date() if isinstance(snapshot_dtm, datetime) else snapshot_dtm
    )

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
    # avoids same-day boundary ambiguity introduced by date-level comparisons).
    # All flags are computed regardless of max_lookback_days (trivially cheap).
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

    # DEC-017: shorthand for an empty (NaN) placeholder when a window exceeds
    # the available data horizon.  Schema stays constant; values are NaN.
    _null = pd.Series(dtype="float64")

    result_parts: dict = {}

    # ── Recency ─────────────────────────────────────────────────────────────
    # Always computable: min_days = 1 per _PROFILE_FEATURE_MIN_DAYS.
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
        if days > max_lookback_days:
            result_parts[f"sessions_{days}d"] = _null
        else:
            result_parts[f"sessions_{days}d"] = _agg_window("session_id", "count", days)
    for days in (30, 90, 365):
        if days > max_lookback_days:
            result_parts[f"active_days_{days}d"] = _null
        else:
            result_parts[f"active_days_{days}d"] = _agg_window("_session_date", "nunique", days)

    # ── Monetary ─────────────────────────────────────────────────────────────
    for days in (7, 30, 90, 180, 365):
        if days > max_lookback_days:
            result_parts[f"turnover_sum_{days}d"] = _null
        else:
            result_parts[f"turnover_sum_{days}d"] = _agg_window("turnover", "sum", days)
    for days in (30, 90, 180, 365):
        if days > max_lookback_days:
            result_parts[f"player_win_sum_{days}d"] = _null
        else:
            result_parts[f"player_win_sum_{days}d"] = _agg_window("player_win", "sum", days)
    for days in (30, 180):
        if days > max_lookback_days:
            result_parts[f"theo_win_sum_{days}d"] = _null
            result_parts[f"num_bets_sum_{days}d"] = _null
            result_parts[f"num_games_with_wager_sum_{days}d"] = _null
        else:
            result_parts[f"theo_win_sum_{days}d"] = _agg_window("theo_win", "sum", days)
            result_parts[f"num_bets_sum_{days}d"] = _agg_window("num_bets", "sum", days)
            result_parts[f"num_games_with_wager_sum_{days}d"] = _agg_window(
                "num_games_with_wager", "sum", days
            )

    # ── Bet intensity ────────────────────────────────────────────────────────
    # Derived from turnover_sum / num_bets_sum; empty inputs -> empty -> NaN in output.
    for days in (30, 180):
        t_sum = result_parts[f"turnover_sum_{days}d"]
        n_sum = result_parts[f"num_bets_sum_{days}d"]
        result_parts[f"turnover_per_bet_mean_{days}d"] = (
            _null if days > max_lookback_days else t_sum / n_sum.replace(0, np.nan)
        )

    # ── Win / Loss & RTP ─────────────────────────────────────────────────────
    for days in (30, 180):
        if days > max_lookback_days:
            result_parts[f"win_session_rate_{days}d"] = _null
            result_parts[f"actual_rtp_{days}d"] = _null
        else:
            sub = sessions[_w(days)]
            if not sub.empty:
                result_parts[f"win_session_rate_{days}d"] = (
                    sub.groupby("canonical_id")["_win_flag"].mean()
                )
            else:
                result_parts[f"win_session_rate_{days}d"] = _null
            t_sum = result_parts[f"turnover_sum_{days}d"]
            p_sum = result_parts[f"player_win_sum_{days}d"]
            result_parts[f"actual_rtp_{days}d"] = 1.0 + p_sum / t_sum.replace(0, np.nan)

    # actual_vs_theo_ratio_30d: min_days=30; empty inputs -> NaN automatically
    t30 = result_parts["theo_win_sum_30d"]
    p30 = result_parts["player_win_sum_30d"]
    result_parts["actual_vs_theo_ratio_30d"] = (
        _null if 30 > max_lookback_days else p30 / t30.replace(0, np.nan)
    )

    # ── Short / Long Ratios (min_days=180) ───────────────────────────────────
    # All three require the 180d window; produce NaN if horizon < 180.
    if 180 > max_lookback_days:
        result_parts["turnover_per_bet_30d_over_180d"] = _null
        result_parts["turnover_30d_over_180d"] = _null
        result_parts["sessions_30d_over_180d"] = _null
    else:
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
        if days > max_lookback_days:
            result_parts[f"avg_session_duration_min_{days}d"] = _null
        else:
            sub = sessions[_w(days) & sessions["_session_ts"].notna()]
            if not sub.empty:
                sub_with_end = sub[sess_end[sub.index].notna()]
                if not sub_with_end.empty:
                    result_parts[f"avg_session_duration_min_{days}d"] = (
                        sub_with_end.groupby("canonical_id")["_duration_min"].mean()
                    )
                else:
                    result_parts[f"avg_session_duration_min_{days}d"] = _null
            else:
                result_parts[f"avg_session_duration_min_{days}d"] = _null

    # ── Venue Stickiness ─────────────────────────────────────────────────────
    for days in (30, 90):
        if days > max_lookback_days:
            result_parts[f"distinct_table_cnt_{days}d"] = _null
        else:
            result_parts[f"distinct_table_cnt_{days}d"] = _agg_window("table_id", "nunique", days)
    result_parts["distinct_pit_cnt_30d"] = (
        _null if 30 > max_lookback_days else _agg_window("pit_name", "nunique", 30)
    )
    result_parts["distinct_gaming_area_cnt_30d"] = (
        _null if 30 > max_lookback_days else _agg_window("gaming_area", "nunique", 30)
    )

    # top_table_share: two-level aggregation (spec §12)
    for days in (30, 90):
        if days > max_lookback_days:
            result_parts[f"top_table_share_{days}d"] = _null
        else:
            sub = sessions[_w(days)]
            if not sub.empty and "table_id" in sub.columns:
                per_table = (
                    sub.groupby(["canonical_id", "table_id"])["turnover"].sum()
                ).reset_index()
                max_tbl = per_table.groupby("canonical_id")["turnover"].max()
                total_tbl = result_parts[f"turnover_sum_{days}d"]
                result_parts[f"top_table_share_{days}d"] = max_tbl / total_tbl.replace(0, np.nan)
            else:
                result_parts[f"top_table_share_{days}d"] = _null

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


def _write_to_local_parquet(
    df: pd.DataFrame,
    canonical_id_whitelist: Optional[set] = None,
    max_lookback_days: int = 365,
) -> None:
    """Append (or create) local Parquet file with atomic write (R88).

    Uses a temp file + os.replace to avoid leaving a corrupt file if the
    process is killed mid-write.
    """
    LOCAL_PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    if LOCAL_PROFILE_PARQUET.exists():
        # R104: only read rows for snapshot_dates NOT covered by the incoming
        # batch.  Pyarrow applies the filter at row-group level, so for a large
        # profile (365 days × tens-of-thousands of players) we skip re-reading
        # rows we are about to replace, substantially reducing peak memory.
        _incoming_dates = list(df["snapshot_date"].unique())
        _retained = pd.read_parquet(
            LOCAL_PROFILE_PARQUET,
            filters=[("snapshot_date", "not in", _incoming_dates)],
        )
        combined = pd.concat([_retained, df], ignore_index=True).reset_index(drop=True)
    else:
        combined = df
    # R88: atomic write — write to temp then os.replace to final path
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=LOCAL_PARQUET_DIR, suffix=".parquet.tmp"
    )
    try:
        os.close(tmp_fd)
        combined.to_parquet(tmp_path, index=False)

        # R95: Write schema fingerprint sidecar BEFORE replacing the parquet so
        # that a crash between the two os.replace calls leaves a *mismatched*
        # hash -> next run sees schema drift -> safe full rebuild.
        hash_tmp_fd, hash_tmp_path = tempfile.mkstemp(
            dir=LOCAL_PARQUET_DIR, suffix=".schema_hash.tmp"
        )
        try:
            os.close(hash_tmp_fd)
            # R106: sidecar must store full hash (with population tag) so
            # ensure_player_profile_daily_ready can compare correctly.
            # R300: also encode max_lookback_days (horizon tag) so that a
            # cache written with horizon=30 is not reused when horizon=365
            # is requested — writer and reader must use identical formula.
            base_hash = compute_profile_schema_hash()
            _pop_tag = (
                f"_whitelist={len(canonical_id_whitelist)}"
                if canonical_id_whitelist
                else "_full"
            )
            _horizon_tag = f"_mlb={max_lookback_days}"
            full_hash = hashlib.md5((base_hash + _pop_tag + _horizon_tag).encode()).hexdigest()
            Path(hash_tmp_path).write_text(full_hash, encoding="utf-8")
            os.replace(hash_tmp_path, LOCAL_PROFILE_SCHEMA_HASH)
        except Exception:
            try:
                os.unlink(hash_tmp_path)
            except OSError:
                pass
            raise

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
    preloaded_sessions: Optional[pd.DataFrame] = None,  # fast-mode: skip Parquet I/O per day
    canonical_id_whitelist: Optional[set] = None,  # R106: for sidecar hash population tag
    max_lookback_days: int = 365,  # DEC-017: horizon restriction for profile feature computation
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
    preloaded_sessions:
        Pre-loaded and DQ-filtered sessions DataFrame (from
        ``_preload_sessions_local``).  When provided, the Parquet file is
        not re-read; only the snapshot_dtm time-window filter is applied
        in-memory (fast-mode, avoids N × I/O in backfill).

    Returns
    -------
    DataFrame of profile rows, or None on failure.
    """
    # R102: snapshot_dtm = next midnight + SESSION_AVAIL_DELAY_MIN so that
    # sessions ending at 23:54 on snapshot_date (available at 00:01 next day)
    # are correctly included rather than silently dropped.
    snapshot_dtm = datetime(
        snapshot_date.year,
        snapshot_date.month,
        snapshot_date.day,
    ) + timedelta(days=1, minutes=SESSION_AVAIL_DELAY_MIN)
    logger.info("Building player_profile_daily for %s (snapshot_dtm=%s)", snapshot_date, snapshot_dtm)

    # 1. Load sessions
    sessions_raw: Optional[pd.DataFrame] = None
    if preloaded_sessions is not None:
        # Fast-mode: filter the in-memory cache for this snapshot's time window,
        # avoiding a full Parquet read on every iteration.
        sessions_raw = _filter_preloaded_sessions(preloaded_sessions, snapshot_dtm)
    elif use_local_parquet:
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

    # 5. Compute profile aggregations (DEC-017: pass horizon so only feasible
    #    windows are computed; out-of-horizon columns are emitted as NaN).
    profile_df = _compute_profile(sessions_clean, snapshot_dtm, max_lookback_days=max_lookback_days)

    # 6. Persist
    if use_local_parquet:
        _write_to_local_parquet(
            profile_df,
            canonical_id_whitelist=canonical_id_whitelist,
            max_lookback_days=max_lookback_days,
        )
    else:
        try:
            client = get_clickhouse_client()
            _write_to_clickhouse(profile_df, client)
        except Exception as exc:
            logger.error("ClickHouse write failed: %s; falling back to local Parquet", exc)
            _write_to_local_parquet(
                profile_df,
                canonical_id_whitelist=canonical_id_whitelist,
                max_lookback_days=max_lookback_days,
            )

    return profile_df


def backfill(
    start_date: date,
    end_date: date,
    use_local_parquet: bool = False,
    canonical_id_whitelist: Optional[set] = None,
    snapshot_interval_days: int = 1,
    preload_sessions: bool = True,
    canonical_map: Optional[pd.DataFrame] = None,
    max_lookback_days: int = 365,  # DEC-017: forwarded to _compute_profile via build_player_profile_daily
) -> None:
    """Backfill player_profile_daily for a range of dates.

    R90: canonical_map is built once and reused across all snapshot dates to
    avoid N redundant mapping queries during a long backfill run.

    Parameters
    ----------
    canonical_id_whitelist:
        When provided (fast-mode), only canonical_ids in this set are
        profiled.  Rated players not in the whitelist are silently skipped,
        dramatically reducing per-day aggregation cost.
    snapshot_interval_days:
        Compute a snapshot only every N days (fast-mode: 7).  Intermediate
        dates are skipped; the PIT join in trainer.py will still find the
        most recent available snapshot for each bet.
    preload_sessions:
        When True (default) and conditions are met (fast-mode or whitelist),
        the entire session Parquet is loaded into memory once for efficient
        per-day filtering.  Set to False on low-RAM machines (e.g. 8 GB) to
        instead use per-day PyArrow pushdown reads via ``_load_sessions_local``,
        avoiding the OOM risk at the cost of more frequent disk I/O.
    canonical_map:
        Pre-built player_id -> canonical_id mapping DataFrame.  When provided
        by the caller (e.g. trainer.py already holds the map in memory), the
        internal map-building step is skipped entirely, eliminating the
        ``No local canonical_mapping.parquet`` warning that fires when the
        sidecar file is absent (DEC-017 bug fix).
    """
    # R90: pre-build canonical_map once for the whole backfill range.
    # DEC-017: skip if caller already supplied canonical_map (avoids the
    # "No local canonical_mapping.parquet" warning when the sidecar file is
    # absent — trainer.py holds the map in memory and passes it directly).
    if canonical_map is not None:
        logger.info(
            "backfill: using pre-built canonical_map (%d rows) supplied by caller",
            len(canonical_map),
        )
    else:
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

    # Apply whitelist: keep only the sampled rated players (fast-mode).
    if canonical_id_whitelist is not None and canonical_map is not None and not canonical_map.empty:
        before_n = len(canonical_map)
        canonical_map = canonical_map[
            canonical_map["canonical_id"].astype(str).isin(canonical_id_whitelist)
        ].copy()
        logger.info(
            "backfill: canonical_id_whitelist applied — %d -> %d rated players",
            before_n, len(canonical_map),
        )

    # Fast-mode: pre-load sessions parquet once so each snapshot day only
    # needs an in-memory time-window filter instead of a full file read.
    # R112: also trigger when whitelist is set (whitelist-only fast-mode).
    # When preload_sessions=False (--fast-mode-no-preload), skip full-table
    # load entirely; _load_sessions_local uses PyArrow pushdown instead.
    preloaded_sessions: Optional[pd.DataFrame] = None
    if preload_sessions and use_local_parquet and (
        snapshot_interval_days > 1 or canonical_id_whitelist is not None
    ):
        preloaded_sessions = _preload_sessions_local()
        if preloaded_sessions is not None:
            logger.info(
                "backfill: session parquet preloaded once (%d rows) "
                "for fast-mode (interval=%d days)",
                len(preloaded_sessions), snapshot_interval_days,
            )
    elif not preload_sessions and use_local_parquet:
        logger.info(
            "backfill: session preload disabled (--fast-mode-no-preload); "
            "each snapshot day will use per-day PyArrow pushdown read."
        )

    current = start_date
    success = 0
    failed = 0
    skipped = 0
    _day_idx = 0
    while current <= end_date:
        if _day_idx % snapshot_interval_days == 0:
            try:
                result = build_player_profile_daily(
                    current,
                    use_local_parquet=use_local_parquet,
                    canonical_map=canonical_map,
                    preloaded_sessions=preloaded_sessions,
                    canonical_id_whitelist=canonical_id_whitelist,
                    max_lookback_days=max_lookback_days,
                )
                if result is not None:
                    success += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.error("Failed for %s: %s", current, exc)
                failed += 1
        else:
            skipped += 1
            logger.debug(
                "backfill: skipping %s (snapshot_interval_days=%d, day_idx=%d)",
                current, snapshot_interval_days, _day_idx,
            )
        current += timedelta(days=1)
        _day_idx += 1
    logger.info(
        "Backfill complete: %d succeeded, %d failed, %d skipped",
        success, failed, skipped,
    )


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
