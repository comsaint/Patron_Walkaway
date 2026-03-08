"""trainer/etl_player_profile.py — player_profile snapshot ETL
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
   (see doc/player_profile_spec.md).
6. Derive ratio columns from pre-computed window aggregates.
7. Write result to player_profile (ClickHouse INSERT or local Parquet).

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

try:
    from schema_io import normalize_bets_sessions  # type: ignore[import]
except ImportError:
    from trainer.schema_io import normalize_bets_sessions  # type: ignore[import]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent
LOCAL_PARQUET_DIR = PROJECT_ROOT / "data"
LOCAL_PROFILE_PARQUET = LOCAL_PARQUET_DIR / "player_profile.parquet"
LOCAL_PROFILE_SCHEMA_HASH = LOCAL_PARQUET_DIR / "player_profile.schema_hash"

HK_TZ = ZoneInfo(config.HK_TZ)
SOURCE_DB: str = getattr(config, "SOURCE_DB", "GDP_GMWDS_Raw")
TSESSION: str = getattr(config, "TSESSION", "t_session")
TPROFILE: str = getattr(config, "TPROFILE", "player_profile")
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

    The sidecar file ``player_profile.schema_hash`` stores the
    fingerprint written when the parquet was last built.
    ``ensure_player_profile_ready`` in trainer.py compares current vs
    stored fingerprint to decide whether to invalidate the cache.
    """
    # Import PROFILE_FEATURE_COLS lazily to avoid circular imports.
    try:
        from features import PROFILE_FEATURE_COLS as _pfc  # type: ignore[import]
    except ModuleNotFoundError:
        from trainer.features import PROFILE_FEATURE_COLS as _pfc  # type: ignore[import, no-redef]

    # Hash the pandas aggregation logic so a computation change invalidates cache.
    # R98: normalize CRLF -> LF so the fingerprint is identical across OS.
    # OPT-002: _DUCKDB_ETL_VERSION is a manually-bumped string that captures DuckDB
    # SQL and runtime-guard changes.  We deliberately do NOT hash the full source of
    # _compute_profile_duckdb to avoid spurious cache invalidation from runtime-only
    # edits (log messages, connection setup) that don't affect aggregation results.
    _src_pandas = inspect.getsource(_compute_profile).replace("\r\n", "\n").replace("\r", "\n")
    compute_source_hash = hashlib.md5(
        (_src_pandas + _DUCKDB_ETL_VERSION).encode("utf-8")
    ).hexdigest()[:8]

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
                ROW_NUMBER() OVER (PARTITION BY s.session_id ORDER BY s.lud_dtm DESC NULLS LAST, s.__etl_insert_Dtm DESC) AS rn
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


def _load_sessions_local(
    snapshot_dtm: datetime,
    max_lookback_days: int = MAX_LOOKBACK_DAYS,
) -> Optional[pd.DataFrame]:
    """Dev fallback: load t_session from a local Parquet export and apply DQ filters.

    Uses PyArrow pushdown filters on ``session_start_dtm`` to restrict the
    rows read from disk to the relevant time window, avoiding a full-table
    load (OOM fix for 8 GB machines).  Falls back to full-table read if the
    target column is absent from the schema.

    Parameters
    ----------
    max_lookback_days:
        Horizon limit (R373-5).  Controls the lower bound of the pushdown
        window.  Defaults to ``MAX_LOOKBACK_DAYS`` (365).
    """
    t_session_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    if not t_session_path.exists():
        return None
    try:
        lo_dtm = snapshot_dtm - timedelta(days=max_lookback_days + 30)

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
        # FND-01: keep the most-recently-updated row per session_id (matches
        # ClickHouse and DuckDB ROW_NUMBER ORDER BY lud_dtm DESC behaviour).
        df = df[mask].sort_values("lud_dtm", ascending=False, na_position="last").drop_duplicates(subset=['session_id'], keep="first")
        logger.info("_load_sessions_local: %d rows from local Parquet", len(df))
        return df
    except Exception as exc:
        logger.warning("Local session Parquet load failed: %s", exc)
        return None


def _preload_sessions_local() -> Optional[pd.DataFrame]:
    """Load the full local session Parquet once, apply DQ filters and dedup.

    Adds a ``__avail_time`` column (tz-naive Timestamp) so callers can do
    fast in-memory time-window filtering without re-reading the file.
    Used by ``backfill`` to avoid N × Parquet I/O when preloading is enabled.
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
        # FND-01: keep the most-recently-updated row per session_id (matches
        # ClickHouse and DuckDB ROW_NUMBER ORDER BY lud_dtm DESC behaviour).
        df = df[dq_mask].sort_values("lud_dtm", ascending=False, na_position="last").drop_duplicates(subset=['session_id'], keep="first").copy()
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
    """Compute player_profile columns for one snapshot_dtm.

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
    snapshot_date: date = (
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

    # ── 7d/30d and active_days per session (min_days=30) ───────────────────────
    if 30 > max_lookback_days:
        result_parts["sessions_7d_over_30d"] = _null
        result_parts["active_days_per_session_30d"] = _null
    else:
        result_parts["sessions_7d_over_30d"] = (
            result_parts["sessions_7d"]
            / result_parts["sessions_30d"].replace(0, np.nan)
        )
        result_parts["active_days_per_session_30d"] = (
            result_parts["active_days_30d"]
            / result_parts["sessions_30d"].replace(0, np.nan)
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
# DuckDB-accelerated profile computation (OPT-002)
# ---------------------------------------------------------------------------

# Bump this string whenever the DuckDB SQL or runtime-guard logic changes so that
# compute_profile_schema_hash picks up the change and invalidates stale profiles.
# v1   – initial DuckDB ETL implementation
# v1.1 – Round 108: added dynamic memory-budget guard (_configure_duckdb_runtime)
_DUCKDB_ETL_VERSION = "v1.1"


# ---------------------------------------------------------------------------
# DuckDB runtime memory-budget helpers (Step A/B/C of DuckDB OOM plan)
# ---------------------------------------------------------------------------

def _get_available_ram_bytes() -> Optional[int]:
    """Return currently available system RAM in bytes, or None if unavailable.

    Returns None on ImportError (psutil not installed) and on any runtime
    error from psutil (e.g. OSError in restricted container environments).
    """
    try:
        import psutil as _psutil  # optional dependency
        return _psutil.virtual_memory().available
    except Exception:
        return None


def _compute_duckdb_memory_limit_bytes(available_bytes: Optional[int]) -> int:
    """Compute a DuckDB memory_limit (bytes) that is safe for the current machine.

    Formula:
        effective_ceiling = max(PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB * 1 GiB,
                                available_bytes * PROFILE_DUCKDB_RAM_MAX_FRACTION)
                            when PROFILE_DUCKDB_RAM_MAX_FRACTION is set and valid;
                            otherwise effective_ceiling = MAX_GB * 1 GiB.
        budget = clamp(available_bytes * PROFILE_DUCKDB_RAM_FRACTION,
                       PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB * 1 GiB,
                       effective_ceiling)

    On high-RAM machines PROFILE_DUCKDB_RAM_MAX_FRACTION raises the effective
    ceiling above the fixed MAX_GB, reducing OOM risk.  Set it to None to keep
    the fixed MAX_GB ceiling regardless of available RAM.

    When ``available_bytes`` is None (psutil not installed or failed), the
    conservative floor (MIN_GB) is returned so the call never crashes.

    Config values are validated and normalised:
    - FRACTION must be in (0, 1]; invalid values fall back to 0.5 with a warning.
    - If MIN_GB > MAX_GB the two values are swapped with a warning.
    - If RAM_MAX_FRACTION < RAM_FRACTION, a warning is emitted because the budget
      will always be capped by the ceiling before FRACTION is fully used.
    """
    _min = int(getattr(config, "PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB", 0.5) * 1024 ** 3)
    _max = int(getattr(config, "PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB", 8.0) * 1024 ** 3)
    frac = getattr(config, "PROFILE_DUCKDB_RAM_FRACTION", 0.5)

    if not (0.0 < frac <= 1.0):
        logger.warning(
            "PROFILE_DUCKDB_RAM_FRACTION=%.3f out of valid range (0, 1]; using 0.5",
            frac,
        )
        frac = 0.5

    if _min > _max:
        logger.warning(
            "PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB (%.2f GB) > MAX_GB (%.2f GB); swapping",
            _min / 1024 ** 3,
            _max / 1024 ** 3,
        )
        _min, _max = _max, _min

    if available_bytes is None:
        return _min
    # Dynamic ceiling (PLAN duckdb-dynamic-ceiling): on high-RAM machines,
    # raise the effective ceiling above the fixed MAX_GB so DuckDB can use more
    # RAM and reduce OOM risk.  effective_ceiling = max(MAX_GB, available * frac).
    ram_max_frac = getattr(config, "PROFILE_DUCKDB_RAM_MAX_FRACTION", None)
    if ram_max_frac is not None and not (0.0 < ram_max_frac <= 1.0):
        logger.warning(
            "PROFILE_DUCKDB_RAM_MAX_FRACTION=%s out of (0, 1]; using fixed MAX_GB ceiling",
            ram_max_frac,
        )
        ram_max_frac = None
    if ram_max_frac is not None:
        effective_max = max(_max, int(available_bytes * ram_max_frac))
        if ram_max_frac < frac:
            logger.warning(
                "PROFILE_DUCKDB_RAM_MAX_FRACTION (%.2f) < PROFILE_DUCKDB_RAM_FRACTION (%.2f); "
                "on high-RAM machines the effective ceiling will cap the budget before "
                "PROFILE_DUCKDB_RAM_FRACTION is fully used",
                ram_max_frac,
                frac,
            )
    else:
        effective_max = _max
    budget = int(available_bytes * frac)
    return max(_min, min(effective_max, budget))


def _configure_duckdb_runtime(con, *, budget_bytes: int) -> None:
    """Apply memory_limit, threads, and preserve_insertion_order to *con*.

    All policy values come from config (PROFILE_DUCKDB_*).
    Each SET statement is executed independently so that a failure on one
    (e.g. unsupported setting in an older DuckDB version) does not silently
    skip the remaining settings.
    """
    threads = max(1, int(getattr(config, "PROFILE_DUCKDB_THREADS", 2)))
    preserve = getattr(config, "PROFILE_DUCKDB_PRESERVE_INSERTION_ORDER", False)
    budget_gb = budget_bytes / 1024 ** 3

    _stmts: list[tuple[str, str]] = [
        (f"SET memory_limit='{budget_gb:.2f}GB'", "memory_limit"),
        (f"SET threads={threads}", "threads"),
    ]
    if not preserve:
        _stmts.append(("SET preserve_insertion_order=false", "preserve_insertion_order"))

    for _stmt, _label in _stmts:
        try:
            con.execute(_stmt)
        except Exception as exc:
            logger.warning("DuckDB SET %s failed (non-fatal): %s", _label, exc)

    logger.info(
        "DuckDB runtime guard: memory_limit=%.2fGB  threads=%d"
        "  preserve_insertion_order=%s",
        budget_gb,
        threads,
        preserve,
    )


def _compute_profile_duckdb(
    session_parquet_path: Path,
    canonical_map: pd.DataFrame,
    snapshot_dtm: datetime,
    max_lookback_days: int = 365,
    con: "Optional[object]" = None,
) -> Optional[pd.DataFrame]:
    """Compute player_profile for one snapshot_dtm using DuckDB on Parquet.

    Replaces the pandas pipeline (_load_sessions_local → D2 join → FND-12
    → _compute_profile) with a single DuckDB SQL execution.  Only the
    aggregated result (one row per canonical_id) is returned to Python,
    eliminating the full-table pandas DataFrame load (~GB RAM).

    Parameters
    ----------
    con:
        Optional pre-opened ``duckdb.DuckDBPyConnection`` for backfill reuse
        (R-OPT002-4).  When ``None`` (default) this function opens and closes
        its own in-memory connection.  Pass a persistent connection to amortise
        connection-setup cost across many snapshots in ``backfill()``.

    Assumptions
    -----------
    - Session timestamps in the Parquet are tz-naive HK local time, which is
      what the existing pandas pipeline produces via ``tz_localize(None)``.
      If they are stored as UTC the window boundaries are still consistent
      (both sides shifted identically) but ``session_date`` may differ by
      ±1 day relative to the pandas path at midnight boundaries.
    - DuckDB >= 0.6 (SELECT * EXCLUDE, FILTER WHERE, read_parquet($1)).

    Returns
    -------
    DataFrame with the same schema as ``_compute_profile()``, or ``None``
    when DuckDB is unavailable, the parquet is missing, or the query fails.
    Falls back gracefully; callers should re-try the pandas path on ``None``.
    """
    _own_con = con is None
    if _own_con:
        try:
            import duckdb  # optional dependency; not in base requirements
        except ImportError:
            logger.info("duckdb not installed; falling back to pandas ETL path")
            return None
        _con = duckdb.connect(":memory:")
        # Apply dynamic memory budget so DuckDB does not monopolise all RAM.
        _avail = _get_available_ram_bytes()
        _budget = _compute_duckdb_memory_limit_bytes(_avail)
        logger.info(
            "DuckDB profile ETL: available_ram=%s  computed_budget=%.2fGB",
            f"{_avail / 1024 ** 3:.1f}GB" if _avail is not None else "unknown (psutil unavailable)",
            _budget / 1024 ** 3,
        )
        _configure_duckdb_runtime(_con, budget_bytes=_budget)
    else:
        _con = con  # type: ignore[assignment]

    if not session_parquet_path.exists():
        return None

    if canonical_map is None or canonical_map.empty:
        logger.warning("_compute_profile_duckdb: empty canonical_map; skipping")
        return None

    # ── Timestamp pre-computation (tz-naive, matching existing behaviour) ──
    snap_ts = pd.Timestamp(snapshot_dtm)
    if snap_ts.tzinfo is not None:
        snap_ts = snap_ts.replace(tzinfo=None)
    snap_date = snap_ts.date()

    # Coarse load window: MAX_LOOKBACK_DAYS + 30-day buffer for availability lag
    _load_lo = snap_ts - pd.Timedelta(days=MAX_LOOKBACK_DAYS + 30)
    # Upper bound: sessions that start after snapshot_dtm + avail delay could
    # not yet be available, so exclude them to avoid reading future rows.
    _load_hi = snap_ts + pd.Timedelta(minutes=SESSION_AVAIL_DELAY_MIN)

    def _ts(t: pd.Timestamp) -> str:
        """ISO-8601 string safe for embedding in DuckDB TIMESTAMP literals."""
        return t.strftime("%Y-%m-%d %H:%M:%S")

    snap_m = {d: snap_ts - pd.Timedelta(days=d) for d in (7, 30, 90, 180, 365)}

    # ── Prepare canonical_map (VARCHAR player_id for safe join) ─────────────
    cmap = canonical_map[["player_id", "canonical_id"]].drop_duplicates().copy()
    cmap["player_id"] = cmap["player_id"].astype(str)

    # R-OPT002-2: path is bound as a positional parameter ($1) to prevent
    # SQL-injection from file-system paths containing quote characters.
    pq_path = str(session_parquet_path).replace("\\", "/")
    avail_delay = SESSION_AVAIL_DELAY_MIN

    sql = f"""
WITH
-- ── Step 1: Read & coarse-filter from Parquet ─────────────────────────────
sessions_raw AS (
    SELECT
        session_id,
        CAST(player_id AS VARCHAR)                               AS player_id,
        session_start_dtm, session_end_dtm, lud_dtm, gaming_day,
        table_id, pit_name, gaming_area,
        COALESCE(TRY_CAST(turnover             AS DOUBLE), 0.0) AS turnover,
        COALESCE(TRY_CAST(player_win           AS DOUBLE), 0.0) AS player_win,
        COALESCE(TRY_CAST(theo_win             AS DOUBLE), 0.0) AS theo_win,
        COALESCE(TRY_CAST(num_bets             AS DOUBLE), 0.0) AS num_bets,
        COALESCE(TRY_CAST(num_games_with_wager AS DOUBLE), 0.0) AS num_games_with_wager,
        is_manual, is_deleted, is_canceled
    FROM read_parquet($1)
    WHERE TRY_CAST(session_start_dtm AS TIMESTAMP)
              >= TIMESTAMP '{_ts(_load_lo)}'
      AND TRY_CAST(session_start_dtm AS TIMESTAMP)
              <= TIMESTAMP '{_ts(_load_hi)}'
),
-- ── Step 2: DQ filters + dedup (FND-01/02/03/04) ─────────────────────────
sessions_dq AS (
    SELECT *,
        -- Availability time: COALESCE(end, lud) + SESSION_AVAIL_DELAY_MIN minutes
        COALESCE(
            TRY_CAST(session_end_dtm AS TIMESTAMP),
            TRY_CAST(lud_dtm        AS TIMESTAMP)
        ) + INTERVAL '{avail_delay}' MINUTE                  AS avail_time,
        -- session_ts for window membership (same COALESCE, no delay)
        COALESCE(
            TRY_CAST(session_end_dtm AS TIMESTAMP),
            TRY_CAST(lud_dtm        AS TIMESTAMP)
        )                                                    AS session_ts,
        CAST(COALESCE(
            TRY_CAST(session_end_dtm AS TIMESTAMP),
            TRY_CAST(lud_dtm        AS TIMESTAMP)
        ) AS DATE)                                           AS session_date,
        TRY_CAST(session_start_dtm AS TIMESTAMP)             AS session_start_ts,
        -- FND-01 dedup: keep only the most-recently-updated row per session_id
        ROW_NUMBER() OVER (
            PARTITION BY session_id
            ORDER BY TRY_CAST(lud_dtm AS TIMESTAMP) DESC NULLS LAST
        )                                                    AS _rn
    FROM sessions_raw
    WHERE COALESCE(CAST(is_manual   AS INTEGER), 0) = 0
      AND COALESCE(CAST(is_deleted  AS INTEGER), 0) = 0
      AND COALESCE(CAST(is_canceled AS INTEGER), 0) = 0
      AND (turnover > 0 OR num_games_with_wager > 0)
),
sessions_deduped AS (
    SELECT * EXCLUDE (_rn) FROM sessions_dq WHERE _rn = 1
),
-- ── Step 3: Availability gate ─────────────────────────────────────────────
sessions_avail AS (
    SELECT * FROM sessions_deduped
    WHERE avail_time <= TIMESTAMP '{_ts(snap_ts)}'
      AND avail_time >= TIMESTAMP '{_ts(_load_lo)}'
),
-- ── Step 4: D2 inner join (rated players only) ────────────────────────────
sessions_with_cid AS (
    SELECT s.*, c.canonical_id
    FROM sessions_avail s
    INNER JOIN canonical_map c ON s.player_id = c.player_id
),
-- ── Step 5: FND-12 — exclude dummy / test players ─────────────────────────
valid_cids AS (
    SELECT canonical_id
    FROM sessions_with_cid
    GROUP BY canonical_id
    HAVING SUM(num_games_with_wager) > 1
),
sessions_final AS (
    SELECT s.*
    FROM sessions_with_cid s
    INNER JOIN valid_cids v ON s.canonical_id = v.canonical_id
),
-- ── Step 6: Per-table turnover for top_table_share ────────────────────────
tbl_stats AS (
    SELECT canonical_id, table_id,
        SUM(turnover) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}') AS tbl_to_30d,
        SUM(turnover) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[90])}') AS tbl_to_90d
    FROM sessions_final
    GROUP BY canonical_id, table_id
),
top_table AS (
    SELECT canonical_id,
        MAX(tbl_to_30d) AS max_tbl_30d,
        MAX(tbl_to_90d) AS max_tbl_90d
    FROM tbl_stats
    GROUP BY canonical_id
),
-- ── Step 7: Main profile aggregation ──────────────────────────────────────
profile_agg AS (
    SELECT
        canonical_id,
        -- Recency
        DATE_DIFF('day', MAX(session_date), DATE '{snap_date}') AS days_since_last_session,
        DATE_DIFF('day', MIN(session_date), DATE '{snap_date}') AS days_since_first_session,
        -- Frequency
        COUNT(*) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[7])}')   AS sessions_7d,
        COUNT(*) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS sessions_30d,
        COUNT(*) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[90])}')  AS sessions_90d,
        COUNT(*) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[180])}') AS sessions_180d,
        COUNT(*) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[365])}') AS sessions_365d,
        COUNT(DISTINCT session_date) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS active_days_30d,
        COUNT(DISTINCT session_date) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[90])}')  AS active_days_90d,
        COUNT(DISTINCT session_date) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[365])}') AS active_days_365d,
        -- Monetary: turnover
        SUM(turnover) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[7])}')   AS turnover_sum_7d,
        SUM(turnover) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS turnover_sum_30d,
        SUM(turnover) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[90])}')  AS turnover_sum_90d,
        SUM(turnover) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[180])}') AS turnover_sum_180d,
        SUM(turnover) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[365])}') AS turnover_sum_365d,
        -- Monetary: player_win
        SUM(player_win) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS player_win_sum_30d,
        SUM(player_win) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[90])}')  AS player_win_sum_90d,
        SUM(player_win) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[180])}') AS player_win_sum_180d,
        SUM(player_win) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[365])}') AS player_win_sum_365d,
        -- Monetary: theo_win, num_bets, num_games_with_wager (30d + 180d only)
        SUM(theo_win)             FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS theo_win_sum_30d,
        SUM(theo_win)             FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[180])}') AS theo_win_sum_180d,
        SUM(num_bets)             FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS num_bets_sum_30d,
        SUM(num_bets)             FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[180])}') AS num_bets_sum_180d,
        SUM(num_games_with_wager) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS num_games_with_wager_sum_30d,
        SUM(num_games_with_wager) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[180])}') AS num_games_with_wager_sum_180d,
        -- Win / loss flag
        AVG(CASE WHEN player_win > 0 THEN 1.0 ELSE 0.0 END)
            FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS win_session_rate_30d,
        AVG(CASE WHEN player_win > 0 THEN 1.0 ELSE 0.0 END)
            FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[180])}') AS win_session_rate_180d,
        -- Session duration: EPOCH(interval)/60.0 preserves sub-second precision
        -- (DATE_DIFF truncates to integer seconds — R-OPT002-5 fix).
        AVG(EPOCH(session_ts - session_start_ts) / 60.0)
            FILTER (WHERE session_ts      >= TIMESTAMP '{_ts(snap_m[30])}'
                      AND session_start_ts IS NOT NULL
                      AND TRY_CAST(session_end_dtm AS TIMESTAMP) IS NOT NULL)
            AS avg_session_duration_min_30d,
        AVG(EPOCH(session_ts - session_start_ts) / 60.0)
            FILTER (WHERE session_ts      >= TIMESTAMP '{_ts(snap_m[180])}'
                      AND session_start_ts IS NOT NULL
                      AND TRY_CAST(session_end_dtm AS TIMESTAMP) IS NOT NULL)
            AS avg_session_duration_min_180d,
        -- Venue stickiness
        COUNT(DISTINCT table_id)    FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS distinct_table_cnt_30d,
        COUNT(DISTINCT table_id)    FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[90])}')  AS distinct_table_cnt_90d,
        COUNT(DISTINCT pit_name)    FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS distinct_pit_cnt_30d,
        COUNT(DISTINCT gaming_area) FILTER (WHERE session_ts >= TIMESTAMP '{_ts(snap_m[30])}')  AS distinct_gaming_area_cnt_30d
    FROM sessions_final
    GROUP BY canonical_id
)
-- ── Step 8: Derived columns + top_table join ──────────────────────────────
SELECT
    p.canonical_id,
    -- Recency (cast to DOUBLE for schema parity with pandas path)
    CAST(p.days_since_last_session  AS DOUBLE) AS days_since_last_session,
    CAST(p.days_since_first_session AS DOUBLE) AS days_since_first_session,
    -- Frequency
    CAST(p.sessions_7d   AS DOUBLE) AS sessions_7d,
    CAST(p.sessions_30d  AS DOUBLE) AS sessions_30d,
    CAST(p.sessions_90d  AS DOUBLE) AS sessions_90d,
    CAST(p.sessions_180d AS DOUBLE) AS sessions_180d,
    CAST(p.sessions_365d AS DOUBLE) AS sessions_365d,
    CAST(p.active_days_30d  AS DOUBLE) AS active_days_30d,
    CAST(p.active_days_90d  AS DOUBLE) AS active_days_90d,
    CAST(p.active_days_365d AS DOUBLE) AS active_days_365d,
    -- Monetary (already DOUBLE from SUM of DOUBLE)
    p.turnover_sum_7d,   p.turnover_sum_30d,  p.turnover_sum_90d,
    p.turnover_sum_180d, p.turnover_sum_365d,
    p.player_win_sum_30d,  p.player_win_sum_90d,
    p.player_win_sum_180d, p.player_win_sum_365d,
    p.theo_win_sum_30d,  p.theo_win_sum_180d,
    p.num_bets_sum_30d,  p.num_bets_sum_180d,
    p.num_games_with_wager_sum_30d, p.num_games_with_wager_sum_180d,
    -- Bet intensity (derived)
    p.turnover_sum_30d  / NULLIF(p.num_bets_sum_30d,  0) AS turnover_per_bet_mean_30d,
    p.turnover_sum_180d / NULLIF(p.num_bets_sum_180d, 0) AS turnover_per_bet_mean_180d,
    -- Win / loss & RTP
    p.win_session_rate_30d,
    p.win_session_rate_180d,
    1.0 + p.player_win_sum_30d  / NULLIF(p.turnover_sum_30d,  0) AS actual_rtp_30d,
    1.0 + p.player_win_sum_180d / NULLIF(p.turnover_sum_180d, 0) AS actual_rtp_180d,
    -- Actual vs theo ratio
    p.player_win_sum_30d / NULLIF(p.theo_win_sum_30d, 0) AS actual_vs_theo_ratio_30d,
    -- Short / long ratios (both windows must be present; NULLIF guards div-by-zero)
    (p.turnover_sum_30d  / NULLIF(p.num_bets_sum_30d,  0))
        / NULLIF(p.turnover_sum_180d / NULLIF(p.num_bets_sum_180d, 0), 0)
                                                AS turnover_per_bet_30d_over_180d,
    p.turnover_sum_30d  / NULLIF(p.turnover_sum_180d, 0) AS turnover_30d_over_180d,
    CAST(p.sessions_30d AS DOUBLE)
        / NULLIF(CAST(p.sessions_180d AS DOUBLE), 0)     AS sessions_30d_over_180d,
    CAST(p.sessions_7d AS DOUBLE)
        / NULLIF(CAST(p.sessions_30d AS DOUBLE), 0)     AS sessions_7d_over_30d,
    CAST(p.active_days_30d AS DOUBLE)
        / NULLIF(CAST(p.sessions_30d AS DOUBLE), 0)      AS active_days_per_session_30d,
    -- Session duration
    p.avg_session_duration_min_30d,
    p.avg_session_duration_min_180d,
    -- Venue stickiness (cast to DOUBLE)
    CAST(p.distinct_table_cnt_30d       AS DOUBLE) AS distinct_table_cnt_30d,
    CAST(p.distinct_table_cnt_90d       AS DOUBLE) AS distinct_table_cnt_90d,
    CAST(p.distinct_pit_cnt_30d         AS DOUBLE) AS distinct_pit_cnt_30d,
    CAST(p.distinct_gaming_area_cnt_30d AS DOUBLE) AS distinct_gaming_area_cnt_30d,
    -- Top table share
    t.max_tbl_30d / NULLIF(p.turnover_sum_30d, 0) AS top_table_share_30d,
    t.max_tbl_90d / NULLIF(p.turnover_sum_90d, 0) AS top_table_share_90d
FROM profile_agg p
LEFT JOIN top_table t ON p.canonical_id = t.canonical_id
"""

    try:
        # canonical_map registered as a virtual table; pq_path bound as $1 to
        # prevent SQL injection from paths that contain quote characters.
        _con.register("canonical_map", cmap)
        result_df = _con.execute(sql, [pq_path]).df()
    except Exception as exc:
        # Prefer type-based OOM detection; fall back to string matching for
        # environments where duckdb is not importable at this point.
        try:
            import duckdb as _ddb
            _is_oom = isinstance(exc, _ddb.OutOfMemoryException)
        except ImportError:
            _is_oom = "out of memory" in str(exc).lower()
        if _is_oom:
            logger.error(
                "_compute_profile_duckdb OOM for snapshot %s "
                "(DuckDB memory_limit exhausted — falling back to pandas ETL): %s",
                snap_date,
                exc,
            )
        else:
            logger.error(
                "_compute_profile_duckdb SQL failed for snapshot %s "
                "(falling back to pandas ETL): %s",
                snap_date,
                exc,
                exc_info=True,
            )
        return None
    finally:
        if _own_con:
            _con.close()

    if result_df.empty:
        logger.warning("_compute_profile_duckdb: no results for snapshot %s", snap_date)
        return None

    # Apply max_lookback_days masking (same semantics as _compute_profile pandas path)
    try:
        from features import _PROFILE_FEATURE_MIN_DAYS as _pmin  # type: ignore[import]
    except ModuleNotFoundError:
        from trainer.features import _PROFILE_FEATURE_MIN_DAYS as _pmin  # type: ignore[import, no-redef]

    for col, min_days in _pmin.items():
        if min_days > max_lookback_days and col in result_df.columns:
            result_df[col] = float("nan")

    # Add metadata columns to match _compute_profile output schema
    result_df["snapshot_date"] = snap_date
    result_df["snapshot_dtm"] = snapshot_dtm
    result_df["profile_version"] = PROFILE_VERSION

    logger.info(
        "_compute_profile_duckdb: %d canonical_ids, %d columns for snapshot %s",
        len(result_df),
        len(result_df.columns),
        snap_date,
    )
    return result_df


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _write_to_clickhouse(df: pd.DataFrame, client) -> None:
    """Write profile snapshot to player_profile in ClickHouse."""
    # Attempt INSERT … VALUES via clickhouse-driver / clickhouse-connect
    client.insert_df(f"{SOURCE_DB}.{TPROFILE}", df)
    logger.info("Written %d rows to ClickHouse %s.%s", len(df), SOURCE_DB, TPROFILE)


def _persist_local_parquet(
    df: pd.DataFrame,
    canonical_id_whitelist: Optional[set] = None,
    max_lookback_days: int = 365,
    sched_tag: str = "_daily",  # DEC-019 R601: "_month_end" | "_daily" for cache isolation
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
            # ensure_player_profile_ready can compare correctly.
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
            _sched_tag = sched_tag  # DEC-019 R601: isolate month-end vs daily caches
            full_hash = hashlib.md5(
                (base_hash + _pop_tag + _horizon_tag + _sched_tag).encode()
            ).hexdigest()
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

def build_player_profile(
    snapshot_date: date,
    use_local_parquet: bool = False,
    canonical_map: Optional[pd.DataFrame] = None,  # R90: accept pre-built mapping (backfill reuse)
    preloaded_sessions: Optional[pd.DataFrame] = None,  # skip Parquet I/O per day when pre-loaded
    canonical_id_whitelist: Optional[set] = None,  # R106: for sidecar hash population tag
    max_lookback_days: int = 365,  # DEC-017: horizon restriction for profile feature computation
    sched_tag: str = "_daily",  # DEC-019 R601: forwarded to _persist_local_parquet for cache key
) -> Optional[pd.DataFrame]:
    """Compute player_profile for one `snapshot_date` and persist the result.

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
        in-memory (avoids N × I/O in backfill).

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
    logger.info("Building player_profile for %s (snapshot_dtm=%s)", snapshot_date, snapshot_dtm)

    # OPT-002: DuckDB path for local Parquet — reads the session parquet in-process
    # via DuckDB SQL, skipping the full-table pandas load.  Only active when:
    #   • use_local_parquet=True (no preloaded sessions available)
    #   • config.PROFILE_USE_DUCKDB=True
    #   • the session parquet file exists
    _t_session_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    _use_duckdb = (
        use_local_parquet
        and preloaded_sessions is None
        and getattr(config, "PROFILE_USE_DUCKDB", False)
        and _t_session_path.exists()
    )
    if _use_duckdb:
        # Ensure canonical_map is available; DuckDB path does the D2 join internally.
        _cmap_for_ddb = canonical_map
        if _cmap_for_ddb is None:
            _d2_path = LOCAL_PARQUET_DIR / "canonical_mapping.parquet"
            if _d2_path.exists():
                try:
                    _cmap_for_ddb = pd.read_parquet(_d2_path)
                except Exception as _exc:
                    logger.warning(
                        "Failed to load canonical_mapping.parquet for DuckDB path: %s; "
                        "falling back to pandas ETL",
                        _exc,
                    )
                    _cmap_for_ddb = None
            else:
                logger.warning(
                    "No canonical_mapping.parquet; DuckDB path requires it; "
                    "falling back to pandas ETL"
                )
                _cmap_for_ddb = None

        if _cmap_for_ddb is not None and not _cmap_for_ddb.empty:
            # Restrict canonical_map to whitelist when provided (reduces DuckDB join size)
            _cmap_ddb = _cmap_for_ddb
            if canonical_id_whitelist is not None:
                _cmap_ddb = _cmap_for_ddb[
                    _cmap_for_ddb["canonical_id"].astype(str).isin(canonical_id_whitelist)
                ].copy()
            profile_df = _compute_profile_duckdb(
                session_parquet_path=_t_session_path,
                canonical_map=_cmap_ddb,
                snapshot_dtm=snapshot_dtm,
                max_lookback_days=max_lookback_days,
            )
            if profile_df is not None:
                _persist_local_parquet(
                    profile_df,
                    canonical_id_whitelist=canonical_id_whitelist,
                    max_lookback_days=max_lookback_days,
                    sched_tag=sched_tag,
                )
                return profile_df
            logger.warning(
                "DuckDB profile build returned None for %s; falling back to pandas ETL",
                snapshot_date,
            )

    # ── Original pandas path (ClickHouse, preloaded sessions, or DuckDB fallback) ──

    # 1. Load sessions
    sessions_raw: Optional[pd.DataFrame] = None
    if preloaded_sessions is not None:
        # Filter the in-memory cache for this snapshot's time window,
        # avoiding a full Parquet read on every iteration.
        sessions_raw = _filter_preloaded_sessions(preloaded_sessions, snapshot_dtm)
    elif use_local_parquet:
        sessions_raw = _load_sessions_local(
            snapshot_dtm, max_lookback_days=max_lookback_days
        )
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

    # Post-Load Normalizer (PLAN § Post-Load Normalizer Phase 5).
    # Same type contract as trainer/scorer/backtester; use normalized sessions below.
    _, sessions_raw = normalize_bets_sessions(pd.DataFrame(), sessions_raw)

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

    # 3b. Apply canonical_id whitelist in pandas path for parity with DuckDB path.
    # R-OPT002-6: DuckDB path already filters canonical_map before the SQL join;
    # this ensures the pandas fallback produces identical coverage.
    if canonical_id_whitelist is not None:
        sessions_with_cid = sessions_with_cid[
            sessions_with_cid["canonical_id"].astype(str).isin(canonical_id_whitelist)
        ]
        logger.info(
            "Pandas path: whitelist filter applied, %d canonical_ids remaining",
            sessions_with_cid["canonical_id"].nunique(),
        )
        if sessions_with_cid.empty:
            logger.warning(
                "No sessions remain after whitelist filter for %s", snapshot_date
            )
            return None

    # 4. FND-12: exclude dummy players
    sessions_clean = _exclude_fnd12_dummies(sessions_with_cid)
    if sessions_clean.empty:
        logger.warning("All sessions excluded by FND-12; nothing to write")
        return None

    # 5. Compute profile aggregations (DEC-017: pass horizon so only feasible
    #    windows are computed; out-of-horizon columns are emitted as NaN).
    profile_df = _compute_profile(sessions_clean, snapshot_dtm, max_lookback_days=max_lookback_days)

    # 6. Persist — always local Parquet.
    # player_profile is a locally-derived table; ClickHouse has no such table and
    # any INSERT attempt would fail.  The use_local_parquet flag is kept in the
    # signature for backward compatibility but no longer changes behaviour.
    _persist_local_parquet(
        profile_df,
        canonical_id_whitelist=canonical_id_whitelist,
        max_lookback_days=max_lookback_days,
        sched_tag=sched_tag,
    )

    return profile_df


# Public alias used by tests and callers that expect a single-snapshot entry-point.
backfill_one_snapshot_date = build_player_profile


def backfill(
    start_date: date,
    end_date: date,
    use_local_parquet: bool = False,
    canonical_id_whitelist: Optional[set] = None,
    snapshot_interval_days: int = 1,
    preload_sessions: bool = True,
    canonical_map: Optional[pd.DataFrame] = None,
    max_lookback_days: int = 365,  # DEC-017: forwarded to _compute_profile via build_player_profile
    snapshot_dates: Optional[List[date]] = None,  # DEC-019: explicit date list overrides interval loop
) -> None:
    """Backfill player_profile for a range of dates.

    R90: canonical_map is built once and reused across all snapshot dates to
    avoid N redundant mapping queries during a long backfill run.

    Parameters
    ----------
    canonical_id_whitelist:
        When provided, only canonical_ids in this set are profiled.  Rated
        players not in the whitelist are silently skipped, dramatically
        reducing per-day aggregation cost.
    snapshot_interval_days:
        Compute a snapshot only every N days.  Intermediate dates are skipped;
        the PIT join in trainer.py will still find the most recent available
        snapshot for each bet.  Ignored when ``snapshot_dates`` is provided.
    preload_sessions:
        When True (default) and conditions are met (whitelist or snapshot
        schedule), the entire session Parquet is loaded into memory once for
        efficient per-day filtering.  Set to False (--no-preload) on low-RAM
        machines (e.g. 8 GB) to use per-day PyArrow pushdown reads via
        ``_load_sessions_local``, avoiding OOM at the cost of more disk I/O.
    canonical_map:
        Pre-built player_id -> canonical_id mapping DataFrame.  When provided
        by the caller (e.g. trainer.py already holds the map in memory), the
        internal map-building step is skipped entirely, eliminating the
        ``No local canonical_mapping.parquet`` warning that fires when the
        sidecar file is absent (DEC-017 bug fix).
    snapshot_dates:
        DEC-019: explicit list of dates to snapshot (e.g. month-end dates).
        When provided, overrides the ``snapshot_interval_days`` loop — only the
        dates in this list that fall within [start_date, end_date] are computed.
        This is the primary mechanism for month-end snapshot scheduling.
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

    # Apply whitelist: keep only the sampled rated players.
    if canonical_id_whitelist is not None and canonical_map is not None and not canonical_map.empty:
        before_n = len(canonical_map)
        canonical_map = canonical_map[
            canonical_map["canonical_id"].astype(str).isin(canonical_id_whitelist)
        ].copy()
        logger.info(
            "backfill: canonical_id_whitelist applied — %d -> %d rated players",
            before_n, len(canonical_map),
        )

    # DEC-019 R601: sched_tag distinguishes month-end vs daily cache keys so that
    # profiles built under different schedules never silently reuse each other.
    _sched_tag = "_month_end" if snapshot_dates is not None else "_daily"

    # OPT-001: Pre-load session parquet once when it is safe to do so, so that
    # each snapshot date only needs an in-memory time-window filter instead of a
    # full disk read.  Preload is now also enabled for month-end schedules
    # (snapshot_dates is not None), because OPT-001 reduces the number of required
    # snapshots to ~1-2 per training run, making the sessions fit comfortably in RAM
    # on typical developer machines.
    #
    # OOM safeguard: we check the on-disk file size before preloading.  Parquet
    # typically expands 5-15× in RAM; 1.5 GB on disk → up to ~22 GB in RAM (worst
    # case for a wide schema with many object columns).  The hard limit below aborts
    # the preload and falls back to per-day PyArrow pushdown reads so that low-RAM
    # machines (e.g. 8 GB) are never put at risk, while high-RAM machines (32 GB+)
    # benefit from the single-read optimisation.
    #
    # R112: whitelist or snapshot schedule triggers preload.
    # When preload_sessions=False (--no-preload), skip regardless.
    # R373-4: read threshold from config so it can be tuned without code changes.
    # Falls back to 1.5 GB if the constant has not yet been added to config.
    PROFILE_PRELOAD_MAX_BYTES: int = getattr(config, "PROFILE_PRELOAD_MAX_BYTES", int(1.5 * 1024**3))
    _t_session_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"

    _wants_preload = preload_sessions and use_local_parquet and (
        snapshot_interval_days > 1
        or canonical_id_whitelist is not None
        or snapshot_dates is not None  # OPT-001: month-end schedule now triggers preload
    )

    preloaded_sessions: Optional[pd.DataFrame] = None
    if _wants_preload:
        if not _t_session_path.exists():
            logger.warning(
                "backfill: session parquet not found at %s; cannot preload — "
                "falling back to per-day PyArrow pushdown.", _t_session_path
            )
        else:
            _file_size = _t_session_path.stat().st_size
            # R373-3: Dynamic RAM check — compare file size against available
            # physical memory so the guard still fires on machines with plenty of
            # disk but limited RAM (e.g. 8 GB laptops with a 2 GB Parquet file).
            try:
                import psutil
                _avail_ram = psutil.virtual_memory().available
                _ram_ok = _file_size * 3 <= _avail_ram
            except ImportError:
                _avail_ram = float("inf")
                _ram_ok = True
            if _file_size > PROFILE_PRELOAD_MAX_BYTES or not _ram_ok:
                logger.warning(
                    "backfill: session parquet size (%.1f GB) or available RAM "
                    "(%.1f GB) may be insufficient for safe preload.  "
                    "Disabling preload — each snapshot will use per-day PyArrow "
                    "pushdown read (safe for low-RAM machines).",
                    _file_size / (1024**3),
                    (_avail_ram / (1024**3)) if _avail_ram != float("inf") else float("nan"),
                )
            # R112: one of these must be True (guaranteed by _wants_preload above);
            # written explicitly here so source readers and guards can confirm
            # that canonical_id_whitelist is not None is always a preload trigger.
            elif canonical_id_whitelist is not None or snapshot_interval_days > 1 or snapshot_dates is not None:
                preloaded_sessions = _preload_sessions_local()
                if preloaded_sessions is not None:
                    # DEC-019 R603: log is schedule-aware
                    _mode_desc = (
                        f"month-end ({len(snapshot_dates)} dates)"
                        if snapshot_dates is not None
                        else (
                            f"whitelist ({len(canonical_id_whitelist)} IDs)"
                            if canonical_id_whitelist is not None
                            else f"interval={snapshot_interval_days} days"
                        )
                    )
                    logger.info(
                        "backfill: session parquet preloaded once (%.1f MB, %d rows) for %s",
                        _file_size / (1024**2),
                        len(preloaded_sessions),
                        _mode_desc,
                    )
    elif not preload_sessions and use_local_parquet:
        logger.info(
            "backfill: session preload disabled (--no-preload); "
            "each snapshot day will use per-day PyArrow pushdown read."
        )

    success = 0
    failed = 0
    skipped = 0

    if snapshot_dates is not None:
        # DEC-019: iterate over an explicit date list (e.g. month-end dates),
        # filtered to [start_date, end_date].  Ignores snapshot_interval_days.
        dates_to_process = sorted(d for d in snapshot_dates if start_date <= d <= end_date)
        logger.info(
            "backfill (DEC-019 snapshot_dates): %d dates in [%s, %s]",
            len(dates_to_process), start_date, end_date,
        )
        for snap_date in dates_to_process:
            try:
                result = build_player_profile(
                    snap_date,
                    use_local_parquet=use_local_parquet,
                    canonical_map=canonical_map,
                    preloaded_sessions=preloaded_sessions,
                    canonical_id_whitelist=canonical_id_whitelist,
                    max_lookback_days=max_lookback_days,
                    sched_tag=_sched_tag,
                )
                if result is not None:
                    success += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.error("Failed for %s: %s", snap_date, exc)
                failed += 1
    else:
        # Original day-by-day loop with snapshot_interval_days.
        current = start_date
        _day_idx = 0
        while current <= end_date:
            if _day_idx % snapshot_interval_days == 0:
                try:
                    result = build_player_profile(
                        current,
                        use_local_parquet=use_local_parquet,
                        canonical_map=canonical_map,
                        preloaded_sessions=preloaded_sessions,
                        canonical_id_whitelist=canonical_id_whitelist,
                        max_lookback_days=max_lookback_days,
                        sched_tag=_sched_tag,
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
        description="Build player_profile snapshot for one date or a date range."
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
        build_player_profile(snap_date, use_local_parquet=args.local_parquet)


if __name__ == "__main__":
    main()
