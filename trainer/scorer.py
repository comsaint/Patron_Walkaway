"""trainer/scorer.py — Phase 1 Refactor
==========================================
Near real-time scoring daemon.

Key changes from pre-Phase-1 version
--------------------------------------
* Single rated-model artifact: model.pkl (v10 DEC-021; falls back to
  rated_model.pkl or legacy walkaway_model.pkl when model.pkl is absent).
* D2 identity resolution via identity.py build_canonical_mapping_from_df.
* Track B features via features.py (compute_loss_streak / compute_run_boundary)
  — guarantees train-serve parity with trainer.py. (table_hc deferred to Phase 2.)
* player_profile PIT join (R79): rated bets enriched with player profile
  features via as-of merge (snapshot_dtm <= bet_time); profile features stay NaN
  for non-rated bets and bets with no prior snapshot.
* H3 model routing: is_rated_obs ← casino_player_id IS NOT NULL.
* FND-01 CTE dedup + session_avail_dtm gate on session query (H2).
* SHAP reason codes -> reason_code_map.json lookup, emitted with every alert.
* New alert DB columns: canonical_id, is_rated_obs, reason_codes,
  model_version, margin, scored_at.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

try:
    from features import (  # type: ignore[import]
        PROFILE_FEATURE_COLS,
        join_player_profile as _join_profile,
        coerce_feature_dtypes,
    )
except ImportError:
    try:
        from trainer.features import (  # type: ignore[import, attr-defined]
            PROFILE_FEATURE_COLS,
            join_player_profile as _join_profile,
            coerce_feature_dtypes,
        )
    except ImportError:
        PROFILE_FEATURE_COLS = []  # type: ignore[assignment]
        _join_profile = None  # type: ignore[assignment]
        coerce_feature_dtypes = None  # type: ignore[assignment]

try:
    from db_conn import get_clickhouse_client  # type: ignore[import]
except ImportError:
    try:
        from trainer.db_conn import get_clickhouse_client  # type: ignore[import]
    except ImportError:
        get_clickhouse_client = None  # type: ignore[assignment]

try:
    import config  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as config  # type: ignore[import, no-redef]

try:
    from features import (  # type: ignore[import]
        compute_loss_streak,
        compute_run_boundary,
        compute_track_llm_features,
        load_feature_spec,
    )
except ImportError:
    from trainer.features import (  # type: ignore[import, attr-defined]
        compute_loss_streak,
        compute_run_boundary,
        compute_track_llm_features,
        load_feature_spec,
    )

try:
    from identity import build_canonical_mapping_from_df  # type: ignore[import]
except ImportError:
    from trainer.identity import build_canonical_mapping_from_df  # type: ignore[import, attr-defined]

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
HK_TZ = ZoneInfo(config.HK_TZ)
BASE_DIR = Path(__file__).parent
STATE_DIR = BASE_DIR / "local_state"
STATE_DIR.mkdir(exist_ok=True)
STATE_DB_PATH = STATE_DIR / "state.db"
MODEL_DIR = BASE_DIR / "models"
FEATURE_SPEC_PATH = BASE_DIR / "feature_spec" / "features_candidates.template.yaml"

RETENTION_HOURS: int = getattr(config, "SCORER_STATE_RETENTION_HOURS", 48)
SESSION_AVAIL_DELAY_MIN: int = getattr(config, "SESSION_AVAIL_DELAY_MIN", 15)
BET_AVAIL_DELAY_MIN: int = getattr(config, "BET_AVAIL_DELAY_MIN", 1)
UNRATED_VOLUME_LOG: bool = bool(getattr(config, "UNRATED_VOLUME_LOG", True))
SHAP_TOP_K = 3

# New alert columns added in Phase 1
_NEW_ALERT_COLS: List[Tuple[str, str]] = [
    ("canonical_id", "TEXT"),
    ("is_rated_obs", "INTEGER"),
    ("reason_codes", "TEXT"),
    ("model_version", "TEXT"),
    ("margin", "REAL"),
    ("scored_at", "TEXT"),
]


# ── Artifact loading ──────────────────────────────────────────────────────────
def load_dual_artifacts(model_dir: Optional[Path] = None) -> dict:
    """Load model artifacts, feature list, reason code map, model version.

    Priority (v10): model.pkl → rated_model.pkl → walkaway_model.pkl.

    Returns dict:
        rated        : {"model": lgb.Booster, "threshold": float} or None
        feature_list : List[str]
        reason_code_map : Dict[str, str]
        model_version   : str
        feature_spec    : parsed YAML dict or None
    """
    d = model_dir or MODEL_DIR
    model_path = d / "model.pkl"      # v10 single-model artifact
    rated_path = d / "rated_model.pkl"
    feature_list_path = d / "feature_list.json"
    reason_map_path = d / "reason_code_map.json"
    version_path = d / "model_version"
    legacy_path = d / "walkaway_model.pkl"

    artifacts: dict = {
        "rated": None,
        "feature_list": [],
        "feature_list_meta": None,  # list of {name, track} when from feature_list.json (Step 6)
        "reason_code_map": {},
        "model_version": "unknown",
        "feature_spec": None,
    }

    if version_path.exists():
        artifacts["model_version"] = version_path.read_text(encoding="utf-8").strip()

    # Track LLM: prefer the frozen feature_spec.yaml inside the model artifact
    # directory (DEC-024 / R3507) for exact train-serve reproducibility.
    # Fall back to the global template when no frozen copy exists.
    _frozen_spec = d / "feature_spec.yaml"
    if _frozen_spec.exists():
        try:
            artifacts["feature_spec"] = load_feature_spec(_frozen_spec)
        except Exception as exc:
            logger.warning("[scorer] frozen feature spec not loaded: %s; falling back to global", exc)
    if artifacts["feature_spec"] is None and FEATURE_SPEC_PATH.exists():
        try:
            artifacts["feature_spec"] = load_feature_spec(FEATURE_SPEC_PATH)
        except Exception as exc:
            logger.warning("[scorer] feature spec not loaded: %s", exc)

    if feature_list_path.exists():
        with feature_list_path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
            # trainer writes [{"name": ..., "track": ...}]; normalize to List[str] (R31)
            artifacts["feature_list"] = [
                (entry["name"] if isinstance(entry, dict) else str(entry))
                for entry in raw
            ]
            # feat-consolidation Step 6: keep name+track so profile vs non-profile is YAML-driven
            if raw and isinstance(raw[0], dict) and "track" in raw[0]:
                artifacts["feature_list_meta"] = raw

    if reason_map_path.exists():
        with reason_map_path.open(encoding="utf-8") as fh:
            artifacts["reason_code_map"] = json.load(fh)

    # v10 single-model: prefer model.pkl
    if model_path.exists():
        rb = joblib.load(model_path)
        artifacts["rated"] = {
            "model": rb["model"],
            "threshold": float(rb.get("threshold", 0.5)),
            "features": rb.get("features", []),
        }
        if not artifacts["feature_list"]:
            artifacts["feature_list"] = rb.get("features", [])
        logger.info(
            "[scorer] Single rated model loaded from model.pkl (v=%s, %d features)",
            artifacts["model_version"],
            len(artifacts["feature_list"]),
        )
        return artifacts

    if rated_path.exists():
        rb = joblib.load(rated_path)
        artifacts["rated"] = {
            "model": rb["model"],
            "threshold": float(rb.get("threshold", 0.5)),
            "features": rb.get("features", []),
        }
        if not artifacts["feature_list"]:
            artifacts["feature_list"] = rb.get("features", [])
        logger.info(
            "[scorer] Rated model loaded from rated_model.pkl (v=%s, %d features)",
            artifacts["model_version"],
            len(artifacts["feature_list"]),
        )
        return artifacts

    if legacy_path.exists():
        bundle = joblib.load(legacy_path)
        artifacts["rated"] = {
            "model": bundle["model"],
            "threshold": float(bundle.get("threshold", 0.5)),
        }
        if not artifacts["feature_list"]:
            artifacts["feature_list"] = bundle.get("features", [])
        logger.warning("[scorer] rated_model.pkl absent; using legacy walkaway_model.pkl")
        return artifacts

    raise FileNotFoundError(
        f"No model artifacts found in {d}. "
        "Run trainer.py first or verify rated_model.pkl / walkaway_model.pkl exists."
    )


# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_recent_data(
    start: datetime,
    end: datetime,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch bets (FINAL) and sessions (FND-01 CTE dedup + H2 avail gate).

    Bets:
    - FINAL, payout_complete_dtm IS NOT NULL, wager > 0
    - player_id IS NOT NULL and player_id != PLACEHOLDER_PLAYER_ID
    - payout_complete_dtm <= end - BET_AVAIL_DELAY_MIN

    Sessions:
    - NO FINAL; FND-01 ROW_NUMBER CTE dedup on session_id
    - is_deleted=0, is_canceled=0, is_manual=0
    - FND-04: COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0
    - H2 gate: COALESCE(session_end_dtm, lud_dtm) <= end - SESSION_AVAIL_DELAY_MIN
    - Fetches casino_player_id for H3 rated routing
    """
    if get_clickhouse_client is None:
        raise RuntimeError("clickhouse_connect not available; cannot fetch live data")

    client = get_clickhouse_client()
    bet_avail = end - timedelta(minutes=BET_AVAIL_DELAY_MIN)
    sess_avail = end - timedelta(minutes=SESSION_AVAIL_DELAY_MIN)
    params: dict = {"start": start, "end": end, "bet_avail": bet_avail, "sess_avail": sess_avail}
    placeholder = getattr(config, "PLACEHOLDER_PLAYER_ID", -1)
    cid_sql = getattr(
        config,
        "CASINO_PLAYER_ID_CLEAN_SQL",
        "casino_player_id",
    )

    bets_query = f"""
        SELECT
            bet_id,
            is_back_bet,
            base_ha,
            bet_type,
            payout_complete_dtm,
            session_id,
            player_id,
            table_id,
            position_idx,
            wager,
            payout_odds,
            status
        FROM {config.SOURCE_DB}.{config.TBET} FINAL
        WHERE payout_complete_dtm >= %(start)s
          AND payout_complete_dtm <= %(bet_avail)s
          AND payout_complete_dtm IS NOT NULL
          AND wager > 0
          AND player_id IS NOT NULL
          AND player_id != {placeholder}
    """

    session_query = f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY session_id
                       ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC
                   ) AS rn
            FROM {config.SOURCE_DB}.{config.TSESSION}
            WHERE session_start_dtm >= %(start)s - INTERVAL 2 DAY
              AND session_start_dtm <= %(end)s + INTERVAL 1 DAY
              AND is_deleted = 0
              AND is_canceled = 0
              AND is_manual = 0
        )
        SELECT
            session_id,
            table_id,
            player_id,
            {cid_sql} AS casino_player_id,
            session_start_dtm,
            session_end_dtm,
            lud_dtm,
            COALESCE(session_end_dtm, lud_dtm) AS session_avail_dtm,
            is_manual,
            is_deleted,
            is_canceled,
            COALESCE(turnover, 0) AS turnover,
            COALESCE(num_games_with_wager, 0) AS num_games_with_wager
        FROM deduped
        WHERE rn = 1
          AND COALESCE(session_end_dtm, lud_dtm) <= %(sess_avail)s
          AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
    """

    bets = client.query_df(bets_query, parameters=params)
    before = len(bets)
    bets = bets[bets["wager"].fillna(0) > 0].copy()
    if len(bets) != before:
        logger.debug("[scorer] filtered zero-wager bets: %d->%d", before, len(bets))

    # Normalize payout_complete_dtm to tz-aware HK time so Track LLM cutoff_time
    # (now_hk, tz-aware) stays consistent with scoring window timestamps.
    if not bets.empty and "payout_complete_dtm" in bets.columns:
        pcd = pd.to_datetime(bets["payout_complete_dtm"])
        if pcd.dt.tz is None:
            bets["payout_complete_dtm"] = pcd.dt.tz_localize(HK_TZ)
        else:
            bets["payout_complete_dtm"] = pcd.dt.tz_convert(HK_TZ)

    sessions = client.query_df(session_query, parameters=params)
    logger.info("[scorer] Fetched %d bets, %d sessions", len(bets), len(sessions))
    return bets, sessions


# ── SQLite state helpers ──────────────────────────────────────────────────────

def init_state_db() -> None:
    """Initialise SQLite state DB; migrates alerts table to add Phase-1 columns."""
    STATE_DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(STATE_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
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
        # Schema migration: add new Phase-1 columns to existing DB without data loss
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(alerts)").fetchall()
        }
        for col_name, col_type in _NEW_ALERT_COLS:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {col_name} {col_type}")
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
            "CREATE INDEX IF NOT EXISTS idx_validation_alert_ts "
            "ON validation_results(alert_ts)"
        )
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


def _get_last_processed_end(conn: sqlite3.Connection) -> Optional[pd.Timestamp]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_processed_end'"
    ).fetchone()
    return pd.to_datetime(row[0]) if row else None


def _set_last_processed_end(conn: sqlite3.Connection, dt: datetime) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('last_processed_end', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (dt.isoformat(),),
    )


def prune_old_state(
    conn: sqlite3.Connection,
    now_hk: datetime,
    retention_hours: int = RETENTION_HOURS,
) -> None:
    cutoff = now_hk - timedelta(hours=retention_hours)
    conn.execute("DELETE FROM session_stats WHERE last_ts < ?", (cutoff.isoformat(),))
    conn.commit()


def _upsert_session(
    conn: sqlite3.Connection,
    sid: object,
    bet_count: int,
    sum_wager: float,
    first_ts: Optional[datetime],
    last_ts: Optional[datetime],
    player_id: object,
    table_id: object,
) -> None:
    conn.execute(
        """
        INSERT INTO session_stats(
            session_id, bet_count, sum_wager, first_ts, last_ts,
            player_id, table_id, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            bet_count  = session_stats.bet_count + excluded.bet_count,
            sum_wager  = session_stats.sum_wager + excluded.sum_wager,
            first_ts   = COALESCE(session_stats.first_ts, excluded.first_ts),
            last_ts    = MAX(session_stats.last_ts, excluded.last_ts),
            player_id  = COALESCE(session_stats.player_id, excluded.player_id),
            table_id   = COALESCE(session_stats.table_id, excluded.table_id),
            updated_at = excluded.updated_at
        """,
        (
            str(sid),
            int(bet_count),
            float(sum_wager),
            first_ts.isoformat() if first_ts is not None else None,
            last_ts.isoformat() if last_ts is not None else None,
            None if pd.isna(player_id) else str(player_id),
            None if pd.isna(table_id) else str(table_id),
            datetime.now(HK_TZ).isoformat(),
        ),
    )


def update_state_with_new_bets(
    conn: sqlite3.Connection,
    bets: pd.DataFrame,
    window_end: datetime,
) -> pd.DataFrame:
    last_processed = _get_last_processed_end(conn)
    new_bets = (
        bets[bets["payout_complete_dtm"] > last_processed]
        if last_processed is not None
        else bets
    )
    for sid, group in new_bets.groupby("session_id"):
        if pd.isna(sid):
            continue
        _upsert_session(
            conn,
            sid,
            len(group),
            group["wager"].sum(),
            group["payout_complete_dtm"].min(),
            group["payout_complete_dtm"].max(),
            group.get("player_id", pd.Series([None])).iloc[0],
            group.get("table_id", pd.Series([None])).iloc[0],
        )
    _set_last_processed_end(conn, window_end)
    conn.commit()
    return new_bets


def get_session_totals(
    conn: sqlite3.Connection,
    session_id: object,
) -> Tuple[int, float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    if pd.isna(session_id):
        return 0, 0.0, None, None
    row = conn.execute(
        "SELECT bet_count, sum_wager, first_ts, last_ts "
        "FROM session_stats WHERE session_id = ?",
        (str(session_id),),
    ).fetchone()
    if not row:
        return 0, 0.0, None, None
    return (
        row[0],
        row[1],
        pd.to_datetime(row[2]) if row[2] else None,
        pd.to_datetime(row[3]) if row[3] else None,
    )


def get_historical_avg(conn: sqlite3.Connection, player_id: object) -> float:
    if pd.isna(player_id):
        return 0.0
    row = conn.execute(
        "SELECT SUM(sum_wager), SUM(bet_count) FROM session_stats WHERE player_id = ?",
        (str(player_id),),
    ).fetchone()
    if not row or row[1] is None or row[1] == 0:
        return 0.0
    return float(row[0]) / float(row[1])


def get_session_count(conn: sqlite3.Connection, player_id: object) -> int:
    if pd.isna(player_id):
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM session_stats WHERE player_id = ?",
        (str(player_id),),
    ).fetchone()
    return int(row[0]) if row else 0


# ── Feature engineering ───────────────────────────────────────────────────────

def build_features_for_scoring(
    bets: pd.DataFrame,
    sessions: pd.DataFrame,
    canonical_map: pd.DataFrame,
    cutoff_time: datetime,
) -> pd.DataFrame:
    """Compute scoring features with train-serve parity.

    Steps
    -----
    1. D2 identity: attach canonical_id from canonical_map.
    2. Track B (features.py): loss_streak, run_id, minutes_since_run_start
       — same functions as trainer.py. (table_hc deferred to Phase 2.)
    3. Session rolling stats: bets_last_5/15/30m, wager_last_10/30m,
       cum_bets, cum_wager, avg_wager_sofar, session_duration_min,
       bets_per_minute (legacy parity with trainer.add_legacy_features).
    4. Time-of-day cyclic encoding.
    """
    if bets.empty:
        return bets.copy()

    bets_df = bets.copy()

    # ── Normalise types (ClickHouse may return object/string; LightGBM needs int/float/bool) ──
    for col in ["position_idx", "payout_odds", "base_ha", "is_back_bet", "wager"]:
        if col not in bets_df.columns:
            bets_df[col] = 0.0
    for col in ["position_idx", "payout_odds", "base_ha", "wager"]:
        bets_df[col] = pd.to_numeric(bets_df[col], errors="coerce").fillna(0)
    bets_df["is_back_bet"] = pd.to_numeric(bets_df["is_back_bet"], errors="coerce").fillna(0)
    bets_df["status"] = bets_df.get("status", pd.Series("", index=bets_df.index)).astype(str).str.upper()

    # Normalise payout_complete_dtm to tz-naive HK local time (R23-style fix)
    pcd = pd.to_datetime(bets_df["payout_complete_dtm"])
    if pcd.dt.tz is not None:
        pcd = pcd.dt.tz_convert(HK_TZ).dt.tz_localize(None)
    bets_df["payout_complete_dtm"] = pcd

    # Normalise cutoff_time to tz-naive
    cutoff_naive = cutoff_time.replace(tzinfo=None) if cutoff_time.tzinfo is not None else cutoff_time

    # ── D2 identity ───────────────────────────────────────────────────────
    if not canonical_map.empty and "player_id" in canonical_map.columns:
        bets_df = bets_df.merge(
            canonical_map[["player_id", "canonical_id"]].drop_duplicates("player_id"),
            on="player_id",
            how="left",
        )
    else:
        bets_df["canonical_id"] = bets_df["player_id"].astype(str)
    bets_df["canonical_id"] = bets_df["canonical_id"].fillna(
        bets_df["player_id"].astype(str)
    )

    # Stable sort (required by Track B functions and labels.py)
    bets_df = bets_df.sort_values(
        ["canonical_id", "payout_complete_dtm", "bet_id"], kind="stable"
    ).reset_index(drop=True)

    # ── Track B ───────────────────────────────────────────────────────────
    bets_df["loss_streak"] = compute_loss_streak(
        bets_df, cutoff_time=cutoff_naive
    ).fillna(0)

    rb = compute_run_boundary(bets_df, cutoff_time=cutoff_naive)
    bets_df["run_id"] = rb["run_id"] if "run_id" in rb.columns else 0
    bets_df["minutes_since_run_start"] = (
        rb["minutes_since_run_start"] if "minutes_since_run_start" in rb.columns else 0.0
    )

    # ── Session rolling stats (legacy parity) ─────────────────────────────
    sess_df = sessions.copy() if not sessions.empty else pd.DataFrame()
    if not sess_df.empty and "session_id" in sess_df.columns:
        sess_df["session_start_dtm"] = pd.to_datetime(
            sess_df.get("session_start_dtm")
        )
        sess_df["session_end_dtm"] = pd.to_datetime(
            sess_df.get("session_end_dtm")
        )
        if "session_end_dtm" in sess_df.columns:
            sess_df = sess_df.sort_values(["session_id", "session_end_dtm"])
        else:
            sess_df = sess_df.sort_values("session_id")
        sess_df = sess_df.drop_duplicates(subset=["session_id"], keep="last")

        bets_df = bets_df.merge(
            sess_df[["session_id", "session_start_dtm", "session_end_dtm"]],
            on="session_id",
            how="left",
        )

    for col in ["session_start_dtm", "session_end_dtm"]:
        if col not in bets_df.columns:
            bets_df[col] = bets_df["payout_complete_dtm"]
        else:
            bets_df[col] = pd.to_datetime(bets_df[col], errors="coerce").fillna(
                bets_df["payout_complete_dtm"]
            )
        # Ensure datetime64 before .dt access (ClickHouse can return object/string)
        bets_df[col] = pd.to_datetime(bets_df[col], errors="coerce")
        # R33: convert to HK local time then strip tz to avoid wall-clock skew
        if pd.api.types.is_datetime64_any_dtype(bets_df[col]) and bets_df[col].dt.tz is not None:
            bets_df[col] = bets_df[col].dt.tz_convert(HK_TZ).dt.tz_localize(None)

    bets_df["cum_bets"] = bets_df.groupby("session_id").cumcount() + 1
    bets_df["cum_wager"] = bets_df.groupby("session_id")["wager"].cumsum()
    bets_df["avg_wager_sofar"] = bets_df["cum_wager"] / bets_df["cum_bets"]

    # R2300: session_duration_min and bets_per_minute — train-serve parity with
    # trainer.add_legacy_features (R2300 scorer parity gap).
    bets_df["session_duration_min"] = (
        (bets_df["session_end_dtm"] - bets_df["session_start_dtm"])
        .dt.total_seconds()
        .clip(lower=0)
        / 60
    )
    bets_df["bets_per_minute"] = (
        bets_df["cum_bets"] / bets_df["session_duration_min"].replace(0, np.nan)
    ).fillna(0.0)

    # Vectorised rolling window counts / sums per session
    _pcd = pd.to_datetime(bets_df["payout_complete_dtm"], errors="coerce")
    if pd.api.types.is_datetime64_any_dtype(_pcd) and _pcd.dt.tz is not None:
        _pcd_utc = _pcd.dt.tz_convert("UTC").dt.tz_localize(None)
    else:
        _pcd_utc = _pcd
    bets_df["_ts_ns"] = _pcd_utc.astype("datetime64[ns]").astype("int64")

    def _session_windows(group: pd.DataFrame) -> pd.DataFrame:
        ts = group["_ts_ns"].to_numpy()
        wager = group["wager"].to_numpy()
        n = len(group)
        cumsum = np.concatenate([[0], np.cumsum(wager)])
        out: Dict[str, np.ndarray] = {}
        for window in (5, 15, 30):
            win_ns = window * 60 * int(1e9)
            lo = 0
            counts = np.empty(n, dtype=np.float64)
            for i, t in enumerate(ts):
                while t - ts[lo] > win_ns:
                    lo += 1
                counts[i] = i - lo + 1
            out[f"bets_last_{window}m"] = counts
        for window in (10, 30):
            win_ns = window * 60 * int(1e9)
            lo = 0
            sums = np.empty(n, dtype=np.float64)
            for i, t in enumerate(ts):
                while t - ts[lo] > win_ns:
                    lo += 1
                sums[i] = cumsum[i + 1] - cumsum[lo]
            out[f"wager_last_{window}m"] = sums
        return pd.DataFrame(out, index=group.index)

    windows_df = bets_df.groupby("session_id", group_keys=False).apply(
        _session_windows, include_groups=False
    )
    bets_df.update(windows_df)
    for col in [
        "bets_last_5m", "bets_last_15m", "bets_last_30m",
        "wager_last_10m", "wager_last_30m",
    ]:
        if col not in bets_df.columns:
            bets_df[col] = 0.0
        else:
            bets_df[col] = bets_df[col].fillna(0.0)

    # Time-of-day cyclic encoding
    bets_df["minutes_into_day"] = (
        bets_df["payout_complete_dtm"].dt.hour * 60
        + bets_df["payout_complete_dtm"].dt.minute
    )
    bets_df["time_of_day_sin"] = np.sin(
        2 * np.pi * bets_df["minutes_into_day"] / 1440
    )
    bets_df["time_of_day_cos"] = np.cos(
        2 * np.pi * bets_df["minutes_into_day"] / 1440
    )

    bets_df.drop(columns=["_ts_ns", "minutes_into_day"], errors="ignore", inplace=True)
    return bets_df


# ── Reason codes ──────────────────────────────────────────────────────────────

def _compute_reason_codes(
    model: object,
    X: pd.DataFrame,
    reason_code_map: dict,
    top_k: int = SHAP_TOP_K,
) -> List[str]:
    """Return JSON-encoded reason-code lists (one per row in X).

    Uses SHAP TreeExplainer; falls back to empty lists on any error
    (e.g., shap not installed, model type unsupported).
    """
    try:
        import shap  # type: ignore[import]

        explainer = shap.TreeExplainer(model)
        shap_vals = explainer.shap_values(X)
        if isinstance(shap_vals, list):
            shap_vals = shap_vals[1]  # binary: class-1 SHAP values
        result: List[str] = []
        for i in range(len(X)):
            top_idx = np.argsort(np.abs(shap_vals[i]))[::-1][:top_k]
            codes = [
                reason_code_map.get(str(X.columns[j]), str(X.columns[j]))
                for j in top_idx
            ]
            result.append(json.dumps(codes))
        return result
    except Exception as exc:
        logger.debug("[scorer] SHAP reason codes skipped: %s", exc)
        return ["[]" for _ in range(len(X))]


# ── player_profile loading for real-time scoring (R79) ─────────────────

PROJECT_ROOT = BASE_DIR.parent
_LOCAL_PARQUET_PROFILE = PROJECT_ROOT / "data" / "player_profile.parquet"

# R85: module-level TTL cache — avoids re-querying the profile table on every
# scoring call within the same process (e.g. repeated score_once calls in a loop).
_profile_cache: dict = {"df": None, "loaded_at": None, "loaded_for": None}
_PROFILE_CACHE_TTL_HOURS: float = 1.0


def _load_profile_for_scoring(
    rated_canonical_ids: set,
    as_of_dtm: datetime,
    lookback_days: int = 365,
) -> "Optional[pd.DataFrame]":
    """Load the *latest* player_profile snapshot per rated player for PIT join.

    Only the most-recent snapshot at or before ``as_of_dtm`` is returned for each
    canonical_id (R84 — avoids sending stale/redundant rows to merge_asof).

    Returns None when the table is unavailable (graceful degradation — profile
    features stay NaN and LightGBM handles them via its trained default-child).
    """
    if not rated_canonical_ids:
        return None

    # R85: check TTL cache first
    global _profile_cache
    if (
        _profile_cache["df"] is not None
        and _profile_cache["loaded_at"] is not None
    ):
        age_hours = (datetime.now() - _profile_cache["loaded_at"]).total_seconds() / 3600
        if age_hours < _PROFILE_CACHE_TTL_HOURS:
            cached_df: "pd.DataFrame" = _profile_cache["df"]
            cids_str_cache = {str(c) for c in rated_canonical_ids}
            result = cached_df[cached_df["canonical_id"].astype(str).isin(cids_str_cache)]
            logger.debug("[scorer] profile: served from cache (age=%.1fh)", age_hours)
            return result if not result.empty else None

    def _naive(dt: datetime) -> "pd.Timestamp":
        ts = pd.Timestamp(dt)
        return ts.tz_localize(None) if ts.tzinfo is None else ts.replace(tzinfo=None)

    df_loaded: "Optional[pd.DataFrame]" = None

    # Local Parquet dev path (mirrors trainer's LOCAL_PARQUET_DIR fallback)
    if _LOCAL_PARQUET_PROFILE.exists():
        try:
            df = pd.read_parquet(
                _LOCAL_PARQUET_PROFILE,
                filters=[("snapshot_dtm", "<=", _naive(as_of_dtm))],
            )
            cids_str = {str(c) for c in rated_canonical_ids}
            df = df[df["canonical_id"].astype(str).isin(cids_str)]
            # R84: keep only the latest snapshot per player (groupby last preserves
            # the most-recent row per canonical_id after sort by snapshot_dtm)
            df = df.sort_values("snapshot_dtm")
            df = df.groupby("canonical_id").last().reset_index()
            logger.info("[scorer] player_profile: %d rows from local Parquet", len(df))
            df_loaded = df if not df.empty else None
        except Exception as exc:
            logger.debug("[scorer] profile local Parquet skipped: %s", exc)

    # player_profile is a locally-derived table (built by etl_player_profile.py from
    # t_session); it is never stored in ClickHouse.  The ClickHouse path is removed
    # to prevent spurious Code-60 errors on every scoring call.
    if df_loaded is None:
        logger.info(
            "[scorer] player_profile not found at %s — "
            "run etl_player_profile.py first; profile features will be NaN",
            _LOCAL_PARQUET_PROFILE,
        )

    # R85: populate cache on successful load
    if df_loaded is not None:
        _profile_cache = {"df": df_loaded, "loaded_at": datetime.now(), "loaded_for": as_of_dtm}

    return df_loaded


# ── Scoring / H3 routing ──────────────────────────────────────────────────────

def _score_df(
    df: pd.DataFrame,
    artifacts: dict,
    feature_list: List[str],
) -> pd.DataFrame:
    """Score all observations with the rated model and compute margin.

    Sets columns: score, is_rated_obs (int 0/1), margin.
    All observations (rated and unrated) are scored with the single rated model
    (v10 DEC-021).  Unrated observations are scored for volume telemetry only;
    alerts are only generated for rated observations (is_rated_obs == 1).
    """
    rated_art = artifacts.get("rated")

    df = df.copy()
    # R74/R79: profile features stay NaN when no prior snapshot exists —
    # LightGBM routes them via its trained default-child (NaN-aware split).
    # Step 6: profile vs non-profile from artifact track (YAML-driven); fallback to PROFILE_FEATURE_COLS.
    # Step 8 backward compat: accept legacy "profile" as track_profile so old feature_list.json works.
    _meta = artifacts.get("feature_list_meta")
    if _meta and isinstance(_meta, list):
        _profile_in_list = {
            e["name"] for e in _meta
            if isinstance(e, dict) and e.get("track") in ("track_profile", "profile")
        }
    else:
        _profile_in_list = set(PROFILE_FEATURE_COLS) & set(feature_list)
    _non_profile_in_list = [c for c in feature_list if c not in _profile_in_list]
    for col in _non_profile_in_list:
        if col not in df.columns:
            df[col] = 0.0
    for col in _profile_in_list:
        if col not in df.columns:
            df[col] = np.nan
    df[_non_profile_in_list] = df[_non_profile_in_list].fillna(0.0)
    # Step 6: use shared coerce_feature_dtypes (train-serve parity); fallback to inline coerce if import failed.
    if coerce_feature_dtypes is not None:
        coerce_feature_dtypes(df, feature_list)
    else:
        for col in feature_list:
            if col in df.columns and not pd.api.types.is_numeric_dtype(df[col]):
                df[col] = pd.to_numeric(df[col], errors="coerce")
    df[_non_profile_in_list] = df[_non_profile_in_list].fillna(0.0)

    is_rated = (
        df["is_rated"].to_numpy(dtype=bool)
        if "is_rated" in df.columns
        else np.zeros(len(df), dtype=bool)
    )
    scores = np.zeros(len(df), dtype=float)

    if rated_art is not None and len(df) > 0:
        model_features = rated_art.get("features") or feature_list
        scores = rated_art["model"].predict_proba(df[model_features])[:, 1]

    df["score"] = scores
    df["is_rated_obs"] = is_rated.astype(int)

    rated_t = float((rated_art or {}).get("threshold", 0.5))
    df["margin"] = df["score"] - rated_t

    return df


# ── Alert persistence ─────────────────────────────────────────────────────────

def load_alert_history(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute("SELECT bet_id FROM alerts").fetchall()
        return {str(r[0]) for r in rows if r[0] is not None}
    except Exception:
        return set()


def refresh_alert_history(
    alert_history: set, now_hk: datetime, conn: sqlite3.Connection
) -> set:
    retention_days = getattr(config, "SCORER_ALERT_RETENTION_DAYS", None)
    if retention_days is not None and retention_days > 0:
        cutoff = now_hk - timedelta(days=retention_days)
        conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff.isoformat(),))
        conn.commit()
    try:
        rows = conn.execute("SELECT bet_id FROM alerts").fetchall()
        alert_history.clear()
        alert_history.update({str(r[0]) for r in rows if r[0] is not None})
    except Exception:
        alert_history.clear()
    return alert_history


def append_alerts(conn: sqlite3.Connection, alerts_df: pd.DataFrame) -> None:
    """Upsert alert rows; handles both legacy and new Phase-1 columns."""

    def _s(v: object) -> Optional[str]:
        try:
            return None if pd.isna(v) else str(v)  # handles pd.NA, pd.NaT, float nan (R34)
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

    rows = [
        (
            _s(r.bet_id),
            _ts(r.ts),
            _ts(r.bet_ts),
            _s(r.player_id),
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
        )
        for r in alerts_df.itertuples(index=False)
    ]

    conn.executemany(
        """
        INSERT INTO alerts(
            bet_id, ts, bet_ts, player_id, table_id, position_idx,
            visit_start_ts, visit_end_ts, session_count, bet_count,
            visit_avg_bet, historical_avg_bet, score, session_id,
            loss_streak, bets_last_5m, bets_last_15m, bets_last_30m,
            wager_last_10m, wager_last_30m, cum_bets, cum_wager,
            avg_wager_sofar, session_duration_min, bets_per_minute,
            canonical_id, is_rated_obs, reason_codes, model_version,
            margin, scored_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
            scored_at=excluded.scored_at
        """,
        rows,
    )


# ── Main scoring loop ─────────────────────────────────────────────────────────

def score_once(
    artifacts: dict,
    lookback_hours: int,
    alert_history: set,
    conn: sqlite3.Connection,
    retention_hours: int = RETENTION_HOURS,
) -> None:
    """Run one scoring cycle.

    Steps
    -----
    1. Fetch bets + sessions (FND-01 CTE, H2 session_avail_dtm gate).
    2. D2 identity via build_canonical_mapping_from_df.
    3. Build Track B + session rolling features (train-serve parity).
    4. Set is_rated flag: canonical_id in rated mapping.
    5. Single rated model scoring via _score_df (v10 DEC-021).
    6. SHAP reason codes for rated alert candidates.
    7. Session stats, duplicate suppression, DB write.
    """
    now_hk = datetime.now(HK_TZ)
    scored_at = now_hk.isoformat()
    feature_list: List[str] = artifacts["feature_list"]
    model_version: str = artifacts["model_version"]

    refresh_alert_history(alert_history, now_hk, conn)
    start = now_hk - timedelta(hours=lookback_hours)
    logger.info("[scorer] Window: %s -> %s", start.isoformat(), now_hk.isoformat())

    bets, sessions = fetch_recent_data(start, now_hk)
    if bets.empty:
        logger.info("[scorer] No bets in window; sleeping")
        return

    prune_old_state(conn, now_hk, retention_hours)
    new_bets = update_state_with_new_bets(conn, bets, now_hk)
    logger.info("[scorer] New bets since last tick: %d", len(new_bets))
    if new_bets.empty:
        logger.info("[scorer] No new bets to score; sleeping")
        return

    # ── D2 identity ───────────────────────────────────────────────────────
    canonical_map = build_canonical_mapping_from_df(sessions, cutoff_dtm=now_hk)

    # H3: every canonical_id in the mapping is a rated player — identity.py only
    # builds entries for players who have a valid casino_player_id (R36 fix).
    rated_canonical_ids: set = (
        set(canonical_map["canonical_id"].unique()) if not canonical_map.empty else set()
    )

    # ── Features on full window (for rolling context) ─────────────────────
    features_all = build_features_for_scoring(bets, sessions, canonical_map, now_hk)

    # Track LLM: compute DuckDB features from feature spec when available.
    _feature_spec = artifacts.get("feature_spec")
    if _feature_spec is not None:
        _n_before_llm = len(features_all)
        try:
            features_all = compute_track_llm_features(
                features_all,
                feature_spec=_feature_spec,
                cutoff_time=now_hk,
            )
            # R3503: log if cutoff filtering silently reduced the scoring window.
            if len(features_all) < _n_before_llm:
                logger.warning(
                    "[scorer] Track LLM dropped %d rows (cutoff filter)",
                    _n_before_llm - len(features_all),
                )
            logger.info("[scorer] Track LLM computed for scoring window")
        except Exception as exc:
            logger.error("[scorer] Track LLM failed: %s", exc)

    # ── player_profile PIT join (R79) ─────────────────────────────────
    # Attach rated-player profile features via as-of merge (snapshot_dtm <= bet_time).
    # Non-rated bets and bets without a prior snapshot keep NaN — LightGBM handles
    # this natively via the default-child path trained in trainer.py (R74/R79).
    if _join_profile is not None and PROFILE_FEATURE_COLS and rated_canonical_ids:
        _profile_df = _load_profile_for_scoring(rated_canonical_ids, now_hk)
        if _profile_df is not None:
            features_all = _join_profile(features_all, _profile_df)
            logger.info("[scorer] player_profile PIT join applied")
        else:
            logger.info(
                "[scorer] player_profile unavailable — profile features will be NaN"
            )

    features_all["is_rated"] = features_all["canonical_id"].isin(rated_canonical_ids)
    logger.info("[scorer] Feature rows (full window): %d", len(features_all))

    new_ids = set(new_bets["bet_id"].astype(str))
    features_df = features_all[
        features_all["bet_id"].astype(str).isin(new_ids)
    ].copy()
    logger.info("[scorer] Rows to score (new bets): %d", len(features_df))
    if features_df.empty:
        logger.info("[scorer] No usable rows after feature engineering; sleeping")
        return
    if UNRATED_VOLUME_LOG:
        rated_bets = int(features_df["is_rated"].fillna(False).astype(bool).sum())
        unrated_bets = int(len(features_df) - rated_bets)
        rated_players = int(
            features_df.loc[features_df["is_rated"].astype(bool), "player_id"]
            .dropna()
            .astype(str)
            .nunique()
        )
        unrated_players = int(
            features_df.loc[~features_df["is_rated"].astype(bool), "player_id"]
            .dropna()
            .astype(str)
            .nunique()
        )
        logger.info(
            "[scorer][volume] poll_cycle_ts=%s rated_player_count=%d rated_bet_count=%d "
            "unrated_player_count=%d unrated_bet_count=%d",
            scored_at,
            rated_players,
            rated_bets,
            unrated_players,
            unrated_bets,
        )

    # ── Score with H3 routing ─────────────────────────────────────────────
    features_df = _score_df(features_df, artifacts, feature_list)

    # ── Alert candidates: score >= threshold AND rated observations only ──
    # Unrated observations are scored for volume telemetry (UNRATED_VOLUME_LOG)
    # but must not be emitted as alerts (v10 DEC-021).
    alert_candidates = features_df[
        (features_df["margin"] >= 0) & (features_df["is_rated_obs"] == 1)
    ].copy()
    if alert_candidates.empty:
        logger.info("[scorer] No above-threshold alerts this cycle")
        return
    logger.info("[scorer] Above-threshold rows: %d", len(alert_candidates))

    # ── SHAP reason codes for rated alert candidates ──────────────────────
    alert_candidates["reason_codes"] = "[]"
    rated_art = artifacts.get("rated")
    rated_mask_ac = alert_candidates["is_rated_obs"].astype(bool)

    rated_idx = alert_candidates.index[rated_mask_ac].tolist()
    if rated_idx and rated_art is not None and feature_list:
        _rated_feats = rated_art.get("features") or feature_list
        X_r = alert_candidates.loc[rated_idx, _rated_feats]
        rc_r = _compute_reason_codes(rated_art["model"], X_r, artifacts["reason_code_map"])
        alert_candidates.loc[rated_idx, "reason_codes"] = rc_r

    # ── Session stats ─────────────────────────────────────────────────────
    session_agg = (
        features_df.groupby("session_id")["wager"]
        .agg(bet_count="count", sum_wager="sum")
        .to_dict("index")
    )
    unique_sids = alert_candidates["session_id"].dropna().unique().tolist()
    session_totals_cache = {sid: get_session_totals(conn, sid) for sid in unique_sids}

    def _st(sid: object) -> Tuple[int, float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
        return session_totals_cache.get(sid, (0, 0.0, None, None))

    def _fallback_avg(sid: object) -> float:
        agg = session_agg.get(sid)
        if not agg:
            return 0.0
        bc = int(agg.get("bet_count", 0) or 0)
        sw = float(agg.get("sum_wager", 0.0) or 0.0)
        return (sw / bc) if bc > 0 else 0.0

    alert_candidates["ts"] = now_hk
    alert_candidates["scored_at"] = scored_at
    alert_candidates["model_version"] = model_version
    alert_candidates["bet_ts"] = alert_candidates["payout_complete_dtm"]
    alert_candidates["bet_count"] = alert_candidates["session_id"].apply(
        lambda sid: max(
            _st(sid)[0],
            int((session_agg.get(sid) or {}).get("bet_count", 0) or 0),
        )
    )
    def _visit_avg(sid: object) -> float:
        bc, sw, _, _ = _st(sid)
        return (sw / bc) if bc > 0 else _fallback_avg(sid)

    alert_candidates["visit_avg_bet"] = alert_candidates["session_id"].apply(_visit_avg)
    alert_candidates["visit_start_ts"] = alert_candidates["session_id"].apply(
        lambda sid: _st(sid)[2]
    )
    alert_candidates["visit_end_ts"] = alert_candidates["session_id"].apply(
        lambda sid: _st(sid)[3]
    )
    alert_candidates["session_count"] = alert_candidates["player_id"].apply(
        lambda pid: get_session_count(conn, pid)
    )
    alert_candidates["historical_avg_bet"] = alert_candidates["player_id"].apply(
        lambda pid: get_historical_avg(conn, pid)
    )

    # ── Duplicate suppression ─────────────────────────────────────────────
    if alert_history:
        alert_candidates["_bid_str"] = alert_candidates["bet_id"].astype(str)
        before = len(alert_candidates)
        alert_candidates = alert_candidates[
            ~alert_candidates["_bid_str"].isin(alert_history)
        ]
        suppressed = before - len(alert_candidates)
        if suppressed:
            logger.info("[scorer] Suppressed %d duplicate alerts", suppressed)
        alert_candidates.drop(columns=["_bid_str"], inplace=True)

    if alert_candidates.empty:
        logger.info("[scorer] Alerts suppressed (already sent)")
        return

    append_alerts(conn, alert_candidates)
    alert_history.update(alert_candidates["bet_id"].astype(str).tolist())
    logger.info("[scorer] Emitted %d alerts", len(alert_candidates))
    conn.commit()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Near real-time scorer for walkaway alerts"
    )
    parser.add_argument(
        "--interval", type=int, default=45,
        help="Polling interval in seconds (includes run time)",
    )
    parser.add_argument(
        "--lookback-hours", type=int, default=8,
        help="Hours of history to pull each cycle",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single scoring cycle and exit",
    )
    parser.add_argument(
        "--model-dir", type=Path, default=None,
        help="Override model artifact directory",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    artifacts = load_dual_artifacts(args.model_dir)
    logger.info(
        "[scorer] Loaded model v=%s, rated=%s, %d features",
        artifacts["model_version"],
        "yes" if artifacts["rated"] else "no",
        len(artifacts["feature_list"]),
    )

    conn = sqlite3.connect(STATE_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    init_state_db()
    alert_history = load_alert_history(conn)

    while True:
        t_start = time.time()
        try:
            score_once(artifacts, args.lookback_hours, alert_history, conn, RETENTION_HOURS)
        except Exception as exc:
            logger.error("[scorer] ERROR: %s", exc, exc_info=True)
        elapsed = time.time() - t_start
        sleep_for = max(0, args.interval - elapsed)
        if args.once:
            break
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
