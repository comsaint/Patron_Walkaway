"""trainer/scorer.py — Phase 1 Refactor
==========================================
Near real-time scoring daemon.

Key changes from pre-Phase-1 version
--------------------------------------
* Single rated-model artifact: model.pkl (v10 DEC-021; falls back to
  rated_model.pkl or legacy walkaway_model.pkl when model.pkl is absent).
* D2 identity resolution via identity.py build_canonical_mapping_from_df.
* Track Human features via features.py (compute_loss_streak / compute_run_boundary)
  — guarantees train-serve parity with trainer.py. (table_hc deferred to Phase 2.)
* player_profile PIT join (R79): rated bets enriched with player profile
  features via as-of merge (snapshot_dtm <= bet_time); profile features stay NaN
  for non-rated bets and bets with no prior snapshot.
* H3 model routing: is_rated_obs ← casino_player_id IS NOT NULL.
* FND-01 CTE dedup + session_avail_dtm gate on session query (H2).
* SHAP reason codes -> reason_code_map.json lookup, emitted with every alert.
* New alert DB columns: canonical_id, is_rated_obs, reason_codes,
  model_version, margin, scored_at.

Architecture: api_server reads only from shared SQLite (state.db) and does not
expose a model API; this scorer is the only component that loads the model
and writes alerts.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
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
        from .features import (  # type: ignore[import, attr-defined]
            PROFILE_FEATURE_COLS,
            join_player_profile as _join_profile,
            coerce_feature_dtypes,
        )
    except ImportError:
        from trainer.features import (  # type: ignore[import, attr-defined]
            PROFILE_FEATURE_COLS,
            join_player_profile as _join_profile,
            coerce_feature_dtypes,
        )
    except ImportError:
        PROFILE_FEATURE_COLS = []  # type: ignore[assignment]
        _join_profile = None  # type: ignore[assignment]
        coerce_feature_dtypes = None  # type: ignore[assignment]

from trainer.db_conn import get_clickhouse_client  # serving lives under trainer; db_conn at package root

try:
    import config  # type: ignore[import]
except ModuleNotFoundError:
    try:
        import trainer.config as config  # type: ignore[import, no-redef]
    except ModuleNotFoundError:
        from trainer.core import config  # type: ignore[no-redef]

try:
    from features import (  # type: ignore[import]
        compute_loss_streak,
        compute_run_boundary,
        compute_track_llm_features,
        load_feature_spec,
    )
except ImportError:
    try:
        from .features import (  # type: ignore[import, attr-defined]
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
    try:
        from .identity import build_canonical_mapping_from_df  # type: ignore[import, attr-defined]
    except ImportError:
        from trainer.identity import build_canonical_mapping_from_df  # type: ignore[import, attr-defined]

try:
    from schema_io import normalize_bets_sessions  # type: ignore[import]
except ImportError:
    try:
        from .schema_io import normalize_bets_sessions  # type: ignore[import]
    except ImportError:
        from trainer.schema_io import normalize_bets_sessions  # type: ignore[import]

logger = logging.getLogger(__name__)

_PERF_WINDOW_SIZE = 200
_SCORER_STAGE_TIMINGS: Dict[str, deque[float]] = {}
_NUMBA_CHECK_DONE = False
_SQLITE_IN_CHUNK_SIZE = 500


def _record_scorer_stage_timing(stage: str, seconds: float) -> None:
    bucket = _SCORER_STAGE_TIMINGS.setdefault(stage, deque(maxlen=_PERF_WINDOW_SIZE))
    bucket.append(max(0.0, float(seconds)))


def _emit_scorer_perf_summary(cycle_stage_seconds: Dict[str, float]) -> None:
    if not cycle_stage_seconds:
        return
    for stage, sec in cycle_stage_seconds.items():
        _record_scorer_stage_timing(stage, sec)
    top_stages = sorted(cycle_stage_seconds.items(), key=lambda x: x[1], reverse=True)[:2]
    parts: List[str] = []
    for stage, sec in top_stages:
        hist = _SCORER_STAGE_TIMINGS.get(stage)
        if not hist:
            continue
        arr = np.asarray(hist, dtype=float)
        p50 = float(np.percentile(arr, 50))
        p95 = float(np.percentile(arr, 95))
        parts.append(f"{stage}={sec:.3f}s (p50={p50:.3f}s, p95={p95:.3f}s, n={len(arr)})")
    if parts:
        logger.debug("[scorer][perf] top_hotspots: %s", "; ".join(parts))

# ── Constants ────────────────────────────────────────────────────────────────
HK_TZ = ZoneInfo(config.HK_TZ)
# ClickHouse / chunk Parquets use UTC-aware timestamps for payout + ETL insert
# (see `data/gmwds_t_bet.parquet`: timestamp[ms, tz=UTC]).
UTC_TZ = ZoneInfo("UTC")


def _meta_iso_to_hk(val: object) -> Optional[pd.Timestamp]:
    """Parse SQLite meta timestamps written by scorer (window_end, watermarks).

    ISO strings from ``datetime.now(HK_TZ).isoformat()`` include an offset.
    legacy naive strings are treated as **Hong Kong local** wall time.
    """
    if val is None:
        return None
    t = pd.Timestamp(val)
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        return t.tz_localize(HK_TZ, ambiguous="NaT", nonexistent="shift_forward")
    return t.tz_convert(HK_TZ)


def _warehouse_timestamp_series_to_hk(series: pd.Series) -> pd.Series:
    """Normalize warehouse bet timestamps to tz-aware HK (single instant semantics).

    Parquet samples store ``payout_complete_dtm`` / ``__etl_insert_Dtm`` as UTC.
    Some drivers may return naive datetimes; those are interpreted as UTC, not HK,
    before converting to HK for comparisons with ``now_hk`` and SQLite meta values.
    """
    ts = pd.to_datetime(series, errors="coerce")
    if ts.empty:
        return ts
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(UTC_TZ, ambiguous="NaT", nonexistent="shift_forward")
    return ts.dt.tz_convert(HK_TZ)
BASE_DIR = Path(__file__).resolve().parent.parent  # trainer/ (serving lives under trainer)
PROJECT_ROOT = BASE_DIR.parent
# Treat empty or whitespace-only env as unset (STATUS Code Review 項目 4 §1; align with DATA_DIR).
_state_db_env = os.environ.get("STATE_DB_PATH")
_state_db_effective = _state_db_env.strip() if (_state_db_env and _state_db_env.strip()) else None
_model_dir_env = os.environ.get("MODEL_DIR")
_model_dir_effective = _model_dir_env.strip() if (_model_dir_env and _model_dir_env.strip()) else None
STATE_DIR = Path(_state_db_effective).parent if _state_db_effective else (PROJECT_ROOT / "local_state")
STATE_DB_PATH = Path(_state_db_effective) if _state_db_effective else (PROJECT_ROOT / "local_state" / "state.db")
MODEL_DIR = (
    Path(_model_dir_effective)
    if _model_dir_effective
    else (getattr(config, "DEFAULT_MODEL_DIR", None) or (BASE_DIR / "models"))
)
STATE_DIR.mkdir(parents=True, exist_ok=True)
FEATURE_SPEC_PATH = BASE_DIR / "feature_spec" / "features_candidates.yaml"

RETENTION_HOURS: int = getattr(config, "SCORER_STATE_RETENTION_HOURS", 48)
SESSION_AVAIL_DELAY_MIN: int = getattr(config, "SESSION_AVAIL_DELAY_MIN", 15)
BET_AVAIL_DELAY_MIN: int = getattr(config, "BET_AVAIL_DELAY_MIN", 1)
UNRATED_VOLUME_LOG: bool = bool(getattr(config, "UNRATED_VOLUME_LOG", True))
SHAP_TOP_K = 3

# New alert columns added in Phase 1 (+ casino_player_id for ML API protocol)
_VALIDATION_RESULTS_MIGRATION_COLS: List[Tuple[str, str]] = [
    ("bet_ts", "TEXT"),
]

_NEW_ALERT_COLS: List[Tuple[str, str]] = [
    ("canonical_id", "TEXT"),
    ("is_rated_obs", "INTEGER"),
    ("reason_codes", "TEXT"),
    ("model_version", "TEXT"),
    ("margin", "REAL"),
    ("scored_at", "TEXT"),
    ("casino_player_id", "TEXT"),
]


# ── Artifact loading ──────────────────────────────────────────────────────────
def load_dual_artifacts(model_dir: Optional[Path] = None) -> dict:
    """Load model artifacts, feature list, reason code map, model version.

    Model is loaded locally only; api_server does not serve /score.

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
    # In deploy (d == MODEL_DIR from env), feature_spec.yaml is required; no fallback.
    # Fall back to the repo feature spec (features_candidates.yaml) when frozen load fails.
    _frozen_spec = d / "feature_spec.yaml"
    if _frozen_spec.exists():
        try:
            artifacts["feature_spec"] = load_feature_spec(_frozen_spec)
        except Exception as exc:
            logger.warning("[scorer] frozen feature spec not loaded: %s; falling back to repo spec", exc)
    if artifacts["feature_spec"] is None:
        if d.resolve() == MODEL_DIR.resolve():
            raise FileNotFoundError(
                "feature_spec.yaml required in deploy but not found at %s. "
                "Ensure the deploy package includes models/feature_spec.yaml." % _frozen_spec
            )
        if FEATURE_SPEC_PATH.exists():
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
        logger.debug(
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
        logger.debug(
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
    # Train–serve parity (PLAN § Train–Serve Parity): default to same expression as trainer/config.
    _default_cid_sql = (
        "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
        "THEN NULL ELSE trim(casino_player_id) END"
    )
    cid_sql = getattr(
        config,
        "CASINO_PLAYER_ID_CLEAN_SQL",
        _default_cid_sql,
    )

    bets_query = f"""
        SELECT
            bet_id,
            is_back_bet,
            base_ha,
            bet_type,
            __etl_insert_Dtm,
            payout_complete_dtm,
            COALESCE(gaming_day, toDate(payout_complete_dtm)) AS gaming_day,
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

    # Align with warehouse: UTC instants -> tz-aware HK.
    # Some drivers return naive datetimes; interpret as UTC then tz_convert(HK_TZ).
    if not bets.empty and "payout_complete_dtm" in bets.columns:
        _pc = pd.to_datetime(bets["payout_complete_dtm"], errors="coerce")
        if getattr(_pc.dt, "tz", None) is None:
            _pc = _pc.dt.tz_localize(UTC_TZ, ambiguous="NaT", nonexistent="shift_forward")
        bets["payout_complete_dtm"] = _pc.dt.tz_convert(HK_TZ)
    if not bets.empty and "__etl_insert_Dtm" in bets.columns:
        _etl = pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce")
        if getattr(_etl.dt, "tz", None) is None:
            _etl = _etl.dt.tz_localize(UTC_TZ, ambiguous="NaT", nonexistent="shift_forward")
        bets["__etl_insert_Dtm"] = _etl.dt.tz_convert(HK_TZ)

    sessions = client.query_df(session_query, parameters=params)
    logger.debug("[scorer] Fetched %d bets, %d sessions", len(bets), len(sessions))
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
        existing_validation_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(validation_results)").fetchall()
        }
        for col_name, col_type in _VALIDATION_RESULTS_MIGRATION_COLS:
            if col_name not in existing_validation_cols:
                conn.execute(
                    f"ALTER TABLE validation_results ADD COLUMN {col_name} {col_type}"
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
        ensure_runtime_rated_threshold_schema(conn)
        conn.commit()


def ensure_runtime_rated_threshold_schema(conn: sqlite3.Connection) -> None:
    """State DB: single-row override for rated alert threshold (T-OnlineCalibration / DEC-032)."""
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
            precision_at_threshold REAL
        )
        """
    )


def upsert_runtime_rated_threshold(
    conn: sqlite3.Connection,
    rated_threshold: float,
    *,
    source: str = "calibration",
    n_mature: Optional[int] = None,
    n_pos: Optional[int] = None,
    window_hours: Optional[float] = None,
    recall_at_threshold: Optional[float] = None,
    precision_at_threshold: Optional[float] = None,
) -> None:
    """Replace the single runtime threshold row (id=1). Caller must commit if needed."""
    t = float(rated_threshold)
    if not math.isfinite(t) or t <= 0.0 or t >= 1.0:
        raise ValueError(
            f"rated_threshold must be strictly between 0 and 1; got {rated_threshold!r}"
        )
    ensure_runtime_rated_threshold_schema(conn)
    now = datetime.now(HK_TZ).isoformat()
    conn.execute(
        """
        INSERT INTO runtime_rated_threshold (
            id, rated_threshold, updated_at, source, n_mature, n_pos,
            window_hours, recall_at_threshold, precision_at_threshold
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            rated_threshold = excluded.rated_threshold,
            updated_at = excluded.updated_at,
            source = excluded.source,
            n_mature = excluded.n_mature,
            n_pos = excluded.n_pos,
            window_hours = excluded.window_hours,
            recall_at_threshold = excluded.recall_at_threshold,
            precision_at_threshold = excluded.precision_at_threshold
        """,
        (
            t,
            now,
            source,
            n_mature,
            n_pos,
            window_hours,
            recall_at_threshold,
            precision_at_threshold,
        ),
    )


def read_effective_runtime_rated_threshold(
    conn: sqlite3.Connection, bundle_threshold: float
) -> float:
    """Return state DB override when valid and fresh; else ``bundle_threshold``."""
    bundle_t = float(bundle_threshold)
    if not math.isfinite(bundle_t):
        bundle_t = 0.5
    try:
        row = conn.execute(
            "SELECT rated_threshold, updated_at FROM runtime_rated_threshold WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return bundle_t
    if not row:
        return bundle_t
    t = float(row[0])
    ts_raw = row[1]
    if not math.isfinite(t) or t <= 0.0 or t >= 1.0:
        logger.warning(
            "[scorer] runtime_rated_threshold out of range (%r); using bundle %.4f",
            t,
            bundle_t,
        )
        return bundle_t
    max_age = getattr(config, "RUNTIME_THRESHOLD_MAX_AGE_HOURS", None)
    ts_text: Optional[str] = None
    if isinstance(ts_raw, str):
        ts_text = ts_raw.strip() or None
    elif ts_raw is not None:
        ts_text = str(ts_raw).strip() or None
    if max_age is not None and float(max_age) > 0.0:
        if not ts_text:
            logger.warning(
                "[scorer] runtime_rated_threshold updated_at missing/blank with TTL; using bundle %.4f",
                bundle_t,
            )
            return bundle_t
        try:
            ts = pd.Timestamp(ts_text)
            if ts.tzinfo is None:
                ts = ts.tz_localize(HK_TZ)
            else:
                ts = ts.tz_convert(HK_TZ)
            now = pd.Timestamp.now(tz=HK_TZ)
            age_h = float((now - ts).total_seconds()) / 3600.0
            if age_h > float(max_age):
                logger.debug(
                    "[scorer] runtime_rated_threshold stale (%.2fh > max %.2fh); using bundle %.4f",
                    age_h,
                    float(max_age),
                    bundle_t,
                )
                return bundle_t
        except Exception as exc:
            logger.warning(
                "[scorer] runtime_rated_threshold updated_at parse failed (%s); using bundle",
                exc,
            )
            return bundle_t
    if abs(t - bundle_t) > 1e-9:
        logger.debug("[scorer] Using runtime_rated_threshold=%.4f (bundle was %.4f)", t, bundle_t)
    return t


def _get_last_processed_end(conn: sqlite3.Connection) -> Optional[pd.Timestamp]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_processed_end'"
    ).fetchone()
    return _meta_iso_to_hk(row[0]) if row else None


def _get_last_processed_etl_insert(conn: sqlite3.Connection) -> Optional[pd.Timestamp]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_processed_etl_insert'"
    ).fetchone()
    if row:
        # Watermark max(__etl_insert_Dtm) is written from HK-zoned Timestamps.
        return _meta_iso_to_hk(row[0])
    # Backward compatibility: bootstrap from legacy watermark if present.
    return _get_last_processed_end(conn)


def _set_last_processed_end(conn: sqlite3.Connection, dt: datetime) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('last_processed_end', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (dt.isoformat(),),
    )


def _set_last_processed_etl_insert(conn: sqlite3.Connection, dt: datetime) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('last_processed_etl_insert', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (dt.isoformat(),),
    )


def _effective_incremental_cursor(bets: pd.DataFrame) -> pd.Series:
    """Use __etl_insert_Dtm as incremental cursor; fallback to payout_complete_dtm.

    Assumes ``fetch_recent_data`` already HK-normalized both columns when present;
    this path repeats the same UTC→HK contract so unit tests / stubs stay safe.
    """
    if "__etl_insert_Dtm" in bets.columns:
        cursor = _warehouse_timestamp_series_to_hk(bets["__etl_insert_Dtm"])
    else:
        cursor = pd.Series(pd.NaT, index=bets.index)
    if "payout_complete_dtm" in bets.columns:
        payout_ts = _warehouse_timestamp_series_to_hk(bets["payout_complete_dtm"])
        cursor = cursor.fillna(payout_ts)
    return cursor


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
    last_processed = _get_last_processed_etl_insert(conn)
    effective_cursor = _effective_incremental_cursor(bets)
    new_bets = (
        bets[effective_cursor > last_processed]
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
    max_cursor = effective_cursor.max()
    if pd.notna(max_cursor):
        _set_last_processed_etl_insert(conn, pd.Timestamp(max_cursor).to_pydatetime())
    # Keep legacy key updated for rollback compatibility.
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


def get_session_totals_bulk(
    conn: sqlite3.Connection,
    session_ids: List[object],
) -> Dict[str, Tuple[int, float, Optional[pd.Timestamp], Optional[pd.Timestamp]]]:
    """Batch version of get_session_totals to avoid per-row SQLite queries."""
    keys = [str(sid) for sid in session_ids if sid is not None and not pd.isna(sid)]
    if not keys:
        return {}
    out: Dict[str, Tuple[int, float, Optional[pd.Timestamp], Optional[pd.Timestamp]]] = {}
    for i in range(0, len(keys), _SQLITE_IN_CHUNK_SIZE):
        chunk = keys[i : i + _SQLITE_IN_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT session_id, bet_count, sum_wager, first_ts, last_ts
            FROM session_stats
            WHERE session_id IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        for sid, bet_count, sum_wager, first_ts, last_ts in rows:
            sid_str = str(sid)
            out[sid_str] = (
                int(bet_count) if bet_count is not None else 0,
                float(sum_wager) if sum_wager is not None else 0.0,
                pd.to_datetime(first_ts) if first_ts else None,
                pd.to_datetime(last_ts) if last_ts else None,
            )
    return out


def get_session_count_bulk(conn: sqlite3.Connection, player_ids: List[object]) -> Dict[str, int]:
    """Batch player session counts."""
    keys = [str(pid) for pid in player_ids if pid is not None and not pd.isna(pid)]
    if not keys:
        return {}
    out: Dict[str, int] = {}
    for i in range(0, len(keys), _SQLITE_IN_CHUNK_SIZE):
        chunk = keys[i : i + _SQLITE_IN_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT player_id, COUNT(*)
            FROM session_stats
            WHERE player_id IN ({placeholders})
            GROUP BY player_id
            """,
            chunk,
        ).fetchall()
        for pid, cnt in rows:
            out[str(pid)] = int(cnt)
    return out


def get_historical_avg_bulk(conn: sqlite3.Connection, player_ids: List[object]) -> Dict[str, float]:
    """Batch historical average wager by player."""
    keys = [str(pid) for pid in player_ids if pid is not None and not pd.isna(pid)]
    if not keys:
        return {}
    out: Dict[str, float] = {}
    for i in range(0, len(keys), _SQLITE_IN_CHUNK_SIZE):
        chunk = keys[i : i + _SQLITE_IN_CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT player_id, SUM(sum_wager), SUM(bet_count)
            FROM session_stats
            WHERE player_id IN ({placeholders})
            GROUP BY player_id
            """,
            chunk,
        ).fetchall()
        for pid, sum_wager, sum_bets in rows:
            if sum_bets is None or float(sum_bets) == 0.0:
                out[str(pid)] = 0.0
            else:
                out[str(pid)] = float(sum_wager) / float(sum_bets)
    return out


def _check_numba_runtime_once() -> None:
    """One-time best-effort numba runtime check for deploy observability."""
    global _NUMBA_CHECK_DONE
    if _NUMBA_CHECK_DONE:
        return
    _NUMBA_CHECK_DONE = True
    try:
        from numba import njit  # type: ignore[import]

        @njit(cache=False)
        def _probe(x: int) -> int:
            return x + 1

        _ = _probe(1)
        logger.debug("[scorer] numba runtime check: available")
    except Exception as exc:
        logger.warning("[scorer] numba runtime check failed: %s", exc)


def _select_incremental_bets_window(
    bets: pd.DataFrame,
    new_bets: pd.DataFrame,
    canonical_map: pd.DataFrame,
) -> pd.DataFrame:
    """Shrink feature input to players linked to current new bets.

    Reliability-first: only narrows when mapping signals are complete enough;
    otherwise falls back to full bets window.
    """
    if bets.empty or new_bets.empty:
        return bets
    if "player_id" not in bets.columns or "player_id" not in new_bets.columns:
        return bets

    try:
        new_pids = set(
            new_bets["player_id"].dropna().astype(str).tolist()
        )
        if not new_pids:
            return bets
        if canonical_map is None or canonical_map.empty:
            logger.warning("[scorer] Incremental narrowing disabled: canonical_map unavailable")
            return bets
        if not {"player_id", "canonical_id"}.issubset(canonical_map.columns):
            logger.warning("[scorer] Incremental narrowing disabled: canonical_map missing required columns")
            return bets

        cm = canonical_map[["player_id", "canonical_id"]].dropna().copy()
        cm["player_id"] = cm["player_id"].astype(str)
        cm["canonical_id"] = cm["canonical_id"].astype(str)
        target_cids = set(cm.loc[cm["player_id"].isin(new_pids), "canonical_id"].tolist())
        if not target_cids:
            return bets[bets["player_id"].astype(str).isin(new_pids)].copy()
        expanded_pids = set(cm.loc[cm["canonical_id"].isin(target_cids), "player_id"].tolist())
        narrowed = bets[bets["player_id"].astype(str).isin(expanded_pids)].copy()
        if narrowed.empty:
            return bets
        logger.debug(
            "[scorer] Incremental input narrowed bets: %d -> %d rows",
            len(bets),
            len(narrowed),
        )
        return narrowed
    except Exception as exc:
        logger.warning("[scorer] Incremental input narrowing skipped: %s", exc)
        return bets


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
    2. Track Human (features.py): loss_streak, run_id, minutes_since_run_start
       — same functions as trainer.py. (table_hc deferred to Phase 2.)
    3. Session rolling stats: bets_last_5/15/30m, wager_last_10/30m,
       cum_bets, cum_wager, avg_wager_sofar, session_duration_min,
       bets_per_minute (legacy parity with trainer.add_legacy_features).
    4. Time-of-day cyclic encoding.
    """
    if bets.empty:
        return bets.copy()

    # R3505 / Code Review: reject invalid cutoff_time (None, NaT, date)
    if cutoff_time is None:
        raise ValueError("build_features_for_scoring: cutoff_time is required and must be a valid datetime")
    if isinstance(cutoff_time, date) and not isinstance(cutoff_time, datetime):
        raise ValueError("build_features_for_scoring: cutoff_time must be a datetime, not date")
    _ct = pd.Timestamp(cutoff_time)
    if pd.isna(_ct):
        raise ValueError("build_features_for_scoring: cutoff_time is required and must be a valid datetime")

    bets_df = bets.copy()

    # ── Normalise types (ClickHouse may return object/string; LightGBM needs int/float/bool) ──
    # Skip categorical columns set by normalizer (PLAN Phase 4: do not overwrite).
    for col in ["position_idx", "payout_odds", "base_ha", "is_back_bet", "wager"]:
        if col not in bets_df.columns:
            bets_df[col] = 0.0
    for col in ["position_idx", "payout_odds", "base_ha", "wager"]:
        if isinstance(bets_df[col].dtype, pd.CategoricalDtype):
            continue
        bets_df[col] = pd.to_numeric(bets_df[col], errors="coerce").fillna(0)
    if not isinstance(bets_df["is_back_bet"].dtype, pd.CategoricalDtype):
        bets_df["is_back_bet"] = pd.to_numeric(bets_df["is_back_bet"], errors="coerce").fillna(0)
    bets_df["status"] = bets_df.get("status", pd.Series("", index=bets_df.index)).astype(str).str.upper()

    # Normalise payout_complete_dtm to tz-naive HK local time (R23-style fix)
    pcd = pd.to_datetime(bets_df["payout_complete_dtm"])
    if pcd.dt.tz is not None:
        pcd = pcd.dt.tz_convert(HK_TZ).dt.tz_localize(None)
    bets_df["payout_complete_dtm"] = pcd

    # Normalise cutoff_time to tz-naive HK (R3505: convert before strip; use HK_TZ for SSOT)
    ct = _ct
    if ct.tz is not None:
        cutoff_naive = ct.tz_convert(HK_TZ).tz_localize(None).to_pydatetime()
    else:
        cutoff_naive = ct.to_pydatetime()

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

    # Stable sort (required by Track Human functions and labels.py)
    bets_df = bets_df.sort_values(
        ["canonical_id", "payout_complete_dtm", "bet_id"], kind="stable"
    ).reset_index(drop=True)

    # ── Track Human (same lookback as trainer for train–serve parity) ────────
    _lookback_hours = getattr(config, "SCORER_LOOKBACK_HOURS", 8)
    bets_df["loss_streak"] = compute_loss_streak(
        bets_df, cutoff_time=cutoff_naive, lookback_hours=_lookback_hours
    ).fillna(0)

    rb = compute_run_boundary(
        bets_df, cutoff_time=cutoff_naive, lookback_hours=_lookback_hours
    )
    bets_df["run_id"] = rb["run_id"] if "run_id" in rb.columns else 0
    bets_df["minutes_since_run_start"] = (
        rb["minutes_since_run_start"] if "minutes_since_run_start" in rb.columns else 0.0
    )
    bets_df["bets_in_run_so_far"] = (
        rb["bets_in_run_so_far"] if "bets_in_run_so_far" in rb.columns else 0
    )
    bets_df["wager_sum_in_run_so_far"] = (
        rb["wager_sum_in_run_so_far"] if "wager_sum_in_run_so_far" in rb.columns else 0.0
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

        merge_cols = ["session_id", "session_start_dtm", "session_end_dtm"]
        if "casino_player_id" in sess_df.columns:
            merge_cols.append("casino_player_id")
        bets_df = bets_df.merge(
            sess_df[merge_cols],
            on="session_id",
            how="left",
        )
    if "casino_player_id" not in bets_df.columns:
        bets_df["casino_player_id"] = pd.NA

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
            counts: np.ndarray = np.empty(n, dtype=np.float64)
            for i, t in enumerate(ts):
                while t - ts[lo] > win_ns:
                    lo += 1
                counts[i] = i - lo + 1
            out[f"bets_last_{window}m"] = counts
        for window in (10, 30):
            win_ns = window * 60 * int(1e9)
            lo = 0
            sums: np.ndarray = np.empty(n, dtype=np.float64)
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

# In deploy, main.py sets DATA_DIR (e.g. deploy root / "data"); use it for profile + canonical.
# Only treat as deploy path when non-empty after strip (avoid Path("") or Path("  ") = cwd).
_data_dir_env = os.environ.get("DATA_DIR")
_DATA_DIR: Path | None
if _data_dir_env and _data_dir_env.strip():
    _DATA_DIR = Path(_data_dir_env.strip())
    _LOCAL_PARQUET_PROFILE = _DATA_DIR / "player_profile.parquet"
    CANONICAL_MAPPING_PARQUET = _DATA_DIR / "canonical_mapping.parquet"
    CANONICAL_MAPPING_CUTOFF_JSON = _DATA_DIR / "canonical_mapping.cutoff.json"
else:
    _DATA_DIR = None
    _LOCAL_PARQUET_PROFILE = PROJECT_ROOT / "data" / "player_profile.parquet"
    CANONICAL_MAPPING_PARQUET = PROJECT_ROOT / "data" / "canonical_mapping.parquet"
    CANONICAL_MAPPING_CUTOFF_JSON = PROJECT_ROOT / "data" / "canonical_mapping.cutoff.json"

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
            logger.debug("[scorer] player_profile: %d rows from local Parquet", len(df))
            df_loaded = df if not df.empty else None
        except Exception as exc:
            logger.debug("[scorer] profile local Parquet skipped: %s", exc)

    # player_profile is a locally-derived table (built by etl_player_profile.py from
    # t_session); it is never stored in ClickHouse.  The ClickHouse path is removed
    # to prevent spurious Code-60 errors on every scoring call.
    if df_loaded is None:
        logger.debug(
            "[scorer] player_profile not found at %s — "
            "run etl_player_profile.py first; profile features will be NaN",
            _LOCAL_PARQUET_PROFILE,
        )

    # R85: populate cache on successful load
    if df_loaded is not None:
        _profile_cache = {"df": df_loaded, "loaded_at": datetime.now(), "loaded_for": as_of_dtm}

    return df_loaded


# ── Phase 2 P1.1 Prediction log (PLAN T4) ───────────────────────────────────────

def _ensure_prediction_log_table(conn: sqlite3.Connection) -> None:
    """Create prediction_log table if not exists (independent DB, WAL)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_log (
            prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scored_at TEXT NOT NULL,
            bet_id TEXT,
            session_id TEXT,
            player_id TEXT,
            canonical_id TEXT,
            casino_player_id TEXT,
            table_id TEXT,
            model_version TEXT NOT NULL,
            score REAL NOT NULL,
            margin REAL NOT NULL,
            is_alert INTEGER NOT NULL,
            is_rated_obs INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_scored_at ON prediction_log(scored_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_model_version ON prediction_log(model_version)"
    )
    _ensure_prediction_export_meta(conn)


def _ensure_prediction_export_meta(conn: sqlite3.Connection) -> None:
    """Create export watermark and audit tables if not exist (PLAN T5)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_export_meta (
            key TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_export_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts TEXT NOT NULL,
            end_ts TEXT,
            min_prediction_id INTEGER,
            max_prediction_id INTEGER,
            row_count INTEGER,
            artifact_path TEXT,
            success INTEGER NOT NULL,
            error_message TEXT
        )
        """
    )


def ensure_prediction_calibration_schema(conn: sqlite3.Connection) -> None:
    """prediction_log.db: labels + calibration audit tables (T-OnlineCalibration / DEC-032)."""
    _ensure_prediction_log_table(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_ground_truth (
            bet_id TEXT PRIMARY KEY,
            label REAL NOT NULL,
            status TEXT NOT NULL,
            labeled_at TEXT,
            prediction_id INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pgt_status ON prediction_ground_truth(status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calibration_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            window_start TEXT,
            window_end TEXT,
            window_hours REAL,
            n_rows_used INTEGER,
            n_pos INTEGER,
            suggested_threshold REAL,
            applied_to_state INTEGER NOT NULL,
            skipped_reason TEXT,
            summary_json TEXT
        )
        """
    )


def _append_prediction_log(
    pl_path: str,
    scored_at: str,
    model_version: str,
    df: pd.DataFrame,
) -> None:
    """Batch-insert scored rows into prediction_log DB. No-op if df is empty."""
    if df.empty:
        return
    Path(pl_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(pl_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        _ensure_prediction_log_table(conn)
        rows = []
        for _, row in df.iterrows():
            ialert = 1 if (row["margin"] >= 0 and row["is_rated_obs"] == 1) else 0
            rows.append(
                (
                    scored_at,
                    None if pd.isna(row.get("bet_id")) else str(row["bet_id"]),
                    None if pd.isna(row.get("session_id")) else str(row["session_id"]),
                    None if pd.isna(row.get("player_id")) else str(row["player_id"]),
                    None if pd.isna(row.get("canonical_id")) else str(row["canonical_id"]),
                    None if pd.isna(row.get("casino_player_id")) else str(row["casino_player_id"]),
                    None if pd.isna(row.get("table_id")) else str(row["table_id"]),
                    model_version,
                    float(row["score"]),
                    float(row["margin"]),
                    ialert,
                    int(row["is_rated_obs"]),
                )
            )
        conn.executemany(
            """
            INSERT INTO prediction_log (
                scored_at, bet_id, session_id, player_id, canonical_id,
                casino_player_id, table_id, model_version, score, margin,
                is_alert, is_rated_obs
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_prediction_log_summary_table(conn: sqlite3.Connection) -> None:
    """Create prediction_log_summary + indexes (Unified Plan v2 T4)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prediction_log_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            model_version TEXT NOT NULL,
            window_minutes INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            alert_rate REAL NOT NULL,
            mean_score REAL NOT NULL,
            mean_margin REAL NOT NULL,
            rated_obs_count INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_summary_recorded_at "
        "ON prediction_log_summary(recorded_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prediction_log_summary_model_version "
        "ON prediction_log_summary(model_version)"
    )


def _export_prediction_log_summary(pl_path: str, model_version: str, scored_at: str) -> None:
    """Aggregate last N minutes of prediction_log and append one summary row (T4).

    Rows are filtered by ``model_version`` to match the current scorer bundle. ``scored_at``
    (ISO) is the window anchor and becomes ``recorded_at`` on the summary row. Overlapping
    windows + fixed poll interval yield correlated samples — treat as an approximate dashboard,
    not i.i.d. statistics (Unified Plan v2).
    """
    window_min = int(getattr(config, "PREDICTION_LOG_SUMMARY_WINDOW_MINUTES", 60))
    if window_min <= 0:
        return
    anchor = pd.Timestamp(scored_at)
    if anchor.tz is None:
        anchor = anchor.tz_localize(HK_TZ)
    else:
        anchor = anchor.tz_convert(HK_TZ)
    cutoff = anchor - pd.Timedelta(minutes=window_min)
    cutoff_str = cutoff.isoformat()

    conn = sqlite3.connect(pl_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        _ensure_prediction_log_table(conn)
        _ensure_prediction_log_summary_table(conn)
        row = conn.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(is_alert), 0),
                   AVG(score),
                   AVG(margin),
                   COALESCE(SUM(is_rated_obs), 0)
            FROM prediction_log
            WHERE scored_at >= ? AND model_version = ?
            """,
            (cutoff_str, model_version),
        ).fetchone()
        n = int(row[0] or 0)
        n_alert = int(row[1] or 0)
        avg_score, avg_margin = row[2], row[3]
        rated_sum = int(row[4] or 0)
        alert_rate = float(n_alert) / float(n) if n > 0 else 0.0
        mean_score = float(avg_score) if avg_score is not None and pd.notna(avg_score) else 0.0
        mean_margin = float(avg_margin) if avg_margin is not None and pd.notna(avg_margin) else 0.0
        conn.execute(
            """
            INSERT INTO prediction_log_summary (
                recorded_at, model_version, window_minutes, row_count,
                alert_rate, mean_score, mean_margin, rated_obs_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scored_at,
                model_version,
                window_min,
                n,
                alert_rate,
                mean_score,
                mean_margin,
                rated_sum,
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ── Scoring / H3 routing ──────────────────────────────────────────────────────

def _score_df(
    df: pd.DataFrame,
    artifacts: dict,
    feature_list: List[str],
    *,
    rated_threshold: Optional[float] = None,
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
    scores: np.ndarray = np.zeros(len(df), dtype=float)

    if rated_art is not None and len(df) > 0:
        model_features = rated_art.get("features") or feature_list
        scores = rated_art["model"].predict_proba(df[model_features])[:, 1]

    df["score"] = scores
    df["is_rated_obs"] = is_rated.astype(int)

    bundle_t = float((rated_art or {}).get("threshold", 0.5))
    rated_t = float(rated_threshold) if rated_threshold is not None else bundle_t
    if not math.isfinite(rated_t):
        rated_t = bundle_t
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


# DB-wide alerts/hour uses span of reference times; floor avoids ÷0 when all share one instant.
_MIN_ALERT_RATE_DB_SPAN_HOURS = 1.0


def _avg_alerts_per_hour_db_by_bet_ts(conn: sqlite3.Connection) -> Optional[float]:
    """Alerts per hour over all rows in ``alerts``: ``count / span_hours``.

    Reference time per row is ``bet_ts`` when present, else ``ts`` (alert/score time).
    ``span_hours`` is ``max(ref) - min(ref)`` in hours, at least
    ``_MIN_ALERT_RATE_DB_SPAN_HOURS``. Returns ``None`` if the table is empty or times
    are unusable.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*), MIN(COALESCE(bet_ts, ts)), MAX(COALESCE(bet_ts, ts)) FROM alerts"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or int(row[0]) <= 0:
        return None
    n_total, tmin_s, tmax_s = int(row[0]), row[1], row[2]
    if not tmin_s or not tmax_s:
        return None
    try:
        t_min = pd.to_datetime(tmin_s, errors="coerce")
        t_max = pd.to_datetime(tmax_s, errors="coerce")
    except Exception:
        return None
    if pd.isna(t_min) or pd.isna(t_max):
        return None
    if t_min.tzinfo is None:
        t_min = t_min.tz_localize(HK_TZ)
    else:
        t_min = t_min.tz_convert(HK_TZ)
    if t_max.tzinfo is None:
        t_max = t_max.tz_localize(HK_TZ)
    else:
        t_max = t_max.tz_convert(HK_TZ)
    delta_h = (t_max - t_min).total_seconds() / 3600.0
    span_h = max(delta_h, _MIN_ALERT_RATE_DB_SPAN_HOURS)
    return float(n_total) / span_h


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

    def _cid(v: object) -> Optional[str]:
        """Casino player ID: None/pd.NA/empty or whitespace-only -> None (FND-03 / Review §1)."""
        if v is None or pd.isna(v):
            return None
        s = str(v).strip()
        return s if s else None

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
            margin, scored_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
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
            scored_at=excluded.scored_at,
            casino_player_id=excluded.casino_player_id
        """,
        rows,
    )


# ── Main scoring loop ─────────────────────────────────────────────────────────


def _naive_ts_for_compare(ts: object) -> pd.Timestamp:
    """Return tz-naive timestamp for cutoff vs now comparison (R51: keep replace out of score_once source)."""
    t = pd.Timestamp(ts)
    return t.replace(tzinfo=None) if t.tzinfo else t


def score_once(
    artifacts: dict,
    lookback_hours: int,
    alert_history: set,
    conn: sqlite3.Connection,
    retention_hours: int = RETENTION_HOURS,
    rebuild_canonical_mapping: bool = False,
) -> None:
    """Run one scoring cycle.

    Steps
    -----
    1. Fetch bets + sessions (FND-01 CTE, H2 session_avail_dtm gate).
    2. D2 identity: load from data/canonical_mapping.parquet if cutoff >= now and not --rebuild;
       else build_canonical_mapping_from_df(sessions, cutoff_dtm=now_hk).
    3. Build Track Human + session rolling features (train-serve parity).
    4. Set is_rated flag: canonical_id in rated mapping.
    5. Single rated model scoring via _score_df (v10 DEC-021).
    6. SHAP reason codes for rated alert candidates.
    7. Session stats, duplicate suppression, DB write.
    """
    cycle_stage_seconds: Dict[str, float] = {}
    now_hk = datetime.now(HK_TZ)
    scored_at = now_hk.isoformat()
    feature_list: List[str] = artifacts["feature_list"]
    model_version: str = artifacts["model_version"]

    refresh_alert_history(alert_history, now_hk, conn)
    start = now_hk - timedelta(hours=lookback_hours)
    logger.debug("[scorer] Window: %s -> %s", start.isoformat(), now_hk.isoformat())

    t_clickhouse = time.perf_counter()
    bets, sessions = fetch_recent_data(start, now_hk)
    # Post-Load Normalizer (PLAN § Post-Load Normalizer Phase 4)
    bets, sessions = normalize_bets_sessions(bets, sessions)
    cycle_stage_seconds["clickhouse"] = time.perf_counter() - t_clickhouse
    if bets.empty:
        logger.debug("[scorer] No bets in window; sleeping")
        _emit_scorer_perf_summary(cycle_stage_seconds)
        return

    prune_old_state(conn, now_hk, retention_hours)
    new_bets = update_state_with_new_bets(conn, bets, now_hk)
    logger.debug("[scorer] New bets since last tick: %d", len(new_bets))
    if new_bets.empty:
        logger.debug("[scorer] No new bets to score; sleeping")
        _emit_scorer_perf_summary(cycle_stage_seconds)
        return

    t_features = time.perf_counter()
    # ── D2 identity ───────────────────────────────────────────────────────
    canonical_map = None
    if not rebuild_canonical_mapping and CANONICAL_MAPPING_PARQUET.exists() and CANONICAL_MAPPING_CUTOFF_JSON.exists():
        try:
            with open(CANONICAL_MAPPING_CUTOFF_JSON, encoding="utf-8") as _f:
                _sidecar = json.load(_f)
            _cutoff_str = _sidecar.get("cutoff_dtm")
            _cutoff_ts = pd.Timestamp(_cutoff_str) if _cutoff_str else None
            if _cutoff_ts is not None:
                _cutoff_naive = _naive_ts_for_compare(_cutoff_ts)
                now_naive = _naive_ts_for_compare(now_hk)
                # In deploy (DATA_DIR set), use persisted file if present so restart does not recompute (DEC-028).
                # In trainer/dev, require cutoff >= now to avoid stale artifact.
                use_persisted = (_DATA_DIR is not None) or (_cutoff_naive >= now_naive)
                if use_persisted:
                    _df = pd.read_parquet(CANONICAL_MAPPING_PARQUET)
                    if set(_df.columns) >= {"player_id", "canonical_id"}:
                        canonical_map = _df
                        logger.debug(
                            "[scorer] Canonical mapping loaded from %s (cutoff %s)",
                            CANONICAL_MAPPING_PARQUET, _cutoff_str,
                        )
        except Exception as exc:
            logger.warning("[scorer] Load canonical mapping artifact failed (%s); will build", exc)
    if canonical_map is None:
        canonical_map = build_canonical_mapping_from_df(sessions, cutoff_dtm=now_hk)
        # DEC-028: in deploy (DATA_DIR set), persist so restart does not recompute
        if _DATA_DIR is not None and not canonical_map.empty:
            try:
                canonical_map.to_parquet(CANONICAL_MAPPING_PARQUET, index=False)
                CANONICAL_MAPPING_CUTOFF_JSON.write_text(
                    json.dumps({"cutoff_dtm": now_hk.isoformat()}, indent=0),
                    encoding="utf-8",
                )
                logger.debug("[scorer] Canonical mapping persisted to %s", CANONICAL_MAPPING_PARQUET)
            except Exception as exc:
                logger.warning("[scorer] Failed to persist canonical mapping: %s", exc)

    # H3: every canonical_id in the mapping is a rated player — identity.py only
    # builds entries for players who have a valid casino_player_id (R36 fix).
    rated_canonical_ids: set = (
        set(canonical_map["canonical_id"].unique()) if not canonical_map.empty else set()
    )

    # ── Features on narrowed incremental window (Phase 3) ──────────────────
    bets_for_features = _select_incremental_bets_window(bets, new_bets, canonical_map)
    features_all = build_features_for_scoring(bets_for_features, sessions, canonical_map, now_hk)

    # Unified Plan v2 (T1): UNRATED_VOLUME_LOG counts must use full features_all ∩ new_bets
    # *before* rated-only slice — otherwise unrated new rows disappear and telemetry lies.
    new_bet_ids_all = set(new_bets["bet_id"].astype(str))
    new_ids = new_bet_ids_all
    _csw = getattr(config, "SCORER_COLD_START_WINDOW_HOURS", None)
    if _csw is not None and float(_csw) > 0 and "payout_complete_dtm" in new_bets.columns:
        _csw_f = min(float(_csw), float(getattr(config, "SCORER_LOOKBACK_HOURS_MAX", 8760)))
        _floor_ts = pd.Timestamp(now_hk - timedelta(hours=_csw_f))
        _pay = pd.to_datetime(new_bets["payout_complete_dtm"], errors="coerce")
        if getattr(_pay.dt, "tz", None) is None:
            _pay = _pay.dt.tz_localize(HK_TZ, ambiguous="NaT", nonexistent="shift_forward")
        else:
            _pay = _pay.dt.tz_convert(HK_TZ)
        _mask = _pay >= _floor_ts
        _mask = _mask.fillna(False)
        new_ids = set(new_bets.loc[_mask, "bet_id"].astype(str))
        logger.debug(
            "[scorer] Payout-age cap (SCORER_COLD_START_WINDOW_HOURS=%gh): score %d of %d new bets",
            _csw_f,
            len(new_ids),
            len(new_bets),
        )
    elif _csw is not None and float(_csw) > 0 and "payout_complete_dtm" not in new_bets.columns:
        logger.debug(
            "[scorer] Payout-age cap configured but payout_complete_dtm missing from new_bets; skipping age filter"
        )
    n_features_full_pre_rated_slice = len(features_all)
    _telemetry_new = features_all[features_all["bet_id"].astype(str).isin(new_bet_ids_all)]
    _tel_is_rated = _telemetry_new["canonical_id"].isin(rated_canonical_ids)
    _tel_is_rated = _tel_is_rated.fillna(False).astype(bool)
    n_rated_new_bets_pre_slice = int(_tel_is_rated.sum())
    n_unrated_new_bets_pre_slice = int(len(_telemetry_new) - n_rated_new_bets_pre_slice)
    unrated_players_new_bets_pre_slice = (
        int(
            _telemetry_new.loc[~_tel_is_rated, "player_id"]
            .dropna()
            .astype(str)
            .nunique()
        )
        if n_unrated_new_bets_pre_slice > 0
        else 0
    )

    # Rated-only slice before Track LLM + profile join (heavy steps); session rolling above unchanged.
    features_all = features_all[features_all["canonical_id"].isin(rated_canonical_ids)].copy()

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
            logger.debug("[scorer] Track LLM computed for scoring window")
        except Exception as exc:
            logger.error("[scorer] Track LLM failed: %s", exc)

    # ── player_profile PIT join (R79) ─────────────────────────────────
    # Attach rated-player profile features via as-of merge (snapshot_dtm <= bet_time).
    # Non-rated bets and bets without a prior snapshot keep NaN — LightGBM handles
    # this natively via the default-child path trained in trainer.py (R74/R79).
    # join_player_profile requires payout_complete_dtm; backfill from bets if missing
    # (e.g. tests mock build_features_for_scoring, or partial feature frames).
    if _join_profile is not None and PROFILE_FEATURE_COLS and rated_canonical_ids:
        if (
            "payout_complete_dtm" not in features_all.columns
            and "bet_id" in features_all.columns
            and "payout_complete_dtm" in bets.columns
        ):
            _t = bets[["bet_id", "payout_complete_dtm"]].drop_duplicates(
                subset=["bet_id"], keep="last"
            )
            _fa = features_all.copy()
            if _fa["bet_id"].dtype != _t["bet_id"].dtype:
                _t = _t.copy()
                _t["bet_id"] = _t["bet_id"].astype(_fa["bet_id"].dtype)
            features_all = _fa.merge(_t, on="bet_id", how="left")
        _profile_df = _load_profile_for_scoring(rated_canonical_ids, now_hk)
        if _profile_df is not None:
            features_all = _join_profile(features_all, _profile_df)
            logger.debug("[scorer] player_profile PIT join applied")
        else:
            logger.debug(
                "[scorer] player_profile unavailable — profile features will be NaN"
            )

    features_all["is_rated"] = features_all["canonical_id"].isin(rated_canonical_ids)
    logger.debug(
        "[scorer] Feature rows: full_window=%d rated_slice(LLM/profile path)=%d",
        n_features_full_pre_rated_slice,
        len(features_all),
    )

    features_df = features_all[
        features_all["bet_id"].astype(str).isin(new_ids)
    ].copy()
    logger.debug("[scorer] Rows to score (new bets): %d", len(features_df))
    cycle_stage_seconds["feature_engineering"] = time.perf_counter() - t_features
    if features_df.empty:
        logger.info("[scorer] No usable rows after feature engineering; sleeping")
        _emit_scorer_perf_summary(cycle_stage_seconds)
        return

    # --- Exclude unrated before model (post rated-only slice this should be all True; keep filter for safety) ---
    is_rated_mask = features_df["is_rated"].fillna(False).astype(bool)
    features_df = features_df[is_rated_mask].copy()
    if features_df.empty:
        logger.debug("[scorer] No rated bets to score; sleeping")
        _emit_scorer_perf_summary(cycle_stage_seconds)
        return
    if UNRATED_VOLUME_LOG and n_unrated_new_bets_pre_slice > 0:
        logger.debug(
            "[scorer] Excluded %d unrated bets (%d players); scoring %d rated bets.",
            n_unrated_new_bets_pre_slice,
            unrated_players_new_bets_pre_slice,
            n_rated_new_bets_pre_slice,
        )

    # ── Score with H3 routing (rated only) ─────────────────────────────────
    bundle_thr = float((artifacts.get("rated") or {}).get("threshold", 0.5))
    effective_thr = read_effective_runtime_rated_threshold(conn, bundle_thr)
    t_predict = time.perf_counter()
    features_df = _score_df(
        features_df, artifacts, feature_list, rated_threshold=effective_thr
    )
    cycle_stage_seconds["predict"] = time.perf_counter() - t_predict

    # ── Phase 2 P1.1: append all scored rows to prediction_log (before alert filter) ──
    sqlite_seconds = 0.0
    pl_path = getattr(config, "PREDICTION_LOG_DB_PATH", None) or ""
    if (pl_path and str(pl_path).strip() and features_df is not None and not features_df.empty):
        t_sqlite = time.perf_counter()
        try:
            _append_prediction_log(
                str(pl_path).strip(),
                scored_at,
                model_version,
                features_df,
            )
            try:
                _export_prediction_log_summary(
                    str(pl_path).strip(), model_version, scored_at
                )
            except Exception as exc2:
                logger.warning("[scorer] Prediction log summary failed: %s", exc2)
        except Exception as exc:
            logger.warning("[scorer] Prediction log write failed: %s", exc)
        sqlite_seconds += time.perf_counter() - t_sqlite

    # ── Alert candidates: score >= threshold AND rated observations only ──
    # UNRATED_VOLUME_LOG uses pre-slice new-bet counts; unrated rows are not scored (v10 DEC-021).
    alert_candidates = features_df[
        (features_df["margin"] >= 0) & (features_df["is_rated_obs"] == 1)
    ].copy()
    if alert_candidates.empty:
        logger.debug("[scorer] No above-threshold alerts this cycle")
        cycle_stage_seconds["sqlite"] = sqlite_seconds
        _emit_scorer_perf_summary(cycle_stage_seconds)
        return
    logger.debug("[scorer] Above-threshold rows: %d", len(alert_candidates))

    # ── SHAP reason codes for rated alert candidates ──────────────────────
    # Guarded by config flag to avoid per-cycle SHAP overhead in production.
    alert_candidates["reason_codes"] = "[]"
    rated_art = artifacts.get("rated")
    rated_mask_ac = alert_candidates["is_rated_obs"].astype(bool)

    rated_idx = alert_candidates.index[rated_mask_ac].tolist()
    if (
        getattr(config, "SCORER_ENABLE_SHAP_REASON_CODES", False)
        and rated_idx
        and rated_art is not None
        and feature_list
    ):
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
    session_totals_cache = get_session_totals_bulk(conn, unique_sids)

    def _st(sid: object) -> Tuple[int, float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
        return session_totals_cache.get(str(sid), (0, 0.0, None, None))

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
    unique_pids = alert_candidates["player_id"].dropna().astype(str).unique().tolist()
    session_count_cache = get_session_count_bulk(conn, unique_pids)
    historical_avg_cache = get_historical_avg_bulk(conn, unique_pids)
    alert_candidates["session_count"] = alert_candidates["player_id"].apply(
        lambda pid: session_count_cache.get(str(pid), 0)
    )
    alert_candidates["historical_avg_bet"] = alert_candidates["player_id"].apply(
        lambda pid: historical_avg_cache.get(str(pid), 0.0)
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
            logger.debug("[scorer] Suppressed %d duplicate alerts", suppressed)
        alert_candidates.drop(columns=["_bid_str"], inplace=True)

    if alert_candidates.empty:
        logger.debug("[scorer] Alerts suppressed (already sent)")
        cycle_stage_seconds["sqlite"] = sqlite_seconds
        _emit_scorer_perf_summary(cycle_stage_seconds)
        return

    t_sqlite_alert = time.perf_counter()
    append_alerts(conn, alert_candidates)
    alert_history.update(alert_candidates["bet_id"].astype(str).tolist())
    _rate = _avg_alerts_per_hour_db_by_bet_ts(conn)
    if _rate is not None:
        logger.info(
            "[scorer] Emitted %d alerts; avg. number of alerts per hour = %.2f",
            len(alert_candidates),
            _rate,
        )
    else:
        logger.info("[scorer] Emitted %d alerts", len(alert_candidates))
    conn.commit()
    sqlite_seconds += time.perf_counter() - t_sqlite_alert
    cycle_stage_seconds["sqlite"] = sqlite_seconds
    _emit_scorer_perf_summary(cycle_stage_seconds)


# ── Programmatic entry (for deploy: one process runs scorer + validator + API) ──

def run_scorer_loop(
    interval_seconds: int | None = None,
    lookback_hours: int | None = None,
    model_dir: Optional[Path] = None,
    once: bool = False,
    first_cycle_done: threading.Event | None = None,
) -> None:
    """Run the scorer loop (no argparse). Used by package/deploy/main.py.
    Uses STATE_DB_PATH and MODEL_DIR from env if set.

    If ``first_cycle_done`` is set, it is signaled exactly once after the first
    ``score_once`` call returns (success or exception), so deploy can defer
    validator startup and avoid SQLite startup lock races.
    """
    interval = interval_seconds if interval_seconds is not None else getattr(config, "SCORER_POLL_INTERVAL_SECONDS", 45)
    lookback = lookback_hours if lookback_hours is not None else getattr(config, "SCORER_LOOKBACK_HOURS", 8)
    if interval <= 0 or lookback <= 0:
        raise ValueError("interval_seconds and lookback_hours must be positive")
    _check_numba_runtime_once()
    artifacts = load_dual_artifacts(model_dir)
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
    first_iteration = True
    while True:
        t_start = time.time()
        try:
            score_once(
                artifacts,
                lookback,
                alert_history,
                conn,
                RETENTION_HOURS,
                rebuild_canonical_mapping=False,
            )
        except Exception as exc:
            logger.error("[scorer] ERROR: %s", exc, exc_info=True)
        finally:
            if first_iteration:
                first_iteration = False
                if first_cycle_done is not None:
                    first_cycle_done.set()
        elapsed = time.time() - t_start
        sleep_for = max(0, interval - elapsed)
        if once:
            break
        time.sleep(sleep_for)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Near real-time scorer for walkaway alerts"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=getattr(config, "SCORER_POLL_INTERVAL_SECONDS", 45),
        help="Polling interval in seconds (includes run time)",
    )
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=getattr(config, "SCORER_LOOKBACK_HOURS", 8),
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
    parser.add_argument(
        "--rebuild-canonical-mapping", action="store_true",
        help="Do not load canonical mapping from data/canonical_mapping.parquet; build from current window.",
    )
    args = parser.parse_args()
    if args.lookback_hours <= 0:
        parser.error("--lookback-hours must be positive")
    if args.interval <= 0:
        parser.error("--interval must be positive")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    _check_numba_runtime_once()
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
            score_once(
                artifacts,
                args.lookback_hours,
                alert_history,
                conn,
                RETENTION_HOURS,
                rebuild_canonical_mapping=getattr(args, "rebuild_canonical_mapping", False),
            )
        except Exception as exc:
            logger.error("[scorer] ERROR: %s", exc, exc_info=True)
        elapsed = time.time() - t_start
        sleep_for = max(0, args.interval - elapsed)
        if args.once:
            break
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
