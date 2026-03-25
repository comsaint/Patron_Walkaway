from __future__ import annotations

import argparse
from collections import Counter, deque
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from bisect import bisect_left, bisect_right

import pandas as pd
# zoneinfo is available in Python 3.9+. If not present, try backports.zoneinfo for older Pythons.
# If both are unavailable, fall back to python-dateutil's gettz so the validator can still run
# without requiring backports (useful in constrained environments).
try:
    from zoneinfo import ZoneInfo
except Exception:
    try:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    except Exception:
        try:
            from dateutil.tz import gettz as ZoneInfo  # type: ignore
        except Exception:
            raise ImportError(
                "ZoneInfo not available: install Python 3.9+ or backports.zoneinfo "
                "or ensure python-dateutil is installed"
            )

try:
    import config  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as config  # type: ignore[import, no-redef]

from trainer.db_conn import get_clickhouse_client  # serving lives under trainer; db_conn at package root

HK_TZ = ZoneInfo(config.HK_TZ)
# Sentinel for ongoing sessions (NULL session_end_dtm in DB); must not trigger
# walkaway logic in validate_alert_row (R59 fix).
_SENTINEL_SESSION_END = pd.Timestamp("2099-12-31 00:00:00", tz="UTC").tz_convert(HK_TZ)

BASE_DIR = Path(__file__).resolve().parent.parent  # trainer/ (serving lives under trainer)
PROJECT_ROOT = BASE_DIR.parent
_state_db_env = os.environ.get("STATE_DB_PATH")
_state_db_effective = _state_db_env.strip() if (_state_db_env and _state_db_env.strip()) else None
STATE_DB_PATH = Path(_state_db_effective) if _state_db_effective else (PROJECT_ROOT / "local_state" / "state.db")
OUT_DIR = BASE_DIR / "out_validator"
OUT_DIR.mkdir(exist_ok=True)
RESULTS_PATH = OUT_DIR / "validation_results.csv"  # legacy only (read-backfill); DB is source of truth

IGNORED_REASONS = {"missing_player_id"}

logger = logging.getLogger(__name__)

_PERF_WINDOW_SIZE = 200
_VALIDATOR_STAGE_TIMINGS: Dict[str, deque[float]] = {}


def _record_validator_stage_timing(stage: str, seconds: float) -> None:
    bucket = _VALIDATOR_STAGE_TIMINGS.setdefault(stage, deque(maxlen=_PERF_WINDOW_SIZE))
    bucket.append(max(0.0, float(seconds)))


def _emit_validator_perf_summary(cycle_stage_seconds: Dict[str, float]) -> None:
    if not cycle_stage_seconds:
        return
    for stage, sec in cycle_stage_seconds.items():
        _record_validator_stage_timing(stage, sec)
    top_stages = sorted(cycle_stage_seconds.items(), key=lambda x: x[1], reverse=True)[:2]
    parts: List[str] = []
    for stage, sec in top_stages:
        hist = _VALIDATOR_STAGE_TIMINGS.get(stage)
        if not hist:
            continue
        arr = pd.Series(hist, dtype="float64")
        p50 = float(arr.quantile(0.5))
        p95 = float(arr.quantile(0.95))
        parts.append(f"{stage}={sec:.3f}s (p50={p50:.3f}s, p95={p95:.3f}s, n={len(arr)})")
    if parts:
        logger.debug("[validator][perf] top_hotspots: %s", "; ".join(parts))

VALIDATION_COLUMNS = [
    "alert_ts",
    "validated_at",
    "player_id",
    "casino_player_id",
    "canonical_id",
    "table_id",
    "position_idx",
    "session_id",
    "bet_id",
    "score",
    "result",
    "gap_start",
    "gap_minutes",
    "reason",
    "bet_ts",
    "model_version",
]

# Columns added to validation_results in Phase 1 (step 8) + casino_player_id (ML API protocol)
_NEW_VAL_COLS: List[Tuple[str, str]] = [
    ("canonical_id", "TEXT"),
    ("model_version", "TEXT"),
    ("casino_player_id", "TEXT"),
    ("bet_ts", "TEXT"),  # legacy DBs created before bet_ts; API/protocol already expose bet_ts
]

# Alerts ALTERs aligned with trainer.serving.scorer.init_state_db (validator-first DB path; Unified Plan v2 T3).
_ALERTS_MIGRATION_COLS: List[Tuple[str, str]] = [
    ("canonical_id", "TEXT"),
    ("is_rated_obs", "INTEGER"),
    ("reason_codes", "TEXT"),
    ("model_version", "TEXT"),
    ("margin", "REAL"),
    ("scored_at", "TEXT"),
    ("casino_player_id", "TEXT"),
]
_VALIDATOR_META_KEY_LAST_ROWID = "validation_results_last_loaded_rowid"


def _latest_model_version_from_alerts(alerts_df: pd.DataFrame) -> Optional[str]:
    """Newest alert by ``ts`` with non-empty ``model_version`` (Unified Plan v2 T3).

    Semantic: **驗證當下認定的版本** for this validation cycle — not a global deploy SSOT.
    """
    if alerts_df.empty or "model_version" not in alerts_df.columns or "ts" not in alerts_df.columns:
        return None
    try:
        sub = alerts_df.sort_values("ts", ascending=False)
    except Exception:
        return None
    for val in sub["model_version"]:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            continue
        s = str(val).strip()
        if s:
            return s
    return None


def _rolling_precision_by_alert_ts(
    finalized_df: pd.DataFrame,
    *,
    now_hk: datetime,
    window: timedelta,
) -> Tuple[float, int, int]:
    """MATCH rate over non-PENDING rows whose ``alert_ts`` falls in ``[now_hk - window, now_hk]`` (HK)."""
    if finalized_df.empty or "alert_ts" not in finalized_df.columns:
        return 0.0, 0, 0
    at = pd.to_datetime(finalized_df["alert_ts"], errors="coerce")
    if getattr(at.dt, "tz", None) is None:
        at = at.dt.tz_localize(HK_TZ, ambiguous="NaT", nonexistent="shift_forward")
    else:
        at = at.dt.tz_convert(HK_TZ)
    cutoff = now_hk - window
    sub = finalized_df[(at >= cutoff) & (at <= now_hk)].copy()
    if sub.empty:
        return 0.0, 0, 0
    total = len(sub)
    matches = int(sub["reason"].eq("MATCH").sum()) if "reason" in sub.columns else 0
    precision = (matches / total) if total > 0 else 0.0
    return float(precision), matches, total


def _append_validator_metrics(
    conn: sqlite3.Connection,
    *,
    recorded_at: str,
    model_version: Optional[str],
    precision: float,
    total: int,
    matches: int,
) -> None:
    """Insert one precision snapshot (``validator_metrics`` table).

    Intended to align with the rolling **15m-by-alert_ts** KPI logged in ``validate_once``.
    """
    conn.execute(
        """
        INSERT INTO validator_metrics (recorded_at, model_version, precision, total, matches)
        VALUES (?, ?, ?, ?, ?)
        """,
        (recorded_at, model_version or "", float(precision), int(total), int(matches)),
    )


def _build_cid_to_player_ids(alerts_df: pd.DataFrame) -> Dict[str, List[int]]:
    """Build {canonical_id: [player_ids]} reverse mapping from the alerts DataFrame.

    The alerts table has both ``canonical_id`` (Phase-1 column) and ``player_id``.
    For rated players the canonical_id is their casino card ID; for non-rated it is
    str(player_id).  Grouping by canonical_id lets us fetch all bets that belong
    to the same person even if they used different player_ids (换卡 / 断链重发).
    """
    if alerts_df.empty:
        return {}

    cid_col = "canonical_id" if "canonical_id" in alerts_df.columns else None
    pid_col = "player_id" if "player_id" in alerts_df.columns else None

    mapping: Dict[str, List[int]] = {}
    for row in alerts_df.itertuples(index=False):
        cid_raw = getattr(row, cid_col, None) if cid_col else None
        pid_raw = getattr(row, pid_col, None) if pid_col else None

        pid = None if (pid_raw is None or pd.isna(pid_raw)) else int(pid_raw)
        if cid_raw is None or pd.isna(cid_raw) or str(cid_raw).strip() == "":
            # fall back: use str(player_id) as canonical_id
            cid = str(pid) if pid is not None else None
        else:
            cid = str(cid_raw)

        if cid is None or pid is None:
            continue
        mapping.setdefault(cid, [])
        if pid not in mapping[cid]:
            mapping[cid].append(pid)

    return mapping


# Maximum player_ids per ClickHouse IN clause; avoids "Query is too large" errors (R44).
_PLAYER_ID_CHUNK_SIZE = 5_000


def fetch_bets_by_canonical_id(
    cid_to_pids: Dict[str, List[int]],
    start: datetime,
    end: datetime,
) -> Dict[str, List[datetime]]:
    """Fetch bets for all player_ids, returning a {canonical_id: [bet_times]} dict.

    Bets from multiple player_ids that share a canonical_id are merged and sorted
    so that the validation logic treats all sub-identities of a rated player as one.

    Large player_id lists are sent in chunks to avoid ClickHouse IN-clause limits
    (R44 — _PLAYER_ID_CHUNK_SIZE per batch).

    DB errors are not silenced — they propagate to the caller so that the
    validation cycle can abort gracefully rather than producing false MISS verdicts
    from an empty cache (R41).
    """
    if not cid_to_pids or get_clickhouse_client is None:
        return {}

    all_pids: List[int] = []
    pid_to_cid: Dict[int, str] = {}
    for cid, pids in cid_to_pids.items():
        for pid in pids:
            if pid not in pid_to_cid:
                pid_to_cid[pid] = cid
                all_pids.append(pid)

    if not all_pids:
        return {}

    client = get_clickhouse_client()  # errors propagate to caller (R41)

    cache: Dict[str, List[datetime]] = {}

    # Batch queries to avoid oversized IN clauses (R44)
    for i in range(0, len(all_pids), _PLAYER_ID_CHUNK_SIZE):
        chunk_pids = all_pids[i : i + _PLAYER_ID_CHUNK_SIZE]
        params = {"players": tuple(chunk_pids), "start": start, "end": end}
        query = f"""
            SELECT player_id, payout_complete_dtm
            FROM {config.SOURCE_DB}.{config.TBET} FINAL
            WHERE player_id IN %(players)s
              AND player_id IS NOT NULL
              AND player_id != {config.PLACEHOLDER_PLAYER_ID}
              AND payout_complete_dtm >= %(start)s
              AND payout_complete_dtm <= %(end)s
              AND payout_complete_dtm IS NOT NULL
              AND wager > 0
            ORDER BY player_id, payout_complete_dtm
        """
        df = client.query_df(query, parameters=params)  # errors propagate (R41)
        if df.empty:
            continue

        df["payout_complete_dtm"] = pd.to_datetime(df["payout_complete_dtm"])
        if df["payout_complete_dtm"].dt.tz is None:
            df["payout_complete_dtm"] = df["payout_complete_dtm"].dt.tz_localize(HK_TZ)
        else:
            df["payout_complete_dtm"] = df["payout_complete_dtm"].dt.tz_convert(HK_TZ)

        for _, row in df.iterrows():
            pid = int(row["player_id"])
            resolved_cid = pid_to_cid.get(pid)
            if resolved_cid is None:
                continue
            cache.setdefault(resolved_cid, [])
            cache[resolved_cid].append(row["payout_complete_dtm"])

    # Sort each canonical_id's bet list by time
    for cid in cache:
        cache[cid].sort()

    return cache


def fetch_sessions_by_canonical_id(
    cid_to_pids: Dict[str, List[int]],
    start: datetime,
    end: datetime,
) -> Dict[str, List[Dict]]:
    """Fetch sessions for all player_ids, grouped by canonical_id (R42).

    Sessions from multiple player_ids that share a canonical_id are merged and
    time-sorted, so that rated players who swap casino-club cards within a run
    are treated as a single identity by validate_alert_row.

    DB errors propagate to the caller — they are not silenced.
    """
    if not cid_to_pids or get_clickhouse_client is None:
        return {}

    all_pids: List[int] = []
    pid_to_cid: Dict[int, str] = {}
    for cid, pids in cid_to_pids.items():
        for pid in pids:
            if pid not in pid_to_cid:
                pid_to_cid[pid] = cid
                all_pids.append(pid)

    if not all_pids:
        return {}

    client = get_clickhouse_client()  # errors propagate to caller

    raw: List[Dict] = []
    for i in range(0, len(all_pids), _PLAYER_ID_CHUNK_SIZE):
        chunk_pids = all_pids[i : i + _PLAYER_ID_CHUNK_SIZE]
        params = {"players": tuple(chunk_pids), "start": start, "end": end}
        query = f"""
            WITH deduped AS (
                SELECT
                    player_id,
                    session_id,
                    COALESCE(session_end_dtm, lud_dtm) AS session_avail_dtm,
                    session_end_dtm,
                    COALESCE(turnover, 0) AS turnover,
                    COALESCE(num_games_with_wager, 0) AS num_games_with_wager,
                    ROW_NUMBER() OVER (
                        PARTITION BY session_id
                        ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC
                    ) AS rn
                FROM {config.SOURCE_DB}.{config.TSESSION}
                WHERE player_id IN %(players)s
                  AND COALESCE(session_end_dtm, lud_dtm) >= %(start)s - INTERVAL 1 DAY
                  AND COALESCE(session_end_dtm, lud_dtm) <= %(end)s + INTERVAL 1 DAY
                  AND is_deleted = 0
                  AND is_canceled = 0
                  AND is_manual = 0
            )
            SELECT player_id, session_id, session_avail_dtm, session_end_dtm
            FROM deduped
            WHERE rn = 1
              AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
            ORDER BY player_id, session_avail_dtm, session_end_dtm
        """
        df = client.query_df(query, parameters=params)
        if df.empty:
            continue

        for col in ["session_avail_dtm", "session_end_dtm"]:
            df[col] = pd.to_datetime(df[col])
            if df[col].dt.tz is None:
                df[col] = df[col].dt.tz_localize(HK_TZ)
            else:
                df[col] = df[col].dt.tz_convert(HK_TZ)

        # session_end_dtm is NULL for ongoing sessions; replace NaT with a
        # far-future sentinel so sort-key and gap arithmetic remain valid (R59).
        df["session_end_dtm"] = df["session_end_dtm"].fillna(_SENTINEL_SESSION_END)

        for _, row in df.iterrows():
            pid = int(row["player_id"])
            resolved_cid = pid_to_cid.get(pid)
            if resolved_cid is None:
                continue
            raw.append(
                {
                    "canonical_id": resolved_cid,
                    "session_id": int(row["session_id"]) if pd.notna(row["session_id"]) else None,
                    "start": row["session_avail_dtm"],
                    "end": row["session_end_dtm"],
                }
            )

    # Group by canonical_id, sort by start time, compute next_start
    by_cid: Dict[str, List[Dict]] = {}
    for item in raw:
        by_cid.setdefault(item["canonical_id"], []).append(item)

    cache: Dict[str, List[Dict]] = {}
    for cid, items in by_cid.items():
        sorted_items = sorted(items, key=lambda s: (s["start"], s["end"]))
        sessions = []
        for idx, item in enumerate(sorted_items):
            next_start = sorted_items[idx + 1]["start"] if idx + 1 < len(sorted_items) else None
            sessions.append(
                {
                    "session_id": item["session_id"],
                    "start": item["start"],
                    "end": item["end"],
                    "next_start": next_start,
                }
            )
        cache[cid] = sessions

    return cache


# ------------------ State helpers (SQLite) ------------------
def get_db_conn() -> sqlite3.Connection:
    STATE_DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_alerts (
            bet_id TEXT PRIMARY KEY,
            processed_ts TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            bet_id TEXT PRIMARY KEY,
            ts TEXT,
            bet_ts TEXT,
            player_id TEXT,
            table_id TEXT,
            position_idx REAL,
            visit_start_ts TEXT,
            visit_end_ts TEXT,
            session_count INTEGER,
            bet_count INTEGER,
            visit_avg_bet REAL,
            historical_avg_bet REAL,
            score REAL,
            session_id TEXT,
            loss_streak INTEGER,
            bets_last_5m REAL,
            bets_last_15m REAL,
            bets_last_30m REAL,
            wager_last_10m REAL,
            wager_last_30m REAL,
            cum_bets REAL,
            cum_wager REAL,
            avg_wager_sofar REAL,
            session_duration_min REAL,
            bets_per_minute REAL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)")
    existing_alert_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()
    }
    for col_name, col_type in _ALERTS_MIGRATION_COLS:
        if col_name not in existing_alert_cols:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col_name} {col_type}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS validator_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            model_version TEXT,
            precision REAL NOT NULL,
            total INTEGER NOT NULL,
            matches INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_validator_metrics_recorded_at "
        "ON validator_metrics(recorded_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_validator_metrics_model_version "
        "ON validator_metrics(model_version)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS validation_results (
            bet_id TEXT PRIMARY KEY,
            alert_ts TEXT,
            validated_at TEXT,
            player_id TEXT,
            table_id TEXT,
            position_idx REAL,
            session_id TEXT,
            score REAL,
            result INTEGER,
            gap_start TEXT,
            gap_minutes REAL,
            reason TEXT,
            bet_ts TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_val_alert_ts ON validation_results(alert_ts)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS validator_runtime_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    # Schema migration: add Phase-1 columns if they don't exist yet (step 8)
    existing_val_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(validation_results)").fetchall()
    }
    for col_name, col_type in _NEW_VAL_COLS:
        if col_name not in existing_val_cols:
            conn.execute(
                f"ALTER TABLE validation_results ADD COLUMN {col_name} {col_type}"
            )

    return conn


def load_processed(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute("SELECT bet_id FROM processed_alerts").fetchall()
        return {r[0] for r in rows if r[0] is not None}
    except Exception:
        return set()


def mark_processed(conn: sqlite3.Connection, bet_ids: List) -> None:
    if not bet_ids:
        return
    ts = datetime.now(HK_TZ).isoformat()
    rows = [(str(bid), ts) for bid in bet_ids if pd.notna(bid)]
    conn.executemany(
        """
        INSERT INTO processed_alerts(bet_id, processed_ts)
        VALUES (?, ?)
        ON CONFLICT(bet_id) DO UPDATE SET processed_ts=excluded.processed_ts
        """,
        rows,
    )
    conn.commit()


def prune_validator_retention(conn: sqlite3.Connection, now_hk: datetime) -> None:
    days = getattr(config, "VALIDATION_RESULTS_RETENTION_DAYS", None)
    if days is None or days <= 0:
        return
    cutoff = now_hk - timedelta(days=days)
    conn.execute("DELETE FROM validation_results WHERE alert_ts < ?", (cutoff.isoformat(),))
    conn.execute("DELETE FROM processed_alerts WHERE processed_ts < ?", (cutoff.isoformat(),))
    conn.commit()


def load_existing_results(conn: sqlite3.Connection) -> Dict:
    return load_existing_results_incremental(conn, {})


def _get_validation_results_last_loaded_rowid(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute(
            "SELECT value FROM validator_runtime_meta WHERE key = ?",
            (_VALIDATOR_META_KEY_LAST_ROWID,),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if not row:
        return 0
    try:
        return max(0, int(str(row[0]).strip()))
    except Exception:
        return 0


def _set_validation_results_last_loaded_rowid(conn: sqlite3.Connection, rowid: int) -> None:
    try:
        conn.execute(
            """
            INSERT INTO validator_runtime_meta(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (_VALIDATOR_META_KEY_LAST_ROWID, str(max(0, int(rowid)))),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # load_existing_results can be called with ad-hoc in-memory connections
        # in tests that do not initialize full schema.
        pass


def load_existing_results_incremental(
    conn: sqlite3.Connection,
    existing_results: Dict[str, Dict],
) -> Dict[str, Dict]:
    """Load validation_results with a rowid watermark.

    First cycle does one full bootstrap; subsequent cycles only load new rowid
    deltas to reduce per-cycle SQLite read and memory pressure on large tables.
    """
    last_loaded_rowid = _get_validation_results_last_loaded_rowid(conn)
    current_max_rowid = 0
    try:
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM validation_results").fetchone()
        current_max_rowid = int(row[0]) if row and row[0] is not None else 0
    except Exception:
        current_max_rowid = 0

    query = (
        "SELECT rowid AS _rowid, * FROM validation_results"
        if last_loaded_rowid <= 0
        else "SELECT rowid AS _rowid, * FROM validation_results WHERE rowid > ?"
    )
    params: Tuple[int, ...] = tuple() if last_loaded_rowid <= 0 else (last_loaded_rowid,)
    try:
        df_db = pd.read_sql_query(query, conn, params=params)
        if not df_db.empty:
            for _, r in df_db.iterrows():
                key = str(r["bet_id"]) if pd.notnull(r["bet_id"]) else f"{r['player_id']}_{r['alert_ts']}"
                existing_results[key] = r.to_dict()
            if "_rowid" in df_db.columns:
                _max = pd.to_numeric(df_db["_rowid"], errors="coerce").max()
                if pd.notna(_max):
                    current_max_rowid = max(current_max_rowid, int(_max))
    except Exception:
        pass

    # Legacy CSV fallback only for initial bootstrap.
    if last_loaded_rowid <= 0 and RESULTS_PATH.exists():
        try:
            df_old = pd.read_csv(RESULTS_PATH)
            for _, r in df_old.iterrows():
                key = str(r["bet_id"]) if pd.notnull(r["bet_id"]) else f"{r['player_id']}_{r['alert_ts']}"
                if key not in existing_results:
                    existing_results[key] = r.to_dict()
        except Exception:
            pass

    if current_max_rowid > last_loaded_rowid:
        _set_validation_results_last_loaded_rowid(conn, current_max_rowid)
    return existing_results


def save_validation_results(conn: sqlite3.Connection, final_df: pd.DataFrame) -> None:
    if final_df.empty:
        return

    def _s(v: object) -> Optional[str]:
        try:
            return None if pd.isna(v) else str(v)
        except (TypeError, ValueError):
            return str(v) if v is not None else None

    def _session_id_safe(v: object) -> Optional[str]:
        """Safe session_id for DB: None/NaN -> None; numeric -> str(int); else str(v) or None on error."""
        if v is None or pd.isna(v):
            return None
        try:
            if isinstance(v, (int, float)):
                return str(int(v))
            return str(v)
        except (TypeError, ValueError):
            return str(v) if v is not None else None

    rows = [
        (
            _s(r.bet_id),
            getattr(r, "alert_ts", None),
            getattr(r, "validated_at", None),
            None if pd.isna(r.player_id) else int(r.player_id),
            _s(getattr(r, "casino_player_id", None)),
            _s(getattr(r, "canonical_id", None)),
            _s(r.table_id),
            getattr(r, "position_idx", None),
            _session_id_safe(getattr(r, "session_id", None)),
            getattr(r, "score", None),
            None if pd.isna(r.result) else int(bool(r.result)),
            getattr(r, "gap_start", None),
            getattr(r, "gap_minutes", None),
            getattr(r, "reason", None),
            getattr(r, "bet_ts", None),
            _s(getattr(r, "model_version", None)),
        )
        for r in final_df.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO validation_results(
            bet_id, alert_ts, validated_at, player_id, casino_player_id, canonical_id, table_id,
            position_idx, session_id, score, result, gap_start, gap_minutes,
            reason, bet_ts, model_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bet_id) DO UPDATE SET
            alert_ts=excluded.alert_ts,
            validated_at=excluded.validated_at,
            player_id=excluded.player_id,
            casino_player_id=excluded.casino_player_id,
            canonical_id=excluded.canonical_id,
            table_id=excluded.table_id,
            position_idx=excluded.position_idx,
            session_id=excluded.session_id,
            score=excluded.score,
            result=excluded.result,
            gap_start=excluded.gap_start,
            gap_minutes=excluded.gap_minutes,
            reason=excluded.reason,
            bet_ts=excluded.bet_ts,
            model_version=excluded.model_version
        """,
        rows,
    )
    conn.commit()


# ------------------ Validation logic ------------------
def parse_alerts(conn: sqlite3.Connection) -> pd.DataFrame:
    retention_days = getattr(config, "VALIDATOR_ALERT_RETENTION_DAYS", None)
    try:
        df = pd.read_sql_query("SELECT * FROM alerts", conn)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
            df["bet_ts"] = pd.to_datetime(df.get("bet_ts"), errors="coerce")
            # Stored naive datetimes are HK local (scorer writes tz-naive HK); do not treat as UTC.
            df["ts"] = df["ts"].dt.tz_localize(HK_TZ) if df["ts"].dt.tz is None else df["ts"].dt.tz_convert(HK_TZ)
            if "bet_ts" in df.columns:
                df["bet_ts"] = df["bet_ts"].dt.tz_localize(HK_TZ) if df["bet_ts"].dt.tz is None else df["bet_ts"].dt.tz_convert(HK_TZ)
            if retention_days is not None and retention_days > 0:
                cutoff = datetime.now(HK_TZ) - timedelta(days=retention_days)
                df = df[df["ts"] >= cutoff]
            return df
    except Exception:
        pass

    return pd.DataFrame()


def find_gap_within_window(alert_ts: datetime, bet_times: List[datetime], base_start: Optional[datetime] = None) -> Tuple[bool, Optional[datetime], float]:
    """Return (is_true, gap_start, gap_minutes). Gap must start within ALERT_HORIZON_MIN of alert and last >= WALKAWAY_GAP_MIN (values from config). Gap start must be >= alert_ts (labels parity)."""
    horizon_end = alert_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN)
    bet_times = [t for t in bet_times if t >= alert_ts and t <= horizon_end]
    bet_times.sort()

    current_start = base_start or alert_ts
    for bt in bet_times:
        gap_minutes = (bt - current_start).total_seconds() / 60.0
        start_ok = (current_start - alert_ts).total_seconds() >= 0 and (current_start - alert_ts).total_seconds() / 60.0 <= config.ALERT_HORIZON_MIN
        if gap_minutes >= config.WALKAWAY_GAP_MIN and start_ok:
            return True, current_start, gap_minutes
        current_start = bt
    # Tail gap to horizon end
    gap_minutes = (horizon_end - current_start).total_seconds() / 60.0
    start_ok = (current_start - alert_ts).total_seconds() >= 0 and (current_start - alert_ts).total_seconds() / 60.0 <= config.ALERT_HORIZON_MIN
    if gap_minutes >= config.WALKAWAY_GAP_MIN and start_ok:
        return True, current_start, gap_minutes
    return False, None, 0.0


def _norm_casino_player_id(v: Any) -> Optional[str]:
    """Normalize casino_player_id: None/pd.NA/empty or whitespace -> None (FND-03 / Review §1)."""
    if v is None or pd.isna(v):
        return None
    s = str(v).strip()
    return s if s else None


def validate_alert_row(
    row: pd.Series,
    bet_cache: Dict[str, List[datetime]],
    session_cache: Dict[str, List[Dict]],
    force_finalize: bool = False,
) -> Dict:
    """Validate a single alert row.

    ``bet_cache`` is now keyed by ``canonical_id`` (str) to support rated players
    whose bets may span multiple ``player_id``s.  A player_id-based fallback is
    still used when canonical_id is absent (e.g., legacy alerts).

    Verdict (MATCH/MISS/PENDING) is bet-based only; session_cache is not used for
    verdict (retained for API compatibility).
    """
    score_ts = pd.to_datetime(row["ts"])
    if score_ts.tzinfo is None:
        score_ts = score_ts.tz_localize(HK_TZ)
    else:
        score_ts = score_ts.tz_convert(HK_TZ)
    bet_ts = row.get("bet_ts")
    if pd.isna(bet_ts):
        bet_ts = score_ts
    else:
        bet_ts = pd.to_datetime(bet_ts)
        if bet_ts.tzinfo is None:
            bet_ts = bet_ts.tz_localize(HK_TZ)
        else:
            bet_ts = bet_ts.tz_convert(HK_TZ)
    player_id = row.get("player_id")
    bet_id = row.get("bet_id")

    # Resolve canonical_id for bet_cache lookup (step 8)
    cid_raw = row.get("canonical_id")
    if cid_raw is None or pd.isna(cid_raw) or str(cid_raw).strip() == "":
        canonical_id = str(int(player_id)) if pd.notna(player_id) else None
    else:
        canonical_id = str(cid_raw)

    model_version = row.get("model_version")

    now_hk = datetime.now(HK_TZ)

    # Template for result with all columns
    res_base: Dict[str, Any] = {col: None for col in VALIDATION_COLUMNS}
    res_base.update(
        {
            "alert_ts": score_ts.isoformat(),
            "validated_at": now_hk.isoformat(),
            "bet_ts": bet_ts.isoformat(),
            "bet_id": bet_id,
            "score": row.get("score"),
            "player_id": int(player_id) if pd.notna(player_id) else None,
            "casino_player_id": _norm_casino_player_id(row.get("casino_player_id")),
            "canonical_id": canonical_id,
            "table_id": row.get("table_id"),
            "position_idx": row.get("position_idx"),
            "session_id": row.get("session_id"),
            "model_version": model_version if pd.notna(model_version) else None,
        }
    )

    if pd.isna(player_id):
        res_base.update({"result": False, "reason": "missing_player_id"})
        return res_base

    # Only validate after the bet has aged past the alert horizon plus a small buffer
    freshness_buffer_min = getattr(config, "VALIDATOR_FRESHNESS_BUFFER_MINUTES", 2)
    wait_minutes = config.LABEL_LOOKAHEAD_MIN + max(0, freshness_buffer_min)
    if bet_ts > now_hk - timedelta(minutes=wait_minutes):
        return {"result": None}  # too recent; special key to signal skip

    # Use canonical_id to look up the merged bet list for this player (step 8).
    # Falls back to player_id lookup for legacy bet_cache entries.
    if canonical_id is not None and canonical_id in bet_cache:
        bet_list = bet_cache[canonical_id]
    else:
        bet_list = bet_cache.get(str(int(player_id)) if pd.notna(player_id) else "", [])

    # Do not conclude MATCH when we have no bet data (e.g. fetch failed, wrong range, or TZ mismatch).
    # Otherwise find_gap_within_window(..., []) would treat "no bets in window" as a LABEL_LOOKAHEAD_MIN gap and we'd falsely MATCH.
    if not bet_list:
        logger.warning(
            "[validator] No bet data for canonical_id=%s player_id=%s bet_id=%s — leaving PENDING (cannot verify late arrivals)",
            canonical_id, player_id, bet_id,
        )
        res_base.update({"result": None, "reason": "PENDING"})
        return res_base

    # Find last bet before bet_ts; keep as base_start context only.
    idx = bisect_left(bet_list, bet_ts)
    last_bet_before = bet_list[idx - 1] if idx > 0 else None
    base_start = last_bet_before or bet_ts

    # Bets within LABEL_LOOKAHEAD_MIN horizon after bet_ts (bet-based only; session_cache not used for verdict)
    horizon_end = bet_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN)
    right_idx = bisect_right(bet_list, horizon_end)
    bet_times = bet_list[idx:right_idx]

    # Bet-gap check (aligned with trainer/labels.py compute_labels; DEC-030)
    is_true, gap_start, gap_minutes = find_gap_within_window(bet_ts, bet_times, base_start=base_start)
    
    # If a gap was found via bets, we verify if it was within ALERT_HORIZON_MIN or late (merged to MISS)
    if is_true:
        # A detected gap is a candidate MATCH, but per policy we allow a short
        # extended wait window for late-arriving data (arrivals with timestamps
        # in the (ALERT_HORIZON_MIN, LABEL_LOOKAHEAD_MIN] interval) before finalizing MATCH.
        res_base.update({
            "result": None,
            "gap_start": gap_start.isoformat() if gap_start is not None else None,
            "gap_minutes": gap_minutes,
            "reason": "PENDING"
        })
        # If we're past the extended wait window, finalize now by checking whether any
        # late arrival (bet only) appeared whose timestamp falls within the horizon window.
        extended_wait = getattr(config, 'VALIDATOR_EXTENDED_WAIT_MINUTES', 15)
        late_threshold = bet_ts + timedelta(minutes=config.ALERT_HORIZON_MIN)
        horizon_end = bet_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN)
        extended_end = bet_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN + extended_wait)

        if now_hk >= extended_end or force_finalize:
            any_late_bet_in_window = any((bt > late_threshold and bt <= horizon_end) for bt in bet_list)
            _gs_iso = gap_start.isoformat() if gap_start is not None else None
            if any_late_bet_in_window:
                res_base.update({
                    "result": False,
                    "gap_start": _gs_iso,
                    "gap_minutes": gap_minutes,
                    "reason": "MISS"
                })
                logger.debug("[validator] Finalizing candidate as MISS (late arrival in horizon window or forced) player=%s bet_id=%s", player_id, bet_id)
            else:
                res_base.update({
                    "result": True,
                    "gap_start": _gs_iso,
                    "gap_minutes": gap_minutes,
                    "reason": "MATCH"
                })
                logger.debug("[validator] Finalizing candidate as MATCH (no late arrivals in horizon window or forced) player=%s bet_id=%s", player_id, bet_id)
    else:
        # No gap found within the horizon.
        # Policy:
        #  - If any bet exists after bet_ts + ALERT_HORIZON_MIN and within LABEL_LOOKAHEAD_MIN horizon,
        #    we can immediately conclude MISS (final at horizon).
        #  - Otherwise, if VALIDATOR_FINALIZE_ON_HORIZON is enabled, wait an extra
        #    VALIDATOR_EXTENDED_WAIT_MINUTES before finalizing; during this period we
        #    return a special {'result': None} to indicate re-check later.
        extended_wait = getattr(config, 'VALIDATOR_EXTENDED_WAIT_MINUTES', 15)
        late_threshold = bet_ts + timedelta(minutes=config.ALERT_HORIZON_MIN)
        horizon_end = bet_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN)
        extended_end = bet_ts + timedelta(minutes=config.LABEL_LOOKAHEAD_MIN + extended_wait)

        # Check for any bets after ALERT_HORIZON_MIN threshold up to LABEL_LOOKAHEAD_MIN horizon -> immediate MISS (bet-based only)
        any_late_bet_within_horizon = any((bt > late_threshold and bt <= horizon_end) for bt in bet_list)

        if any_late_bet_within_horizon:
            res_base.update({
                "result": False,
                "gap_start": None,
                "gap_minutes": 0,
                "reason": "MISS"
            })
            logger.debug("[validator] Finalizing alert as MISS (evidence within horizon) player=%s bet_id=%s", player_id, bet_id)
        else:
            if getattr(config, 'VALIDATOR_FINALIZE_ON_HORIZON', False):
                # Still within extended wait window -> skip (to be re-checked later)
                if now_hk < extended_end and not force_finalize:
                    return {"result": None}

                # Either extended window passed or force_finalize requested; check for any late arrivals (bet only)
                # whose timestamps fall within the horizon window after bet_ts.
                any_late_bet_in_extended = any((bt > late_threshold and bt <= horizon_end) for bt in bet_list)

                if any_late_bet_in_extended:
                    res_base.update({
                        "result": False,
                        "gap_start": None,
                        "gap_minutes": 0,
                        "reason": "MISS"
                    })
                    logger.debug("[validator] Finalizing alert as MISS (late arrival in horizon window) player=%s bet_id=%s", player_id, bet_id)
                else:
                    # No late arrivals in the horizon window -> confirm MATCH
                    res_base.update({
                        "result": True,
                        "gap_start": None,
                        "gap_minutes": 0,
                        "reason": "MATCH"
                    })
                    logger.debug("[validator] Finalizing alert as MATCH (no late arrivals in horizon window) player=%s bet_id=%s", player_id, bet_id)
            else:
                res_base.update({
                    "result": False,
                    "gap_start": None,
                    "gap_minutes": 0,
                    "reason": "PENDING"
                })
    return res_base


# ------------------ Main loop ------------------
def validate_once(conn: sqlite3.Connection, force_finalize: bool = False) -> None:
    cycle_stage_seconds: Dict[str, float] = {}
    now_hk = datetime.now(HK_TZ)
    t_sqlite = time.perf_counter()
    prune_validator_retention(conn, now_hk)
    cycle_stage_seconds["sqlite"] = time.perf_counter() - t_sqlite

    t_sqlite = time.perf_counter()
    alerts = parse_alerts(conn)
    cycle_stage_seconds["sqlite"] += time.perf_counter() - t_sqlite
    if alerts.empty:
        logger.debug("[validator] No alerts to validate")
        _emit_validator_perf_summary(cycle_stage_seconds)
        return

    t_sqlite = time.perf_counter()
    processed = {str(bid) for bid in load_processed(conn)}
    cycle_stage_seconds["sqlite"] += time.perf_counter() - t_sqlite
    alerts["bet_id_str"] = alerts["bet_id"].astype(str)
    pending_all = alerts[~alerts["bet_id_str"].isin(processed)].copy()
    if pending_all.empty:
        logger.debug("[validator] Alerts: %d, Pending: 0 (all processed)", len(alerts))
        _emit_validator_perf_summary(cycle_stage_seconds)
        return

    freshness_buffer_min = getattr(config, "VALIDATOR_FRESHNESS_BUFFER_MINUTES", 2)
    wait_minutes = config.LABEL_LOOKAHEAD_MIN + max(0, freshness_buffer_min)
    cutoff = now_hk - timedelta(minutes=wait_minutes)
    finality_cutoff = now_hk - timedelta(hours=getattr(config, 'VALIDATOR_FINALITY_HOURS', 1))

    effective_ts = pd.to_datetime(pending_all["bet_ts"].fillna(pending_all["ts"]))
    if effective_ts.dt.tz is None:
        effective_ts = effective_ts.dt.tz_localize(HK_TZ)
    else:
        effective_ts = effective_ts.dt.tz_convert(HK_TZ)

    # Debug: bet_ts and effective_ts range for pending alerts (diagnose "all too recent")
    bet_ts_ser = pending_all["bet_ts"]
    if bet_ts_ser.notna().any():
        bet_ts_valid = pd.to_datetime(bet_ts_ser.dropna())
        if bet_ts_valid.dt.tz is None:
            bet_ts_valid = bet_ts_valid.dt.tz_localize(HK_TZ)
        else:
            bet_ts_valid = bet_ts_valid.dt.tz_convert(HK_TZ)
        logger.debug(
            "[validator] pending_all: n=%d, bet_ts min=%s, bet_ts max=%s, effective_ts min=%s, effective_ts max=%s, cutoff=%s (wait_min=%s)",
            len(pending_all), bet_ts_valid.min(), bet_ts_valid.max(),
            effective_ts.min(), effective_ts.max(), cutoff, wait_minutes,
        )
    else:
        logger.debug(
            "[validator] pending_all: n=%d, bet_ts all NaT (using ts); effective_ts min=%s, max=%s, cutoff=%s (wait_min=%s)",
            len(pending_all), effective_ts.min(), effective_ts.max(), cutoff, wait_minutes,
        )

    pending = pending_all[effective_ts <= cutoff].copy()
    if pending.empty:
        logger.debug("[validator] %d pending, but all are too recent (<%sm)", len(pending_all), wait_minutes)
        _emit_validator_perf_summary(cycle_stage_seconds)
        return

    if force_finalize:
        logger.warning("[validator] running with --force-finalize; PENDING candidates will be finalized now")

    logger.debug("[validator] Processing %d alerts (including re-checks)...", len(pending))

    t_sqlite = time.perf_counter()
    existing_results = load_existing_results_incremental(conn, {})
    cycle_stage_seconds["sqlite"] += time.perf_counter() - t_sqlite

    # Build canonical_id → [player_ids] mapping from all relevant alerts (step 8)
    cid_to_pids = _build_cid_to_player_ids(alerts)

    player_ids = (
        pending.loc[pending["player_id"].notna(), "player_id"]
        .astype(int)
        .unique()
        .tolist()
    )
    try:
        processed_players = (
            alerts[alerts["bet_id_str"].isin(processed)]["player_id"]
            .dropna()
            .astype(int)
            .unique()
            .tolist()
        )
        for pid in processed_players:
            if pid not in player_ids:
                player_ids.append(pid)
    except Exception:
        pass

    bet_cache: Dict[str, List[datetime]] = {}
    # Phase 1 (Task 3): keep validate_alert_row signature compatibility, but
    # disable session fetch/query path in validator cycle to cut ClickHouse load.
    session_cache_disabled: Dict[str, List[Dict]] = {}
    if player_ids or cid_to_pids:
        if get_clickhouse_client is None:
            raise RuntimeError(
                "Validator requires ClickHouse to fetch bets/sessions for validation; "
                "get_clickhouse_client is unavailable. Run as package (e.g. python -m trainer.validator)."
            )
        fetch_start = effective_ts[pending.index].min() - timedelta(hours=1)
        fetch_end = now_hk
        try:
            t_clickhouse = time.perf_counter()
            # R41: errors propagate so the cycle aborts rather than producing false MISSes
            bet_cache = fetch_bets_by_canonical_id(cid_to_pids, fetch_start, fetch_end)
            cycle_stage_seconds["clickhouse"] = time.perf_counter() - t_clickhouse
        except Exception as exc:
            logger.warning("[validator] DB fetch error — skipping this validation cycle: %s", exc)
            _emit_validator_perf_summary(cycle_stage_seconds)
            return

    new_processed_ids: List = []
    updated_count = 0
    # bet_id (str) -> verdict reason for one INFO summary line this cycle
    cycle_bet_to_reason: Dict[str, str] = {}

    for bid in list(processed):
        key = str(bid)
        if key not in existing_results:
            try:
                match = alerts[alerts["bet_id_str"] == key]
                if not match.empty:
                    r = validate_alert_row(
                        match.iloc[0],
                        bet_cache,
                        session_cache_disabled,
                        force_finalize=force_finalize,
                    )
                    if r.get("result") is not None:
                        existing_results[key] = r
            except Exception:
                continue

    for key, saved_row in list(existing_results.items()):
        try:
            if saved_row.get("reason") == "PENDING":
                pending_bid = saved_row.get("bet_id")
                match = (
                    alerts[alerts["bet_id_str"] == str(pending_bid)]
                    if pd.notna(pending_bid)
                    else pd.DataFrame()
                )
                if not match.empty:
                    newr = validate_alert_row(
                        match.iloc[0],
                        bet_cache,
                        session_cache_disabled,
                        force_finalize=force_finalize,
                    )
                    if newr.get("result") is not None and (newr.get("reason") != "PENDING"):
                        existing_results[key] = newr
                        updated_count += 1
                        _pb = newr.get("bet_id")
                        if _pb is None:
                            _pb = saved_row.get("bet_id")
                        if _pb is not None and pd.notna(_pb):
                            cycle_bet_to_reason[str(_pb)] = str(newr.get("reason", "UNKNOWN"))
        except Exception:
            continue

    for _, row in pending.iterrows():
        res = validate_alert_row(
            row,
            bet_cache,
            session_cache_disabled,
            force_finalize=force_finalize,
        )
        if res.get("result") is None:
            continue

        bid = str(row["bet_id"])
        key = bid if bid != "nan" else f"{row['player_id']}_{row['ts']}"

        is_new = key not in existing_results
        # Treat stored result as MATCH only when explicitly True/1/1.0 (R393: NaN/0/None allow upgrade to MATCH)
        stored = existing_results.get(key, {}).get("result")
        stored_is_match = (
            stored is True
            or stored == 1
            or (isinstance(stored, float) and not pd.isna(stored) and stored == 1.0)
        )
        is_upgrade = not is_new and res["result"] and not stored_is_match
        was_pending = not is_new and existing_results.get(key, {}).get("reason") == "PENDING"
        is_finalize = was_pending and res.get("reason") == "MISS"

        if res.get("reason") in IGNORED_REASONS:
            existing_results[key] = res
            processed.add(row["bet_id"])
            cycle_bet_to_reason[str(row["bet_id"])] = str(res.get("reason", "UNKNOWN"))
            new_processed_ids.append(row["bet_id"])
            continue

        if is_new or is_upgrade or is_finalize:
            existing_results[key] = res
            if is_upgrade or is_finalize:
                updated_count += 1

        alert_dt = pd.to_datetime(row["bet_ts"] if pd.notnull(row["bet_ts"]) else row["ts"])
        if alert_dt.tzinfo is None:
            alert_dt = alert_dt.replace(tzinfo=HK_TZ)

        if res["result"] or alert_dt <= finality_cutoff:
            if not res["result"]:
                res["reason"] = "MISS"
                existing_results[key] = res

            processed.add(row["bet_id"])
            cycle_bet_to_reason[str(row["bet_id"])] = str(res.get("reason", "UNKNOWN"))
            new_processed_ids.append(row["bet_id"])

    if cycle_bet_to_reason:
        _vc = Counter(cycle_bet_to_reason.values())
        _parts = ", ".join(f"{_r}={_vc[_r]}" for _r in sorted(_vc.keys()))
        logger.info(
            "[validator] This cycle: %d alert(s) verified — %s",
            len(cycle_bet_to_reason),
            _parts,
        )

    if existing_results:
        final_df = pd.DataFrame(list(existing_results.values()))
        # Ensure all expected columns are present (fill missing with None)
        for col in VALIDATION_COLUMNS:
            if col not in final_df.columns:
                final_df[col] = None
        final_df = final_df[VALIDATION_COLUMNS]

        kpi_df = final_df[~final_df["reason"].isin(IGNORED_REASONS)]
        finalized_or_old = kpi_df[kpi_df["reason"] != "PENDING"].copy()
        precision_15m, matches_15m, total_15m = _rolling_precision_by_alert_ts(
            finalized_or_old, now_hk=now_hk, window=timedelta(minutes=15)
        )
        precision_1h, matches_1h, total_1h = _rolling_precision_by_alert_ts(
            finalized_or_old, now_hk=now_hk, window=timedelta(hours=1)
        )
        logger.info(
            "[validator] Cumulative Precision (15m window): %.2f%% (%d/%d)",
            precision_15m * 100,
            matches_15m,
            total_15m,
        )
        logger.info(
            "[validator] Cumulative Precision (1h window): %.2f%% (%d/%d)",
            precision_1h * 100,
            matches_1h,
            total_1h,
        )

        try:
            _mv = _latest_model_version_from_alerts(alerts)
            _append_validator_metrics(
                conn,
                recorded_at=now_hk.isoformat(),
                model_version=_mv,
                precision=float(precision_15m),
                total=int(total_15m),
                matches=int(matches_15m),
            )
        except Exception as exc:
            logger.warning("[validator] validator_metrics insert failed: %s", exc)

        final_df["alert_ts_dt"] = pd.to_datetime(final_df["alert_ts"])
        final_df = final_df.sort_values("alert_ts_dt").drop(columns=["alert_ts_dt"])
        t_sqlite = time.perf_counter()
        save_validation_results(conn, final_df)
        cycle_stage_seconds["sqlite"] += time.perf_counter() - t_sqlite
        logger.info(
            "[validator] Saved %d total validations to SQLite (Updated %d, Finalized %d)",
            len(final_df), updated_count, len(new_processed_ids),
        )

    t_sqlite = time.perf_counter()
    mark_processed(conn, new_processed_ids)
    cycle_stage_seconds["sqlite"] += time.perf_counter() - t_sqlite
    _emit_validator_perf_summary(cycle_stage_seconds)
    return


def run_validator_loop(
    interval_seconds: int = 60,
    once: bool = False,
    force_finalize: bool = False,
) -> None:
    """Run the validator loop (no argparse). Used by package/deploy/main.py.
    Uses STATE_DB_PATH from env if set.
    """
    conn = get_db_conn()
    while True:
        start_time = time.time()
        try:
            validate_once(conn, force_finalize=force_finalize)
        except Exception as exc:
            logger.exception("[validator] ERROR: %s", exc)
        if once:
            break
        elapsed = time.time() - start_time
        sleep_time = max(0, interval_seconds - elapsed)
        time.sleep(sleep_time)


def main():
    # Ensure console logs include timestamp (when not already set by deploy main)
    if not logging.root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    parser = argparse.ArgumentParser(description="Validate alerts against realized walkaways")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run a single validation pass and exit")
    parser.add_argument("--force-finalize", action="store_true", help="Force-finalize PENDING candidates immediately (for manual runs)")
    args = parser.parse_args()

    conn = get_db_conn()
    interval = args.interval

    while True:
        start_time = time.time()
        try:
            validate_once(conn, force_finalize=args.force_finalize)
        except Exception as exc:
            logger.exception("[validator] ERROR: %s", exc)
        if args.once:
            break
        # Sleep to next tick (preventing overlap)
        elapsed = time.time() - start_time
        sleep_time = max(0, interval - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
