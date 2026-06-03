"""SQLite ``state.db`` initialization (trainer-compatible ``alerts`` / ``validation_results`` schema)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from trainer_hightier.config import default_hightier_serving_config
from trainer_hightier.serving.contracts import (
    ALERTS_MIGRATION_COLUMNS,
    META_KEY_LAST_ETL_WATERMARK,
    META_KEY_SCHEMA_VERSION,
    NEW_ALERT_COLUMNS,
    STATE_SCHEMA_VERSION,
    VALIDATION_RESULTS_BASE_MIGRATION_COLUMNS,
    VALIDATION_RESULTS_PHASE1_MIGRATION_COLUMNS,
)

logger = logging.getLogger(__name__)


def apply_sqlite_serving_pragmas(conn: sqlite3.Connection) -> None:
    """Best-effort WAL settings for concurrent scorer/validator/API."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows}


def _ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    migrations: tuple[tuple[str, str], ...],
) -> None:
    have = _existing_columns(conn, table)
    for col_name, col_type in migrations:
        if col_name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")


def init_state_db(path: Optional[Path] = None) -> Path:
    """Create or migrate shared state DB; idempotent and safe to call on every process start.

    Returns
    -------
    Path
        Resolved DB path.
    """
    cfg = default_hightier_serving_config()
    db_path = Path(path or cfg.state_db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        apply_sqlite_serving_pragmas(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_stats (
                session_id TEXT PRIMARY KEY,
                bet_count INTEGER NOT NULL,
                sum_wager REAL NOT NULL,
                first_ts TEXT,
                last_ts TEXT,
                player_id TEXT,
                table_id TEXT,
                updated_at TEXT
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
        # Phase-1 columns + casino_player_id (ML API protocol)
        _ensure_columns(conn, "alerts", NEW_ALERT_COLUMNS)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_player ON alerts(player_id)")
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_validation_alert_ts ON validation_results(alert_ts)"
        )
        _ensure_columns(conn, "validation_results", VALIDATION_RESULTS_BASE_MIGRATION_COLUMNS)
        _ensure_columns(conn, "validation_results", VALIDATION_RESULTS_PHASE1_MIGRATION_COLUMNS)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_alerts (
                bet_id TEXT PRIMARY KEY,
                processed_ts TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_last_ts ON session_stats(last_ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_player ON session_stats(player_id)"
        )
        _init_validator_aux_tables(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_player_game ON alerts(player_id, game_id)"
        )
        _init_runtime_rated_threshold(conn)
        _meta_set_if_missing(conn, META_KEY_SCHEMA_VERSION, STATE_SCHEMA_VERSION)
        conn.commit()
    logger.debug("[state_db] initialized %s", db_path)
    return db_path


def _init_validator_aux_tables(conn: sqlite3.Connection) -> None:
    """Tables created by ``trainer.serving.validator.get_db_conn`` (metrics + meta)."""
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
        CREATE TABLE IF NOT EXISTS validator_runtime_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    have_alerts = _existing_columns(conn, "alerts")
    for col_name, col_type in ALERTS_MIGRATION_COLUMNS:
        if col_name not in have_alerts:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col_name} {col_type}")


def _init_runtime_rated_threshold(conn: sqlite3.Connection) -> None:
    """Optional override table (trainer T-OnlineCalibration); keep for schema parity."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_rated_threshold (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            rated_threshold REAL NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT,
            n_mature INTEGER,
            n_pos INTEGER,
            window_hours REAL,
            recall_at_threshold REAL,
            precision_at_threshold REAL,
            selection_mode TEXT
        )
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runtime_rated_threshold)").fetchall()}
    if cols and "selection_mode" not in cols:
        conn.execute("ALTER TABLE runtime_rated_threshold ADD COLUMN selection_mode TEXT")


def _meta_set_if_missing(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (key, value),
    )


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    return None if row[0] is None else str(row[0])


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def get_last_processed_etl_insert(conn: sqlite3.Connection) -> Optional[pd.Timestamp]:
    raw = meta_get(conn, META_KEY_LAST_ETL_WATERMARK)
    if not raw:
        return None
    t = pd.to_datetime(raw, errors="coerce")
    if pd.isna(t):
        return None
    return pd.Timestamp(t)


def set_last_processed_etl_insert(conn: sqlite3.Connection, dt: datetime) -> None:
    meta_set(conn, META_KEY_LAST_ETL_WATERMARK, pd.Timestamp(dt).isoformat())


def append_alerts(conn: sqlite3.Connection, alerts_df: pd.DataFrame) -> None:
    """Upsert alert rows (trainer-compatible Phase-1 column set)."""

    def _s(v: object) -> Optional[str]:
        try:
            return None if pd.isna(v) else str(v)
        except (TypeError, ValueError):
            return str(v) if v is not None else None

    def _f(v: object) -> Optional[float]:
        try:
            return float(v) if v is not None else None  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _i(v: object) -> Optional[int]:
        try:
            return int(v) if v is not None else None  # type: ignore[arg-type, call-overload]
        except (TypeError, ValueError):
            return None

    def _ts(v: object) -> Optional[str]:
        try:
            return pd.to_datetime(v).isoformat() if pd.notna(v) else None
        except Exception:
            return None

    def _cid(v: object) -> Optional[str]:
        if v is None or pd.isna(v):
            return None
        s = str(v).strip()
        return s if s else None

    if alerts_df.empty:
        return
    rows = [
        (
            _s(r.bet_id),
            _ts(r.ts),
            _ts(r.bet_ts),
            _s(r.player_id),
            _cid(getattr(r, "casino_player_id", None)),
            _s(r.table_id),
            _f(r.position_idx),
            _ts(r.visit_start_ts),
            _ts(r.visit_end_ts),
            _i(r.session_count),
            _i(r.bet_count),
            _f(r.visit_avg_bet),
            _f(r.historical_avg_bet),
            _f(r.score),
            _s(r.session_id),
            _i(r.loss_streak),
            _f(r.bets_last_5m),
            _f(r.bets_last_15m),
            _f(r.bets_last_30m),
            _f(r.wager_last_10m),
            _f(r.wager_last_30m),
            _f(r.cum_bets),
            _f(r.cum_wager),
            _f(getattr(r, "avg_wager_sofar", None)),
            _f(getattr(r, "session_duration_min", 0.0)),
            _f(getattr(r, "bets_per_minute", 0.0)),
            _s(getattr(r, "canonical_id", None)),
            _i(getattr(r, "is_rated_obs", None)),
            _s(getattr(r, "reason_codes", None)),
            _s(getattr(r, "model_version", None)),
            _f(getattr(r, "margin", None)),
            _ts(getattr(r, "scored_at", None)),
            _s(getattr(r, "game_id", None)),
            _f(getattr(r, "player_game_score", None)),
            _i(getattr(r, "player_game_bet_count", None)),
        )
        for r in alerts_df.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO alerts(
            bet_id, ts, bet_ts, player_id, casino_player_id, table_id, position_idx,
            visit_start_ts, visit_end_ts, session_count, bet_count,
            visit_avg_bet, historical_avg_bet, score, session_id,
            loss_streak, bets_last_5m, bets_last_15m, bets_last_30m,
            wager_last_10m, wager_last_30m, cum_bets, cum_wager,
            avg_wager_sofar, session_duration_min, bets_per_minute,
            canonical_id, is_rated_obs, reason_codes, model_version,
            margin, scored_at, game_id, player_game_score, player_game_bet_count
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(bet_id) DO UPDATE SET
            ts=excluded.ts,
            bet_ts=excluded.bet_ts,
            score=excluded.score,
            canonical_id=excluded.canonical_id,
            is_rated_obs=excluded.is_rated_obs,
            reason_codes=excluded.reason_codes,
            model_version=excluded.model_version,
            margin=excluded.margin,
            scored_at=excluded.scored_at,
            casino_player_id=excluded.casino_player_id,
            game_id=excluded.game_id,
            player_game_score=excluded.player_game_score,
            player_game_bet_count=excluded.player_game_bet_count
        """,
        rows,
    )


def connect_state_db(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open SQLite connection with WAL pragmas (caller closes)."""
    cfg = default_hightier_serving_config()
    db_path = Path(path or cfg.state_db_path).resolve()
    init_state_db(db_path)
    conn = sqlite3.connect(db_path)
    apply_sqlite_serving_pragmas(conn)
    return conn
