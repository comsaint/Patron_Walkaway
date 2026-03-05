"""trainer/trainer.py — Phase 1 Refactor
=========================================
Patron Walkaway Prediction — Training Pipeline

Pipeline (SSOT §4.3 / §9)
--------------------------
1. time_fold.get_monthly_chunks(start, end)  -> month boundaries
2. Per chunk: load bets + sessions -> DQ -> identity -> labels -> Track-B features
   - Data source: ClickHouse (production) OR local Parquet (dev iteration)
   - Labels use C1 extended pull; bets in (window_end, extended_end] are
     used only for label computation, NOT added to training rows.
3. Write each processed chunk to .data/chunks/ as Parquet.
4. Concatenate all chunks; split train / valid / test at ROW level (time-ordered
   70/15/15 — SSOT §9.2).  Chunks control ETL/cache volume only, not split semantics.
5. sample_weight = 1 / N_run  (canonical_id × run_id from compute_run_boundary), train set only.
6. Optuna TPE hyperparameter search on validation set (per model type).
7. Train Rated LightGBM with class_weight='balanced' + sample_weight (v10 single-model, DEC-021).
8. Atomic artifact bundle -> trainer/models/.

Artifact format (version-tagged, v10 single-model)
--------------------------------------------------
models/
  model.pkl                 LightGBM model for rated (casino-card) players
  feature_list.json         [{name, track}]  track ∈ {"B", "legacy"}
  model_version             YYYYMMDD-HHMMSS-<git7>  (plain text)
  training_metrics.json     validation + test metrics, feature importance (gain), Optuna best params

Backward compatibility
----------------------
The legacy artifact walkaway_model.pkl (single-model dict) is ALSO written
alongside the v10 bundle so that the existing scorer/validator can keep
running until they are refactored in Steps 7–8.

Data source switching
---------------------
  --use-local-parquet   Read from data/ Parquet files instead of
                        ClickHouse.  Same DQ filters + time semantics apply.
  Default: ClickHouse for production.
"""

from __future__ import annotations

import argparse
import calendar
import os
import hashlib
import json
import logging
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve
from zoneinfo import ZoneInfo

optuna.logging.set_verbosity(optuna.logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("trainer")

# ---------------------------------------------------------------------------
# Config imports
# ---------------------------------------------------------------------------
try:
    import config as _cfg  # type: ignore[import]

    WALKAWAY_GAP_MIN = _cfg.WALKAWAY_GAP_MIN
    ALERT_HORIZON_MIN = _cfg.ALERT_HORIZON_MIN
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    BET_AVAIL_DELAY_MIN = _cfg.BET_AVAIL_DELAY_MIN
    SESSION_AVAIL_DELAY_MIN = _cfg.SESSION_AVAIL_DELAY_MIN
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    # G1_PRECISION_MIN / G1_ALERT_VOLUME_MIN_PER_HOUR / G1_FBETA intentionally
    # not imported — deprecated per DEC-009/010; rollback path only.
    PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
    SOURCE_DB = _cfg.SOURCE_DB
    TBET = _cfg.TBET
    TSESSION = _cfg.TSESSION
    TPROFILE: str = getattr(_cfg, "TPROFILE", "player_profile")
    HK_TZ_STR: str = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    TRAINER_DAYS: int = getattr(_cfg, "TRAINER_DAYS", 30)
    CHUNK_CONCAT_MEMORY_WARN_BYTES: int = getattr(_cfg, "CHUNK_CONCAT_MEMORY_WARN_BYTES", 2 * (1024**3))
    CHUNK_CONCAT_RAM_FACTOR: float = getattr(_cfg, "CHUNK_CONCAT_RAM_FACTOR", 3)
    TRAIN_SPLIT_FRAC: float = getattr(_cfg, "TRAIN_SPLIT_FRAC", 0.70)
    VALID_SPLIT_FRAC: float = getattr(_cfg, "VALID_SPLIT_FRAC", 0.15)
    MIN_VALID_TEST_ROWS: int = getattr(_cfg, "MIN_VALID_TEST_ROWS", 50)
    MIN_THRESHOLD_ALERT_COUNT: int = getattr(_cfg, "MIN_THRESHOLD_ALERT_COUNT", 5)
    THRESHOLD_MIN_RECALL: Optional[float] = getattr(_cfg, "THRESHOLD_MIN_RECALL", None)
    THRESHOLD_FBETA: float = getattr(_cfg, "THRESHOLD_FBETA", 0.5)
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

    WALKAWAY_GAP_MIN = _cfg.WALKAWAY_GAP_MIN
    ALERT_HORIZON_MIN = _cfg.ALERT_HORIZON_MIN
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    BET_AVAIL_DELAY_MIN = _cfg.BET_AVAIL_DELAY_MIN
    SESSION_AVAIL_DELAY_MIN = _cfg.SESSION_AVAIL_DELAY_MIN
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    # G1_PRECISION_MIN / G1_ALERT_VOLUME_MIN_PER_HOUR / G1_FBETA intentionally
    # not imported — deprecated per DEC-009/010; rollback path only.
    PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
    SOURCE_DB = _cfg.SOURCE_DB
    TBET = _cfg.TBET
    TSESSION = _cfg.TSESSION
    TPROFILE = getattr(_cfg, "TPROFILE", "player_profile")
    HK_TZ_STR = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    TRAINER_DAYS = getattr(_cfg, "TRAINER_DAYS", 30)
    CHUNK_CONCAT_MEMORY_WARN_BYTES = getattr(_cfg, "CHUNK_CONCAT_MEMORY_WARN_BYTES", 2 * (1024**3))
    CHUNK_CONCAT_RAM_FACTOR = getattr(_cfg, "CHUNK_CONCAT_RAM_FACTOR", 3)
    TRAIN_SPLIT_FRAC = getattr(_cfg, "TRAIN_SPLIT_FRAC", 0.70)
    VALID_SPLIT_FRAC = getattr(_cfg, "VALID_SPLIT_FRAC", 0.15)
    MIN_VALID_TEST_ROWS = getattr(_cfg, "MIN_VALID_TEST_ROWS", 50)
    MIN_THRESHOLD_ALERT_COUNT = getattr(_cfg, "MIN_THRESHOLD_ALERT_COUNT", 5)
    THRESHOLD_MIN_RECALL = getattr(_cfg, "THRESHOLD_MIN_RECALL", None)
    THRESHOLD_FBETA = getattr(_cfg, "THRESHOLD_FBETA", 0.5)

# Module-level pipeline imports (same try/except pattern)
try:
    from time_fold import get_monthly_chunks, get_train_valid_test_split  # type: ignore[import]
    from identity import (  # type: ignore[import]
        build_canonical_mapping_from_df,
        build_canonical_mapping,
        get_dummy_player_ids,
        get_dummy_player_ids_from_df,
    )
    from labels import compute_labels  # type: ignore[import]
    from features import (  # type: ignore[import]
        compute_loss_streak,
        compute_run_boundary,
        compute_track_llm_features,
        load_feature_spec,
        join_player_profile,
        screen_features,
        PROFILE_FEATURE_COLS,
        get_profile_feature_cols,
    )
    from db_conn import get_clickhouse_client  # type: ignore[import]
    from etl_player_profile import (  # type: ignore[import]
        compute_profile_schema_hash,
        LOCAL_PROFILE_SCHEMA_HASH,
        backfill as _etl_backfill,
    )
except ModuleNotFoundError:
    from trainer.time_fold import get_monthly_chunks, get_train_valid_test_split  # type: ignore[import]
    from trainer.identity import (  # type: ignore[import]
        build_canonical_mapping_from_df,
        build_canonical_mapping,
        get_dummy_player_ids,
        get_dummy_player_ids_from_df,
    )
    from trainer.labels import compute_labels  # type: ignore[import]
    from trainer.features import (  # type: ignore[import]
        compute_loss_streak,
        compute_run_boundary,
        compute_track_llm_features,
        load_feature_spec,
        join_player_profile,
        screen_features,
        PROFILE_FEATURE_COLS,
        get_profile_feature_cols,
    )
    from trainer.db_conn import get_clickhouse_client  # type: ignore[import]
    from trainer.etl_player_profile import (  # type: ignore[import]
        compute_profile_schema_hash,
        LOCAL_PROFILE_SCHEMA_HASH,
        backfill as _etl_backfill,
    )

HK_TZ = ZoneInfo(HK_TZ_STR)

# Minimal session columns needed for canonical-map + dummy-player detection.
# Defined at module level so tests can validate coverage against identity._REQUIRED_SESSION_COLS.
# Reading only these columns (instead of all 80+) avoids OOM on the 74M-row session parquet.
_CANONICAL_MAP_SESSION_COLS: list = [
    "session_id", "player_id", "casino_player_id",
    "lud_dtm", "session_start_dtm", "session_end_dtm",
    "is_manual", "is_deleted", "is_canceled", "num_games_with_wager",
    "turnover",
]

# DEPRECATED(DEC-017): FAST_MODE_RATED_SAMPLE_N is no longer used by any
# runtime logic.  Rated sampling was decoupled from fast-mode (R205) and is
# now controlled by the independent --sample-rated N CLI flag.  This constant
# is kept only as a reference; do not use it in new code.
FAST_MODE_RATED_SAMPLE_N: int = 1_000
# DEPRECATED(DEC-019 follow-up): profile snapshots are now forced to month-end
# across all modes, including fast-mode.  Keep this constant only for backward
# compatibility in logs/tests that may still import it.
FAST_MODE_SNAPSHOT_INTERVAL_DAYS: int = 7

BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / ".data"
CHUNK_DIR = DATA_DIR / "chunks"
LOCAL_PARQUET_DIR = PROJECT_ROOT / "data"
FEATURE_SPEC_PATH = BASE_DIR / "feature_spec" / "features_candidates.template.yaml"
MODEL_DIR = BASE_DIR / "models"
OUT_DIR = BASE_DIR / "out_trainer"

for _d in (DATA_DIR, CHUNK_DIR, LOCAL_PARQUET_DIR, MODEL_DIR, OUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Track-B feature column list (shared with scorer via feature_list.json)
# ---------------------------------------------------------------------------
TRACK_B_FEATURE_COLS: List[str] = [
    "loss_streak",
    # "run_id" removed intentionally (R67): it is an ordinal per-player sequence
    # without cross-player comparability. LightGBM might learn spurious patterns.
    # It remains in the DataFrame for sample weighting but is not a model feature.
    "minutes_since_run_start",
]

# Legacy feature columns (kept for backward compat until scorer is refactored)
LEGACY_FEATURE_COLS: List[str] = [
    "wager",
    "payout_odds",
    "base_ha",
    "is_back_bet",
    "position_idx",
    "cum_bets",
    "cum_wager",
    "avg_wager_sofar",
    "time_of_day_sin",
    "time_of_day_cos",
]

ALL_FEATURE_COLS: List[str] = TRACK_B_FEATURE_COLS + LEGACY_FEATURE_COLS + PROFILE_FEATURE_COLS

# Extra days of bet history pulled before each chunk window_start to give
# Track-B state machines (loss_streak, run_boundary) cross-chunk context.
HISTORY_BUFFER_DAYS: int = 2

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _to_hk(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=HK_TZ)
    return dt.astimezone(HK_TZ)


def default_training_window(days: int = TRAINER_DAYS) -> Tuple[datetime, datetime]:
    now = datetime.now(HK_TZ)
    return now - timedelta(days=days), now - timedelta(minutes=30)


def parse_window(args) -> Tuple[datetime, datetime]:
    if args.start or args.end:
        if not (args.start and args.end):
            raise ValueError("Provide both --start and --end or neither")
        start = _to_hk(pd.to_datetime(args.start).to_pydatetime())
        end = _to_hk(pd.to_datetime(args.end).to_pydatetime())
        return start, end
    return default_training_window(getattr(args, "days", TRAINER_DAYS))


# ---------------------------------------------------------------------------
# Model versioning
# ---------------------------------------------------------------------------

def get_model_version() -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        git_hash = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=BASE_DIR,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        git_hash = "nogit"
    return f"{ts}-{git_hash}"


# ---------------------------------------------------------------------------
# ClickHouse data loading (production path)
# ---------------------------------------------------------------------------

_BET_SELECT_COLS = """
    bet_id,
    session_id,
    player_id,
    table_id,
    payout_complete_dtm,
    wager,
    status,
    COALESCE(gaming_day, toDate(payout_complete_dtm)) AS gaming_day,
    is_back_bet,
    base_ha,
    bet_type,
    payout_odds,
    position_idx
""".strip()

_SESSION_SELECT_COLS = """
    session_id,
    player_id,
    CASE WHEN lower(trim(casino_player_id)) IN ('', 'null')
         THEN NULL ELSE trim(casino_player_id) END AS casino_player_id,
    table_id,
    session_start_dtm,
    session_end_dtm,
    COALESCE(lud_dtm, session_end_dtm, session_start_dtm) AS lud_dtm,
    is_manual,
    is_deleted,
    is_canceled,
    COALESCE(turnover, 0) AS turnover,
    COALESCE(num_games_with_wager, 0) AS num_games_with_wager
""".strip()


def load_clickhouse_data(
    window_start: datetime,
    extended_end: datetime,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Query ClickHouse for bets in [window_start, extended_end] and matching sessions."""
    logger.info("ClickHouse pull: %s -> %s", window_start, extended_end)
    client = get_clickhouse_client()
    params = {"start": window_start, "end": extended_end}

    # Pull extra history so Track-B state machines (loss_streak, run_boundary)
    # have cross-chunk context.  process_chunk filters training rows to
    # [window_start, window_end) after Track-B features are computed.
    # E4/F1: exclude invalid player_id (PLAN Step 1)
    # E5: t_bet may use FINAL for read-after-write consistency (G1: t_session must NOT)
    bets_query = f"""
        SELECT {_BET_SELECT_COLS}
        FROM {SOURCE_DB}.{TBET} FINAL
        WHERE payout_complete_dtm >= %(start)s - INTERVAL {HISTORY_BUFFER_DAYS} DAY
          AND payout_complete_dtm < %(end)s
          AND wager > 0
          AND payout_complete_dtm IS NOT NULL
          AND player_id IS NOT NULL
          AND player_id != {PLACEHOLDER_PLAYER_ID}
    """

    # No FINAL on t_session (G1). FND-01 CTE dedup for train-serve parity with scorer/validator.
    # Pull sessions overlapping the window with a ±1-day buffer.
    # FND-02: is_manual=1 rows are accounting adjustments, not real play (R38 parity fix)
    # FND-04: exclude sessions with no real activity (SSOT §5)
    session_query = f"""
        WITH deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY session_id
                       ORDER BY lud_dtm DESC NULLS LAST, __etl_insert_Dtm DESC
                   ) AS rn
            FROM {SOURCE_DB}.{TSESSION}
            WHERE session_start_dtm >= %(start)s - INTERVAL 1 DAY
              AND session_start_dtm < %(end)s + INTERVAL 1 DAY
              AND is_deleted = 0
              AND is_canceled = 0
              AND is_manual = 0
        )
        SELECT {_SESSION_SELECT_COLS}
        FROM deduped
        WHERE rn = 1
          AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
    """

    bets = client.query_df(bets_query, parameters=params)
    sessions = client.query_df(session_query, parameters=params)
    logger.info("Loaded %d bets, %d sessions", len(bets), len(sessions))
    return bets, sessions


# ---------------------------------------------------------------------------
# Local Parquet data loading (dev / offline iteration path)
# ---------------------------------------------------------------------------

def load_local_parquet(
    window_start: datetime,
    extended_end: datetime,
    sessions_only: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load bets + sessions from local Parquet files, filtered to the window.

    Expects:
      data/gmwds_t_bet.parquet     — full t_bet export with the same columns
      data/gmwds_t_session.parquet — full t_session export with the same columns

    Applies the same DQ filters (wager > 0, payout_complete_dtm IS NOT NULL)
    and time window restriction as the ClickHouse path.

    Args:
        sessions_only: If True, skip loading the bet parquet entirely and
            return an empty bets DataFrame.  Use this when only sessions are
            needed (e.g. canonical map build) to avoid OOM on the 400M+ row
            bet file.
    """
    # R402: contract check — module-level _CANONICAL_MAP_SESSION_COLS must include
    # "turnover" so FND-04 DQ logic sees consistent columns in sessions_only mode.
    assert "turnover" in _CANONICAL_MAP_SESSION_COLS, (
        "FND-04 contract violated: _CANONICAL_MAP_SESSION_COLS must include 'turnover'"
    )

    bets_path = LOCAL_PARQUET_DIR / "gmwds_t_bet.parquet"
    sess_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"

    if not sess_path.exists():
        raise FileNotFoundError(
            f"Local Parquet files not found in {LOCAL_PARQUET_DIR}. "
            "Export ClickHouse tables first or run without --use-local-parquet."
        )
    if not sessions_only and not bets_path.exists():
        raise FileNotFoundError(
            f"Local Parquet files not found in {LOCAL_PARQUET_DIR}. "
            "Export ClickHouse tables first or run without --use-local-parquet."
        )

    logger.info("Reading local Parquet: %s%s", LOCAL_PARQUET_DIR, " (sessions only)" if sessions_only else "")

    def _filter_ts(dt, parquet_path: Path, col: str) -> pd.Timestamp:
        """Return a Timestamp compatible with the Parquet column's tz schema.

        Reads the schema of the target file once (cheap: no data rows) to
        determine whether the column is tz-aware or tz-naive, then returns
        either a UTC-aware or tz-naive Timestamp accordingly.

        Background: R28 originally stripped tz for tz-naive columns, but
        ClickHouse exports can produce tz=UTC columns (timestamp[ms, tz=UTC]),
        which requires a tz-aware filter bound.  Mismatched tz triggers
        ArrowNotImplementedError at pushdown time.
        """
        import pyarrow.parquet as pq
        ts = pd.Timestamp(dt)
        try:
            schema = pq.read_schema(parquet_path)
            field = schema.field(col)
            col_tz = getattr(field.type, "tz", None)
        except Exception:
            col_tz = None
        if col_tz:
            # Column is tz-aware — filter must also be tz-aware (UTC)
            return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        else:
            # Column is tz-naive — strip tz from filter (original R28 behaviour)
            return ts.tz_localize(None) if ts.tzinfo is None else ts.replace(tzinfo=None)

    if sessions_only:
        bets = pd.DataFrame()
        # When building canonical map, read only the minimal set of session
        # columns to avoid OOM on the 74M-row × 80-column session parquet.
        import pyarrow.parquet as _pq
        _sess_schema_cols = set(_pq.read_schema(sess_path).names)
        _sess_cols = [c for c in _CANONICAL_MAP_SESSION_COLS if c in _sess_schema_cols]
        # Include optional tiebreaker if present
        if "__etl_insert_Dtm" in _sess_schema_cols:
            _sess_cols.append("__etl_insert_Dtm")
    else:
        # Use pyarrow pushdown filters to avoid loading the full table per chunk (R26).
        bets_lo = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
        bets = pd.read_parquet(
            bets_path,
            filters=[
                ("payout_complete_dtm", ">=", _filter_ts(bets_lo, bets_path, "payout_complete_dtm")),
                ("payout_complete_dtm", "<",  _filter_ts(extended_end, bets_path, "payout_complete_dtm")),
            ],
        )
        # DQ filters are applied fully in apply_dq; quick guards here (E4/F1 parity with ClickHouse).
        # Use one combined mask to avoid double-copy RAM overhead on large Parquet chunks.
        _mask = pd.Series(True, index=bets.index)
        if "wager" in bets.columns:
            _mask &= bets.get("wager", pd.Series(dtype=float)).fillna(0) > 0
        if "player_id" in bets.columns:
            _mask &= bets["player_id"].notna() & (bets["player_id"] != PLACEHOLDER_PLAYER_ID)
        bets = bets[_mask].copy()
        _sess_cols = None  # read all columns for normal chunk processing

    sessions = pd.read_parquet(
        sess_path,
        filters=[
            ("session_start_dtm", ">=", _filter_ts(window_start - timedelta(days=1), sess_path, "session_start_dtm")),
            ("session_start_dtm", "<",  _filter_ts(extended_end + timedelta(days=1), sess_path, "session_start_dtm")),
        ],
        columns=_sess_cols,
    )

    sessions = sessions[
        (sessions.get("is_deleted", pd.Series(0, index=sessions.index)) == 0)
        & (sessions.get("is_canceled", pd.Series(0, index=sessions.index)) == 0)
    ].copy() if len(sessions) > 0 else sessions

    logger.info("Local Parquet: %d bets, %d sessions", len(bets), len(sessions))
    return bets, sessions


# ---------------------------------------------------------------------------
# player_profile loading (PLAN Step 4 / DEC-011)
# ---------------------------------------------------------------------------

def load_player_profile(
    window_start: datetime,
    window_end: datetime,
    use_local_parquet: bool = False,
    canonical_ids: Optional[List[str]] = None,
) -> Optional[pd.DataFrame]:
    """Load player_profile snapshots covering the training window.

    Returns a DataFrame with ``canonical_id``, ``snapshot_dtm``, and all
    Phase 1 profile feature columns, or ``None`` if data is unavailable.

    The caller should pass the returned DataFrame to ``process_chunk`` via its
    ``profile_df`` parameter.  ``join_player_profile`` handles the
    PIT/as-of alignment per bet.

    Parameters
    ----------
    window_start:
        Earliest chunk window_start in the run.  Snapshots from
        window_start - 365 days are included so that longer lookback windows
        (e.g. sessions_365d) have data at the start of the training range.
    window_end:
        Latest chunk window_end in the run.  Snapshots up to window_end are
        included.
    use_local_parquet:
        If True, reads from ``data/player_profile.parquet``
        instead of ClickHouse.
    canonical_ids:
        R82: optional list of canonical_id values to filter the profile table.
        Pass the full set of rated player IDs from canonical_map to cap memory
        usage; None loads all players in the time window.
    """
    profile_path = LOCAL_PARQUET_DIR / "player_profile.parquet"

    if use_local_parquet:
        if profile_path.exists():
            logger.info("Loading player_profile from local Parquet: %s", profile_path)
            try:
                from datetime import timedelta as _td
                snap_lo = window_start - _td(days=365)
                snap_hi = window_end

                def _naive(dt: datetime) -> pd.Timestamp:
                    ts = pd.Timestamp(dt)
                    return ts.tz_localize(None) if ts.tzinfo is None else ts.replace(tzinfo=None)

                df = pd.read_parquet(
                    profile_path,
                    filters=[
                        ("snapshot_dtm", ">=", _naive(snap_lo)),
                        ("snapshot_dtm", "<=", _naive(snap_hi)),
                    ],
                )
                # R82: filter to known canonical_ids to limit memory footprint
                if canonical_ids is not None and not df.empty:
                    df = df[df["canonical_id"].astype(str).isin(set(str(c) for c in canonical_ids))]
                logger.info("player_profile: %d rows loaded from local Parquet", len(df))
                return df
            except Exception as exc:
                logger.warning("player_profile local Parquet load failed: %s", exc)
                return None
        logger.info(
            "player_profile: %s not found; profile features will be 0", profile_path
        )
        return None

    # ClickHouse path
    try:
        client = get_clickhouse_client()
        from datetime import timedelta as _td

        snap_lo = window_start - _td(days=365)
        profile_cols_sql = ", ".join(PROFILE_FEATURE_COLS)
        # R82: push canonical_id IN filter to ClickHouse when provided so only
        # rated-player rows are fetched, capping memory to ~O(rated_players).
        _cid_clause = ""
        _params: dict = {"snap_lo": snap_lo, "snap_hi": window_end}
        if canonical_ids:
            _cid_clause = "AND canonical_id IN %(canonical_ids)s"
            _params["canonical_ids"] = list(canonical_ids)
        query = f"""
            SELECT
                canonical_id,
                snapshot_dtm,
                {profile_cols_sql}
            FROM {SOURCE_DB}.{TPROFILE}
            WHERE snapshot_dtm >= %(snap_lo)s
              AND snapshot_dtm <= %(snap_hi)s
              {_cid_clause}
            ORDER BY canonical_id, snapshot_dtm
        """
        df = client.query_df(query, parameters=_params)
        logger.info("player_profile: %d rows loaded from ClickHouse", len(df))
        return df if not df.empty else None
    except Exception as exc:
        logger.warning(
            "player_profile ClickHouse load failed (%s); profile features will be 0", exc
        )
        return None


def _parse_obj_to_date(v: Any) -> Optional[date]:
    """Best-effort parse for Parquet stats values (date/datetime/str)."""
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            return v.astimezone(HK_TZ).date()
        return v.date()
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


def _parquet_date_range(path: Path, candidate_cols: List[str]) -> Optional[Tuple[date, date]]:
    """Read min/max date from Parquet metadata stats without full table scan."""
    if not path.exists():
        return None
    try:
        import pyarrow.parquet as pq  # local import: optional runtime dependency

        pf = pq.ParquetFile(path)
        cols = pf.schema_arrow.names
        for col in candidate_cols:
            if col not in cols:
                continue
            col_idx = cols.index(col)
            mins: List[date] = []
            maxs: List[date] = []
            for i in range(pf.metadata.num_row_groups):
                stats = pf.metadata.row_group(i).column(col_idx).statistics
                if stats is None or not getattr(stats, "has_min_max", False):
                    continue
                dmin = _parse_obj_to_date(stats.min)
                dmax = _parse_obj_to_date(stats.max)
                if dmin is not None:
                    mins.append(dmin)
                if dmax is not None:
                    maxs.append(dmax)
            if mins and maxs:
                return min(mins), max(maxs)
    except Exception as exc:
        logger.warning("Failed to read parquet metadata date range (%s): %s", path, exc)
    return None


def _detect_local_data_end() -> Optional[date]:
    """Detect the latest available date from local bet & session Parquet metadata.

    Uses row-group statistics only (no data scan). Returns the conservative
    (min) of the two max dates so both tables have data up to the returned
    date. Returns None if metadata is unavailable for both.
    """
    bet_path = LOCAL_PARQUET_DIR / "gmwds_t_bet.parquet"
    sess_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"

    bet_rng = _parquet_date_range(bet_path, ["payout_complete_dtm", "gaming_day"])
    sess_rng = _parquet_date_range(
        sess_path, ["gaming_day", "session_end_dtm", "lud_dtm", "session_start_dtm"]
    )

    maxes: List[date] = []
    if bet_rng is not None:
        maxes.append(bet_rng[1])
    if sess_rng is not None:
        maxes.append(sess_rng[1])

    if not maxes:
        return None
    return min(maxes)


def _month_end_dates(start_date: date, end_date: date) -> List[date]:
    """Return the last calendar day of each month in [start_date, end_date].

    Used by DEC-019 to build a month-end profile snapshot schedule.
    At most one snapshot per month is produced; the PIT join in
    join_player_profile uses the most-recent snapshot <= bet_time,
    so bets mid-month will fall back to the previous month-end snapshot.

    Parameters
    ----------
    start_date, end_date:
        Inclusive date range.  Both must use HK-calendar dates.

    Returns
    -------
    Sorted list of date objects, each being the last day of its month,
    filtered to [start_date, end_date].
    """
    result: List[date] = []
    year, month = start_date.year, start_date.month
    while True:
        last_day = calendar.monthrange(year, month)[1]
        month_end = date(year, month, last_day)
        if month_end > end_date:
            break
        if month_end >= start_date:
            result.append(month_end)
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return result


def _latest_month_end_on_or_before(ref_date: date) -> date:
    """Return the nearest month-end date that is <= ref_date."""
    year, month = ref_date.year, ref_date.month
    month_last = calendar.monthrange(year, month)[1]
    cand = date(year, month, month_last)
    if cand <= ref_date:
        return cand
    # Previous month-end.
    if month == 1:
        year -= 1
        month = 12
    else:
        month -= 1
    prev_last = calendar.monthrange(year, month)[1]
    return date(year, month, prev_last)


def ensure_player_profile_ready(
    window_start: datetime,
    window_end: datetime,
    use_local_parquet: bool = False,
    canonical_id_whitelist: Optional[set] = None,
    snapshot_interval_days: int = 1,
    preload_sessions: bool = True,
    canonical_map: Optional[pd.DataFrame] = None,
    fast_mode: bool = False,
    max_lookback_days: int = 365,
    use_month_end_snapshots: bool = True,
) -> None:
    """Auto-check profile table freshness and rebuild missing local ranges if needed.

    Local-parquet training mode only:
      1) determine required snapshot window for PIT join,
      2) compare against existing player_profile coverage,
      3) auto-run helper script to backfill missing range(s).

    Parameters
    ----------
    canonical_id_whitelist:
        When provided (fast-mode), passed to ``backfill`` to restrict
        profiling to the sampled rated player set.  Also triggers
        in-process backfill (avoids subprocess overhead and allows
        the whitelist to be passed directly).
    snapshot_interval_days:
        Deprecated for scheduling.  Month-end scheduling is now enforced in all
        modes.  This value is still forwarded for backward compatibility, but
        it does not control snapshot date selection.
    preload_sessions:
        Forwarded to ``backfill``.  Set False (--fast-mode-no-preload) to
        disable full-table session preload, using per-day PyArrow pushdown
        reads instead.  Reduces peak RAM at the cost of more disk I/O.
    canonical_map:
        Pre-built player_id -> canonical_id mapping DataFrame from
        trainer.py.  Forwarded to ``backfill`` so the ETL does not
        redundantly search for ``canonical_mapping.parquet`` on disk
        (DEC-017 bug fix — eliminates the
        ``No local canonical_mapping.parquet`` warning).
    use_month_end_snapshots:
        Deprecated override flag.  Month-end scheduling is now always enabled
        (including fast-mode) to keep profile update cadence stable.
    """
    if not use_local_parquet:
        # ClickHouse mode: schema version is not auto-checked; if PROFILE_FEATURE_COLS
        # or _SESSION_COLS change, a manual TRUNCATE / re-population is required.
        logger.info("Profile auto-build skipped (ClickHouse mode).")
        return

    profile_path = LOCAL_PARQUET_DIR / "player_profile.parquet"
    session_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    auto_script = BASE_DIR / "scripts" / "auto_build_player_profile.py"
    # Force a single scheduling policy across all execution modes/options:
    # player_profile snapshots are always month-end.
    effective_month_end = True

    # --- Schema-hash check ---------------------------------------------------
    # Compare the current profile schema fingerprint (PROFILE_VERSION +
    # PROFILE_FEATURE_COLS + _SESSION_COLS) against the sidecar written when
    # the parquet was last built.  A mismatch means features changed and the
    # entire cached parquet must be discarded before the date-range check runs.
    if profile_path.exists():
        current_hash = compute_profile_schema_hash()
        # R106: add population-mode indicator so fast/normal caches do not mix.
        # R200: also include max_lookback_days so that a profile cache built with
        # horizon=30 (fast-mode) is not reused by normal-mode (horizon=365).
        _pop_tag = (
            f"_whitelist={len(canonical_id_whitelist)}"
            if canonical_id_whitelist
            else "_full"
        )
        _horizon_tag = f"_mlb={max_lookback_days}"
        # DEC-019 R601: include schedule mode so month-end and daily caches never collide.
        _sched_tag = "_month_end" if effective_month_end else "_daily"
        current_hash = hashlib.md5(
            (current_hash + _pop_tag + _horizon_tag + _sched_tag).encode()
        ).hexdigest()
        stored_hash: Optional[str] = None
        if LOCAL_PROFILE_SCHEMA_HASH.exists():
            try:
                stored_hash = LOCAL_PROFILE_SCHEMA_HASH.read_text(encoding="utf-8").strip()
            except OSError:
                stored_hash = None

        if stored_hash != current_hash:
            logger.warning(
                "player_profile schema has changed "
                "(stored=%s, current=%s). "
                "Deleting stale cache and checkpoint — full rebuild required.",
                stored_hash or "<missing>",
                current_hash,
            )
            try:
                profile_path.unlink()
                logger.info("Deleted stale player_profile.parquet")
            except OSError as exc:
                logger.error("Could not delete stale profile parquet: %s", exc)
            try:
                LOCAL_PROFILE_SCHEMA_HASH.unlink(missing_ok=True)
            except OSError:
                pass
            # Also remove the ETL checkpoint so auto_build restarts from scratch.
            checkpoint_path = LOCAL_PARQUET_DIR / "player_profile_etl_checkpoint.json"
            if checkpoint_path.exists():
                try:
                    checkpoint_path.unlink()
                    logger.info("Deleted stale ETL checkpoint")
                except OSError as exc:
                    logger.warning("Could not delete stale ETL checkpoint: %s", exc)
        else:
            logger.debug("player_profile schema fingerprint matches (%s).", current_hash)
    # -------------------------------------------------------------------------

    if not session_path.exists():
        logger.warning("Session parquet missing at %s; skip profile auto-build", session_path)
        return

    # DEC-017: fast-mode restricts the profile snapshot range to the effective
    # data window — no 365-day lookback push-back.  Normal mode still requests
    # 365 days of snapshots so that 365d-window features have data available.
    if fast_mode:
        required_start = window_start.date()
    else:
        required_start = (window_start - timedelta(days=365)).date()
    required_end = window_end.date()

    session_rng = _parquet_date_range(
        session_path,
        ["gaming_day", "session_end_dtm", "lud_dtm", "session_start_dtm"],
    )
    if session_rng:
        required_start = max(required_start, session_rng[0])
        required_end = min(required_end, session_rng[1])

    if required_start > required_end:
        logger.warning(
            "Profile auto-build skipped: effective required range is empty (%s > %s)",
            required_start,
            required_end,
        )
        return

    profile_rng = _parquet_date_range(profile_path, ["snapshot_date", "snapshot_dtm"])
    missing_ranges: List[Tuple[date, date]] = []
    if profile_rng is None:
        missing_ranges.append((required_start, required_end))
    else:
        prof_start, prof_end = profile_rng
        if prof_start > required_start:
            missing_ranges.append((required_start, prof_start - timedelta(days=1)))
        if prof_end < required_end:
            missing_ranges.append((prof_end + timedelta(days=1), required_end))

    if not missing_ranges:
        logger.info(
            "player_profile is up-to-date for training window (%s -> %s).",
            required_start,
            required_end,
        )
        return

    for miss_start, miss_end in missing_ranges:
        if miss_start > miss_end:
            continue
        logger.info(
            "player_profile missing range %s -> %s; auto-building before training.",
            miss_start,
            miss_end,
        )
        _backfill_start, _backfill_end = miss_start, miss_end
        # Enforced month-end schedule (all modes): build only month-end snapshots.
        _snap_dates = _month_end_dates(miss_start, miss_end) if effective_month_end else None
        # If the missing range is intra-month (no month-end within range), anchor
        # PIT with the most recent month-end on/before miss_end.
        if _snap_dates is not None and len(_snap_dates) == 0:
            _anchor = _latest_month_end_on_or_before(miss_end)
            _snap_dates = [_anchor]
            _backfill_start = min(_backfill_start, _anchor)
            logger.info(
                "Month-end-only schedule: intra-month missing range %s -> %s; "
                "building anchor snapshot at %s.",
                miss_start, miss_end, _anchor,
            )

        # Use in-process backfill when any of:
        # (a) fast-mode: whitelist or interval != 1 — avoids subprocess overhead
        #     and allows whitelist / snapshot_interval_days to be forwarded
        #     directly without CLI serialisation.
        # (b) canonical_map already in memory (DEC-017 R120 fix) — a subprocess
        #     cannot receive a Python DataFrame object, so in-process is the
        #     only path that can forward the pre-built map.  Without this,
        #     normal-mode local-parquet backfill would still trigger the
        #     "No local canonical_mapping.parquet" warning.
        # (c) DEC-019: snapshot_dates is provided (in-process required to pass
        #     the date list directly without CLI serialisation).
        use_inprocess = (
            canonical_map is not None
            or canonical_id_whitelist is not None
            or snapshot_interval_days != 1
            or _snap_dates is not None
        )
        if use_inprocess:
            try:
                _etl_backfill(
                    _backfill_start,
                    _backfill_end,
                    use_local_parquet=True,
                    canonical_id_whitelist=canonical_id_whitelist,
                    snapshot_interval_days=snapshot_interval_days,
                    preload_sessions=preload_sessions,
                    canonical_map=canonical_map,
                    max_lookback_days=max_lookback_days,
                    snapshot_dates=_snap_dates,
                )
                _sched_desc = (
                    f"month-end ({len(_snap_dates)} dates)" if _snap_dates is not None
                    else f"interval={snapshot_interval_days}"
                )
                logger.info(
                    "In-process profile build completed for %s -> %s "
                    "(whitelist=%s, schedule=%s)",
                    _backfill_start, _backfill_end,
                    f"{len(canonical_id_whitelist)} IDs" if canonical_id_whitelist else "none",
                    _sched_desc,
                )
            except Exception as _exc:
                logger.warning(
                    "In-process profile build failed for %s -> %s: %s",
                    _backfill_start, _backfill_end, _exc,
                )
        else:
            # R105: auto_script check only for subprocess path; fast-mode uses
            # in-process backfill and does not need the script.
            if not auto_script.exists():
                logger.warning(
                    "Auto profile builder script missing at %s; skip this range",
                    auto_script,
                )
                continue
            cmd = [
                sys.executable,
                str(auto_script),
                "--local-parquet",
                "--start-date",
                miss_start.isoformat(),
                "--end-date",
                miss_end.isoformat(),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                logger.warning(
                    "Auto profile build failed for %s -> %s (rc=%s). stderr tail:\n%s",
                    miss_start,
                    miss_end,
                    proc.returncode,
                    "\n".join([ln for ln in proc.stderr.splitlines() if ln.strip()][-40:]),
                )
            else:
                logger.info("Auto profile build completed for %s -> %s", miss_start, miss_end)

    # Final coverage check after auto-build attempt.
    # R111: when snapshot_interval_days > 1 or use_month_end_snapshots, date gaps
    # are expected; only warn if coverage is truly insufficient.
    # DEC-019: month-end snapshots allow gaps up to ~31 days.
    _effective_interval = 31 if effective_month_end else snapshot_interval_days
    profile_rng_after = _parquet_date_range(profile_path, ["snapshot_date", "snapshot_dtm"])
    if profile_rng_after is None:
        logger.warning(
            "player_profile still unavailable after auto-build. "
            "Training will continue with profile features as NaN."
        )
        return
    after_start, after_end = profile_rng_after
    if _effective_interval > 1:
        if after_end < required_end - timedelta(days=_effective_interval):
            logger.warning(
                "player_profile coverage still partial after auto-build. "
                "required=%s->%s, have=%s->%s. Training continues with partial profile coverage.",
                required_start,
                required_end,
                after_start,
                after_end,
            )
        else:
            _sched_label = "month-end" if effective_month_end else f"interval={snapshot_interval_days}"
            logger.info(
                "player_profile coverage acceptable (%s).", _sched_label,
            )
    elif after_start > required_start or after_end < required_end:
        logger.warning(
            "player_profile coverage still partial after auto-build. "
            "required=%s->%s, have=%s->%s. Training continues with partial profile coverage.",
            required_start,
            required_end,
            after_start,
            after_end,
        )
    else:
        logger.info("player_profile coverage validated after auto-build.")


# ---------------------------------------------------------------------------
# DQ & preprocessing
# ---------------------------------------------------------------------------

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
        Track-B state machines cross-chunk context.  Defaults to window_start.

    Notes
    -----
    When ``bets`` is empty (e.g. sessions-only DQ path used when building the
    canonical mapping), the bets processing block is skipped entirely and only
    session DQ filters are applied.  This avoids a ``KeyError`` on
    ``payout_complete_dtm`` when a caller passes a stub DataFrame.
    """
    # --- sessions (FND-01 / FND-02 / FND-04) — applied first so that the
    # bets.empty early-return path still yields clean session data.
    sessions = sessions.copy()
    for dt_col in ("session_start_dtm", "session_end_dtm", "lud_dtm"):
        if dt_col in sessions.columns:
            sessions[dt_col] = pd.to_datetime(sessions[dt_col], utc=False, errors="coerce")

    for col in ("session_id", "player_id"):
        sessions[col] = pd.to_numeric(sessions.get(col), errors="coerce")
    sessions = sessions.dropna(subset=["session_id"]).copy()

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

    # FND-02: exclude manual adjustment sessions and soft-deleted rows
    sessions = sessions[
        (sessions["is_manual"] == 0)
        & (sessions["is_deleted"] == 0)
        & (sessions["is_canceled"] == 0)
    ].copy()

    # FND-04: exclude ghost sessions with no real wager activity (SSOT §5).
    # Guard: only apply when at least one activity column is present.
    if "turnover" in sessions.columns or "num_games_with_wager" in sessions.columns:
        _turnover = sessions.get(
            "turnover", pd.Series(0.0, index=sessions.index)
        ).fillna(0)
        _games = sessions["num_games_with_wager"].fillna(0)
        sessions = sessions[(_turnover > 0) | (_games > 0)].copy()

    bets = bets.copy()
    if bets.empty:
        # Sessions-only path — return clean sessions, skip bets processing entirely.
        # This avoids a KeyError on payout_complete_dtm when called with a stub DataFrame.
        return bets, sessions

    # --- bets ---
    bets["payout_complete_dtm"] = pd.to_datetime(bets["payout_complete_dtm"], utc=False)

    # R23: Timezone normalisation — tz_localize naive, tz_convert aware to HK,
    # then strip tz so downstream callers (labels, features) receive tz-naive
    # HK local time and no naive/aware TypeError can occur at the boundary.
    if bets["payout_complete_dtm"].dt.tz is None:
        bets["payout_complete_dtm"] = bets["payout_complete_dtm"].dt.tz_localize(
            HK_TZ, nonexistent="shift_forward", ambiguous="NaT"
        )
    else:
        bets["payout_complete_dtm"] = bets["payout_complete_dtm"].dt.tz_convert(HK_TZ)
    # Strip tz after normalization — downstream (compute_labels, features) is tz-naive.
    bets["payout_complete_dtm"] = bets["payout_complete_dtm"].dt.tz_localize(None)
    # DEC-018: unify datetime resolution to ns so merge_asof / comparisons always see
    # the same dtype regardless of Parquet file's stored precision ([ms] vs [us]).
    bets["payout_complete_dtm"] = bets["payout_complete_dtm"].astype("datetime64[ns]")

    # Boundary comparison — both sides are tz-naive after DEC-018 process_chunk strip.
    # The explicit .replace(tzinfo=None) guards here are kept as a defensive fallback
    # for callers that bypass process_chunk (e.g. backtester, tests).
    _lo = bets_history_start if bets_history_start is not None else window_start
    _lo = _lo.replace(tzinfo=None) if getattr(_lo, "tzinfo", None) else _lo
    _hi = extended_end.replace(tzinfo=None) if getattr(extended_end, "tzinfo", None) else extended_end

    bets = bets[
        bets["payout_complete_dtm"].between(_lo, _hi, inclusive="left")
        & bets["payout_complete_dtm"].notna()
    ].copy()

    # Defense-in-depth wager guard (R1602): upstream SQL already filters wager>0,
    # but apply_dq is also called directly (e.g. by backtester tests) so we
    # enforce it here when the column is present.  Uses .gt() to avoid
    # ambiguity with NA comparison (R1706 alignment).
    if "wager" in bets.columns:
        bets = bets[bets["wager"].fillna(0).gt(0)].copy()

    for col in ("bet_id", "session_id", "player_id", "table_id"):
        bets[col] = pd.to_numeric(bets.get(col), errors="coerce")
    bets = bets.dropna(subset=["bet_id", "session_id"]).copy()

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
        ].copy()

    # Ensure gaming_day exists (fallback: date of payout)
    if "gaming_day" not in bets.columns:
        bets["gaming_day"] = pd.to_datetime(bets["payout_complete_dtm"]).dt.date

    # Ensure status column exists (for loss_streak)
    if "status" not in bets.columns:
        bets["status"] = None

    # Numeric guard for legacy features
    for col in ("wager", "payout_odds", "base_ha", "is_back_bet", "position_idx"):
        if col in bets.columns:
            bets[col] = pd.to_numeric(bets[col], errors="coerce").fillna(0)

    # DEC-018 / R23 contract assertion: payout_complete_dtm must leave apply_dq tz-naive.
    if not bets.empty and "payout_complete_dtm" in bets.columns:
        assert bets["payout_complete_dtm"].dt.tz is None, \
            "R23 violation: payout_complete_dtm must be tz-naive after DQ"

    return bets, sessions


# ---------------------------------------------------------------------------
# Track-B feature computation
# ---------------------------------------------------------------------------

def add_track_b_features(
    bets: pd.DataFrame,
    canonical_map: pd.DataFrame,
    window_end: datetime,
) -> pd.DataFrame:
    """Attach Track-B features to bets.  Requires canonical_id column."""
    if "canonical_id" not in bets.columns:
        logger.warning("canonical_id missing; Track-B features will be zeros")
        bets["loss_streak"] = 0
        bets["run_id"] = 0
        bets["minutes_since_run_start"] = 0.0
        return bets

    df = bets.copy()

    # loss_streak (cutoff = window_end so future bets don't influence streak)
    streak = compute_loss_streak(df, cutoff_time=window_end)
    df["loss_streak"] = streak.reindex(df.index, fill_value=0)

    # run_boundary (cutoff = window_end)
    run_df = compute_run_boundary(df, cutoff_time=window_end)
    run_df = run_df.set_index(run_df.index)  # keep original index
    df["run_id"] = run_df.get("run_id", pd.Series(0, index=df.index))
    df["minutes_since_run_start"] = run_df.get(
        "minutes_since_run_start", pd.Series(0.0, index=df.index)
    )

    return df


# ---------------------------------------------------------------------------
# Legacy features (session-based aggregates — kept for parity with old scorer)
# ---------------------------------------------------------------------------

def add_legacy_features(
    bets: pd.DataFrame,
    sessions: pd.DataFrame,
) -> pd.DataFrame:
    """Compute legacy session-level aggregates merged into bets.

    These mirror the features used by the pre-Phase-1 scorer so that the
    legacy scorer path can keep running until Step 7 refactors it.
    """
    sess = sessions[
        [c for c in ("session_id", "session_start_dtm", "session_end_dtm") if c in sessions.columns]
    ].drop_duplicates(subset=["session_id"], keep="last").copy()

    df = bets.merge(sess, on="session_id", how="left", validate="many_to_one")

    # session_start_dtm availability guard
    if "session_start_dtm" not in df.columns:
        df["session_start_dtm"] = pd.NaT

    df["cum_bets"] = df.groupby("session_id").cumcount() + 1
    df["cum_wager"] = df.groupby("session_id")["wager"].cumsum().fillna(0)
    df["avg_wager_sofar"] = (df["cum_wager"] / df["cum_bets"]).fillna(0)

    # Cyclic time-of-day encoding
    min_into_day = (
        df["payout_complete_dtm"].dt.hour * 60 + df["payout_complete_dtm"].dt.minute
    )
    df["time_of_day_sin"] = np.sin(2 * np.pi * min_into_day / 1440)
    df["time_of_day_cos"] = np.cos(2 * np.pi * min_into_day / 1440)

    for col in LEGACY_FEATURE_COLS:
        if col in df.columns:
            df[col] = df[col].fillna(0)

    return df


# ---------------------------------------------------------------------------
# Chunk processing
# ---------------------------------------------------------------------------

def _chunk_parquet_path(chunk: dict) -> Path:
    ws = chunk["window_start"].strftime("%Y%m%d")
    we = chunk["window_end"].strftime("%Y%m%d")
    return CHUNK_DIR / f"chunk_{ws}_{we}.parquet"


def _chunk_cache_key(
    chunk: dict,
    bets: pd.DataFrame,
    profile_hash: str = "none",
    no_afg: bool = False,
) -> str:
    """Hash to detect stale parquet cache (TRN-07).

    Includes a config-constants hash (R71) so that changes to
    WALKAWAY_GAP_MIN, SESSION_AVAIL_DELAY_MIN, or HISTORY_BUFFER_DAYS
    automatically invalidate all cached chunk Parquets.

    R77: profile_hash encodes the shape/content of player_profile so that
    changes to the snapshot table also invalidate the chunk cache.

    R904/R1003: no_afg is included so that toggling --no-afg produces a distinct
    cache key, preventing stale Track-LLM-enabled chunks from being reused when AFG
    is turned off (or vice versa).
    """
    ws = chunk["window_start"].isoformat()
    we = chunk["window_end"].isoformat()
    data_hash = hashlib.md5(
        pd.util.hash_pandas_object(bets, index=False).values.tobytes()
    ).hexdigest()[:8]
    cfg_str = json.dumps({
        "WALKAWAY_GAP_MIN": WALKAWAY_GAP_MIN,
        "SESSION_AVAIL_DELAY_MIN": SESSION_AVAIL_DELAY_MIN,
        "HISTORY_BUFFER_DAYS": HISTORY_BUFFER_DAYS,
    }, sort_keys=True)
    cfg_hash = hashlib.md5(cfg_str.encode()).hexdigest()[:6]
    afg_tag = "no_afg" if no_afg else "afg"
    return f"{ws}|{we}|{data_hash}|{cfg_hash}|{profile_hash}|{afg_tag}"


def process_chunk(
    chunk: dict,
    canonical_map: pd.DataFrame,
    dummy_player_ids: Optional[set] = None,
    use_local_parquet: bool = False,
    force_recompute: bool = False,
    profile_df: Optional[pd.DataFrame] = None,
    feature_spec: Optional[dict] = None,
    no_afg: bool = False,
) -> Optional[Path]:
    """Process one monthly chunk; return path to written Parquet or None if empty.

    The canonical_map is built once at the global level (cutoff = training end)
    and passed in here.  Phase 2 should use per-chunk PIT mapping.
    dummy_player_ids: FND-12 dummy/fake-account player_ids to drop from training (TRN-04).
    profile_df: player_profile snapshot table for PIT join (PLAN Step 4/DEC-011).
        Pass None to skip; profile feature columns will be 0 for all rows.
    feature_spec: parsed Track LLM feature spec loaded by run_pipeline.
    no_afg: when True, skip Track LLM feature computation (DEC-020 --no-afg / --fast-mode).
    """
    window_start = chunk["window_start"]
    window_end = chunk["window_end"]
    extended_end = chunk["extended_end"]

    # DEC-018: pipeline interior is uniformly tz-naive HK local time.
    # time_fold produces tz-aware bounds; strip here so all downstream callers
    # (apply_dq, compute_labels, add_track_b_features, label filter) receive
    # tz-naive datetimes matching the tz-naive data columns from apply_dq R23.
    window_start = window_start.replace(tzinfo=None) if window_start.tzinfo else window_start
    window_end   = window_end.replace(tzinfo=None)   if window_end.tzinfo   else window_end
    extended_end = extended_end.replace(tzinfo=None)  if extended_end.tzinfo  else extended_end
    # Guard: all three boundaries must be tz-naive inside process_chunk.
    for _bname, _bval in (("window_start", window_start), ("window_end", window_end), ("extended_end", extended_end)):
        assert getattr(_bval, "tzinfo", None) is None, \
            f"DEC-018: {_bname} must be tz-naive inside process_chunk (got {_bval!r})"

    chunk_path = _chunk_parquet_path(chunk)

    # --- Load data ---
    if use_local_parquet:
        bets_raw, sessions_raw = load_local_parquet(window_start, extended_end)
    else:
        bets_raw, sessions_raw = load_clickhouse_data(window_start, extended_end)

    if bets_raw.empty:
        logger.warning("Chunk %s–%s: no bets, skipping", window_start.date(), window_end.date())
        return None

    # --- TRN-07: cache validity via content hash ---
    # Compute the cache key from chunk metadata + raw bets hash so that DQ rule
    # or config changes (which alter bets_raw content) automatically invalidate
    # the cached Parquet even when force_recompute=False.
    # R77: include profile snapshot shape/col list so profile table changes also
    # bust the cache.
    _profile_hash: str
    if profile_df is not None and not profile_df.empty:
        _profile_cols_key = "|".join(sorted(profile_df.columns.tolist()))
        _profile_hash = hashlib.md5(
            f"{len(profile_df)}:{_profile_cols_key}".encode()
        ).hexdigest()[:6]
    else:
        _profile_hash = "none"
    current_key = _chunk_cache_key(chunk, bets_raw, profile_hash=_profile_hash, no_afg=no_afg)
    key_path = chunk_path.with_suffix(".cache_key")
    if not force_recompute and chunk_path.exists():
        stored_key = key_path.read_text(encoding="utf-8").strip() if key_path.exists() else ""
        if stored_key == current_key:
            try:
                cached = pd.read_parquet(chunk_path)
                logger.info(
                    "Chunk %s–%s: cache hit (%d rows, key=%s)",
                    window_start.date(), window_end.date(), len(cached), current_key,
                )
                return chunk_path
            except Exception:
                logger.warning(
                    "Chunk %s–%s: cache corrupt, recomputing", window_start.date(), window_end.date()
                )
        else:
            logger.info(
                "Chunk %s–%s: cache stale (key mismatch), recomputing", window_start.date(), window_end.date()
            )

    # --- DQ --- (bets_history_start pulls HISTORY_BUFFER_DAYS of extra context for Track-B)
    history_start = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
    bets, sessions = apply_dq(
        bets_raw, sessions_raw, window_start, extended_end,
        bets_history_start=history_start,
    )
    if bets.empty:
        logger.warning("Chunk %s–%s: empty after DQ", window_start.date(), window_end.date())
        return None

    # --- TRN-04: drop FND-12 dummy/fake-account rows before feature engineering ---
    if dummy_player_ids and "player_id" in bets.columns:
        before = len(bets)
        bets = bets[~bets["player_id"].isin(dummy_player_ids)].copy()
        if len(bets) < before:
            logger.info("Chunk %s–%s: dropped %d dummy player_id rows (FND-12)", window_start.date(), window_end.date(), before - len(bets))
        if bets.empty:
            logger.warning("Chunk %s–%s: empty after FND-12 filter", window_start.date(), window_end.date())
            return None

    # --- Identity: attach canonical_id ---
    if not canonical_map.empty and "player_id" in canonical_map.columns:
        bets = bets.merge(
            canonical_map[["player_id", "canonical_id"]].drop_duplicates("player_id"),
            on="player_id",
            how="left",
        )
    else:
        bets["canonical_id"] = bets["player_id"].astype(str)

    # R27: Fallback — rows absent from canonical mapping keep their player_id as canonical_id.
    # Without this, left-merge NaNs would be dropped by labels.compute_labels, losing
    # all anonymous (non-rated) players from training data.
    bets["canonical_id"] = bets["canonical_id"].fillna(bets["player_id"].astype(str))

    # --- Track-B features (on FULL bets incl. history, cutoff=window_end) ---
    # Computing before label filtering ensures cross-chunk state (loss_streak,
    # run_boundary) uses historical context from HISTORY_BUFFER_DAYS before window_start.
    bets = add_track_b_features(bets, canonical_map, window_end)

    # --- Track LLM: DuckDB + Feature Spec YAML (DEC-022/023/024) ---
    # R3500: compute on the FULL bets DataFrame (with HISTORY_BUFFER_DAYS context)
    # BEFORE label filtering so window features see the same history as the scorer
    # (train-serve parity).  The result is merged back onto bets by bet_id so that
    # compute_labels still receives the extended-zone rows it needs for right-censoring.
    _bets_llm_feature_cols: list = []
    if not no_afg and feature_spec is not None:
        try:
            _t0_llm = time.perf_counter()
            _bets_llm_result = compute_track_llm_features(
                bets,
                feature_spec=feature_spec,
                cutoff_time=window_end,
            )
            _llm_cand_ids = [
                c.get("feature_id")
                for c in (feature_spec.get("track_llm") or {}).get("candidates", [])
            ]
            _bets_llm_feature_cols = [
                fid for fid in _llm_cand_ids
                if fid and fid in _bets_llm_result.columns
            ]
            if _bets_llm_feature_cols and "bet_id" in _bets_llm_result.columns:
                bets = bets.merge(
                    _bets_llm_result[["bet_id"] + _bets_llm_feature_cols].drop_duplicates("bet_id"),
                    on="bet_id",
                    how="left",
                )
            logger.info(
                "Chunk %s–%s: Track LLM computed (%.1fs)",
                window_start.date(),
                window_end.date(),
                time.perf_counter() - _t0_llm,
            )
        except Exception as exc:
            logger.error(
                "Chunk %s–%s: Track LLM failed — %s",
                window_start.date(),
                window_end.date(),
                exc,
            )

    # --- Labels (C1 extended pull) ---
    labeled = compute_labels(
        bets_df=bets,
        window_end=window_end,
        extended_end=extended_end,
    )
    # H1: drop censored terminal bets — they cannot be reliably labelled
    labeled = labeled[~labeled["censored"]].copy()

    # Filter to training window — exclude historical context rows AND extended zone.
    # Both sides are tz-naive after DEC-018 strip at process_chunk() entry.
    labeled = labeled[
        (labeled["payout_complete_dtm"] >= window_start)
        & (labeled["payout_complete_dtm"] < window_end)
    ].copy()
    if labeled.empty:
        logger.warning("Chunk %s–%s: empty after label filtering", window_start.date(), window_end.date())
        return None

    # --- player_profile PIT join (PLAN Step 4 / DEC-011) ---
    # Attaches Rated-player profile features via as-of merge (snapshot_dtm <= bet_time).
    # Non-rated bets and bets without a prior snapshot receive 0 for all profile columns.
    labeled = join_player_profile(labeled, profile_df)

    # --- Legacy (Track B) features ---
    labeled = add_legacy_features(labeled, sessions)

    # Ensure all non-profile feature columns exist with numeric defaults.
    # R74: profile columns are intentionally left as NaN when a player has no
    # prior snapshot — LightGBM routes them to the trained default-child.
    # Blanket fillna(0) across ALL_FEATURE_COLS would erase that signal.
    _non_profile_cols = [c for c in ALL_FEATURE_COLS if c not in PROFILE_FEATURE_COLS]
    for col in _non_profile_cols:
        if col not in labeled.columns:
            labeled[col] = 0
    labeled[_non_profile_cols] = labeled[_non_profile_cols].fillna(0)

    # Mark rated/non-rated (H3: identity.build_canonical_mapping* only builds entries
    # for players who have a valid casino_player_id, so every canonical_id in the
    # mapping is by definition a rated player.  Checking for a non-existent
    # "casino_player_id" column was always False and caused Rated model to receive
    # zero training rows (R36).
    rated_ids: set = (
        set(canonical_map["canonical_id"].unique()) if not canonical_map.empty else set()
    )
    labeled["is_rated"] = labeled["canonical_id"].isin(rated_ids)

    logger.info(
        "Chunk %s–%s: %d rows (label=1: %d, rated: %d)",
        window_start.date(), window_end.date(),
        len(labeled),
        int(labeled["label"].sum()),
        int(labeled["is_rated"].sum()),
    )

    labeled.to_parquet(chunk_path, index=False)
    # Persist the cache key so future runs can detect stale data (TRN-07)
    key_path.write_text(current_key, encoding="utf-8")
    return chunk_path


# ---------------------------------------------------------------------------
# Run-level sample weights (SSOT §9.3, DEC-013)
# ---------------------------------------------------------------------------

def compute_sample_weights(df: pd.DataFrame) -> pd.Series:
    """Return sample_weight = 1 / N_run for each row.

    N_run = number of bets in the same run (same canonical_id, same run_id from
    compute_run_boundary) in ``df``.  Corrects length bias: long runs would
    otherwise dominate the loss compared to short runs.
    Only call this on the TRAINING set; never on valid/test (leakage guard).
    """
    if "run_id" not in df.columns or "canonical_id" not in df.columns:
        logger.warning("Cannot compute run weights — missing canonical_id or run_id; using 1.0")
        return pd.Series(1.0, index=df.index)

    run_key = df["canonical_id"].astype(str) + "|" + df["run_id"].astype(str)
    n_run = run_key.map(run_key.value_counts())
    weights = (1.0 / n_run).fillna(1.0)
    return weights


# ---------------------------------------------------------------------------
# Optuna hyperparameter search (per model type)
# ---------------------------------------------------------------------------

def _base_lgb_params() -> dict:
    return {
        "objective": "binary",
        "class_weight": "balanced",
        "force_col_wise": True,
        "verbose": -1,
        "n_jobs": -1,
        "random_state": 42,
    }


def run_optuna_search(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    n_trials: int = OPTUNA_N_TRIALS,
    label: str = "",
) -> dict:
    """TPE hyperparameter search.  Optimises PR-AUC on validation set."""
    # R705: guard against empty validation input — return empty dict (base params)
    # rather than crashing inside LightGBM or average_precision_score.
    if X_val.empty or len(y_val) == 0:
        logger.warning(
            "%s: empty validation set — skipping Optuna search, returning base params.",
            label or "model",
        )
        return {}
    logger.info("Optuna search (%s): %d trials", label or "model", n_trials)

    def objective(trial: optuna.Trial) -> float:
        params = {
            **_base_lgb_params(),
            "n_estimators": trial.suggest_int("n_estimators", 100, 800, step=50),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "max_depth": trial.suggest_int("max_depth", 3, 10),
            "num_leaves": trial.suggest_int("num_leaves", 15, 127),
            "min_child_samples": trial.suggest_int("min_child_samples", 10, 100),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "subsample": trial.suggest_float("subsample", 0.5, 1.0),
            "subsample_freq": 1,
            "reg_alpha": trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
        }
        model = lgb.LGBMClassifier(**params)
        model.fit(
            X_train,
            y_train,
            sample_weight=sw_train,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
        )
        scores = model.predict_proba(X_val)[:, 1]
        return average_precision_score(y_val, scores)

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    best = study.best_params
    logger.info("Optuna (%s) best PR-AUC=%.4f, params=%s", label or "model", study.best_value, best)
    return best


# ---------------------------------------------------------------------------
# Dual-model training
# ---------------------------------------------------------------------------

def _train_one_model(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    hyperparams: dict,
    label: str = "",
) -> Tuple[lgb.LGBMClassifier, dict]:
    """Train a single LightGBM model and compute validation metrics."""
    # R1509: guard single-class training set (LightGBM would train a constant predictor).
    if y_train.nunique() < 2:
        raise ValueError(
            "%s: training set has only one class (y_train.nunique()=%d); need both 0 and 1."
            % (label or "model", int(y_train.nunique()))
        )
    params = {**_base_lgb_params(), **hyperparams}
    model = lgb.LGBMClassifier(**params)

    # bug-empty-valid-test-when-few-chunks: LightGBM raises ValueError when
    # eval_set contains an empty DataFrame.  Skip eval_set + early_stopping
    # when the validation set is too small or has no positive labels.
    # R801: also guard against NaN labels — pandas sum() silently skips NaN,
    # so a y_val with mixed NaN/valid labels passes the sum() check but causes
    # sklearn precision_recall_curve to raise ValueError: Input contains NaN.
    _has_val = (
        not X_val.empty
        and len(y_val) >= MIN_VALID_TEST_ROWS
        and int(y_val.isna().sum()) == 0
        and int(y_val.sum()) >= 1
        and int((y_val == 0).sum()) >= 1
    )
    if _has_val:
        model.fit(
            X_train,
            y_train,
            sample_weight=sw_train,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
        )
    else:
        _n_pos = int(y_val.sum()) if not y_val.empty else 0
        _n_neg = int((y_val == 0).sum()) if not y_val.empty else 0
        logger.warning(
            "%s: validation set inadequate (%d rows, %d positives, %d negatives) — "
            "training without eval_set / early stopping.",
            label or "model",
            len(y_val),
            _n_pos,
            _n_neg,
        )
        model.fit(X_train, y_train, sample_weight=sw_train)

    if _has_val:
        val_scores = model.predict_proba(X_val)[:, 1]
        prauc = float(average_precision_score(y_val, val_scores)) if y_val.sum() > 0 else 0.0

        # Threshold selection: vectorised PR-curve scan (R65 — avoids O(N²) loop).
        # precision_recall_curve returns arrays aligned so that for each threshold
        # index i: preds = val_scores >= thresholds[i].  We maximise F-beta (beta=THRESHOLD_FBETA)
        # over the full threshold grid; beta < 1 favours precision over recall.
        pr_prec, pr_rec, pr_thresholds = precision_recall_curve(y_val, val_scores)
        # pr_prec / pr_rec have one extra element (last = 1/0); align with thresholds
        pr_prec = pr_prec[:-1]
        pr_rec = pr_rec[:-1]
        # Minimum-alert guard: vectorised via searchsorted (R68 — O(N log N) total)
        _sorted_scores = np.sort(val_scores)
        alert_counts = len(val_scores) - np.searchsorted(
            _sorted_scores, pr_thresholds, side="left"
        )
        valid_mask = alert_counts >= MIN_THRESHOLD_ALERT_COUNT
        if THRESHOLD_MIN_RECALL is not None:
            valid_mask = valid_mask & (pr_rec >= THRESHOLD_MIN_RECALL)
        if valid_mask.any():
            # F_beta = (1 + beta^2) * P * R / (beta^2 * P + R)
            b = THRESHOLD_FBETA
            denom = b * b * pr_prec + pr_rec
            fbeta_arr = np.where(
                denom > 0,
                (1.0 + b * b) * pr_prec * pr_rec / denom,
                0.0,
            )
            fbeta_arr = np.where(valid_mask, fbeta_arr, -1.0)
            best_idx = int(np.argmax(fbeta_arr))
            best_t = float(pr_thresholds[best_idx])
            best_prec = float(pr_prec[best_idx])
            best_rec = float(pr_rec[best_idx])
            best_fbeta = float(fbeta_arr[best_idx])
            # F1 at chosen threshold (for reporting / backward compat)
            best_f1 = (
                2.0 * best_prec * best_rec / (best_prec + best_rec)
                if (best_prec + best_rec) > 0
                else 0.0
            )
        else:
            best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
            best_fbeta = 0.0
    else:
        prauc = 0.0
        best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
        best_fbeta = 0.0

    n_val = int(len(y_val))
    n_val_pos = int(y_val.sum())
    val_random_ap = (n_val_pos / n_val) if n_val > 0 else 0.0

    metrics = {
        "label": label,
        "val_prauc": prauc,
        "val_precision": best_prec,
        "val_recall": best_rec,
        "val_f1": best_f1,
        "val_fbeta_05": best_fbeta,
        "threshold": best_t,
        "val_samples": n_val,
        "val_positives": n_val_pos,
        "val_random_ap": val_random_ap,
        "best_hyperparams": hyperparams,
        # R804: track via code-path (not value == 0.5) so a legitimately-optimised
        # threshold of 0.5 is never falsely flagged as uncalibrated.
        "_uncalibrated": not _has_val,
    }
    logger.info(
        "%s: PR-AUC=%.4f  F0.5=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f",
        label, prauc, best_fbeta, best_f1, best_prec, best_rec, best_t,
    )
    return model, metrics


def _compute_test_metrics(
    model: lgb.LGBMClassifier,
    threshold: float,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    label: str = "",
    _uncalibrated: bool = False,
) -> dict:
    """Evaluate a trained model on the held-out test set at the val-derived threshold.

    Uses the same MIN_VALID_TEST_ROWS guard as _train_one_model so an under-sized
    test split returns zeroed metrics rather than crashing.  test_prauc is computed
    without any threshold so it is comparable to val_prauc.

    R1100: requires at least one negative label so PR-AUC is meaningful.
    R1101: _uncalibrated=True is propagated into test_threshold_uncalibrated key.
    R1105: y_test.values is used for positional comparisons to avoid index misalign.
    """
    # R1100: guard against all-positive labels (average_precision_score = 1.0 trivially)
    _has_test = (
        not X_test.empty
        and len(y_test) >= MIN_VALID_TEST_ROWS
        and int(y_test.isna().sum()) == 0
        and int(y_test.sum()) >= 1
        and int((y_test == 0).sum()) >= 1
    )
    if not _has_test:
        logger.warning(
            "%s: test set too small or unbalanced (%d rows, %d positives, %d negatives)"
            " — test metrics will be zero.",
            label or "model",
            len(y_test),
            int(y_test.sum()) if not y_test.empty else 0,
            int((y_test == 0).sum()) if not y_test.empty else 0,
        )
        n_te = int(len(y_test))
        n_te_pos = int(y_test.sum()) if not y_test.empty else 0
        return {
            "test_prauc": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            # R1101: propagate uncalibrated flag
            "test_threshold_uncalibrated": _uncalibrated,
        }

    test_scores = model.predict_proba(X_test)[:, 1]
    prauc = float(average_precision_score(y_test, test_scores))
    preds = (test_scores >= threshold).astype(int)
    # R1105: use .values to prevent pandas index misalignment with numpy preds array
    y_arr = y_test.values
    tp = int(((preds == 1) & (y_arr == 1)).sum())
    fp = int(((preds == 1) & (y_arr == 0)).sum())
    fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    n_te = int(len(y_test))
    n_te_pos = int(y_test.sum())
    test_random_ap = (n_te_pos / n_te) if n_te > 0 else 0.0
    logger.info(
        "%s test: PR-AUC=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f",
        label, prauc, f1, prec, rec, threshold,
    )
    return {
        "test_prauc": prauc,
        "test_precision": prec,
        "test_recall": rec,
        "test_f1": f1,
        "test_samples": n_te,
        "test_positives": n_te_pos,
        "test_random_ap": test_random_ap,
        # R1101: propagate uncalibrated flag so downstream can distrust P/R/F1
        "test_threshold_uncalibrated": _uncalibrated,
    }


def _compute_train_metrics(
    model: lgb.LGBMClassifier,
    threshold: float,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    label: str = "",
) -> dict:
    """Evaluate a trained model on the training set (for reporting overfit / fit quality).

    Reports train_prauc, P/R/F1 at the validation-derived threshold, train_samples,
    train_positives, and train_random_ap (positives/samples = theoretical AP for random guess).
    """
    if X_train.empty or y_train.empty:
        return {
            "train_prauc": 0.0,
            "train_precision": 0.0,
            "train_recall": 0.0,
            "train_f1": 0.0,
            "train_samples": 0,
            "train_positives": 0,
            "train_random_ap": 0.0,
        }
    n_tr = int(len(y_train))
    n_tr_pos = int(y_train.sum())
    train_random_ap = (n_tr_pos / n_tr) if n_tr > 0 else 0.0
    train_scores = model.predict_proba(X_train)[:, 1]
    has_both = n_tr_pos >= 1 and (n_tr - n_tr_pos) >= 1
    train_prauc = float(average_precision_score(y_train, train_scores)) if has_both else 0.0
    preds = (train_scores >= threshold).astype(int)
    y_arr = y_train.values
    tp = int(((preds == 1) & (y_arr == 1)).sum())
    fp = int(((preds == 1) & (y_arr == 0)).sum())
    fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    logger.info(
        "%s train: PR-AUC=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  random_ap=%.4f",
        label, train_prauc, f1, prec, rec, train_random_ap,
    )
    return {
        "train_prauc": train_prauc,
        "train_precision": prec,
        "train_recall": rec,
        "train_f1": f1,
        "train_samples": n_tr,
        "train_positives": n_tr_pos,
        "train_random_ap": train_random_ap,
    }


def _compute_feature_importance(
    model: lgb.LGBMClassifier,
    feature_cols: List[str],
) -> list:
    """Return features ranked by LightGBM 'gain' importance (descending).

    Each entry has importance_gain_pct: share of total gain as a percentage (0–100).
    Uses the booster's native feature_importance(importance_type='gain'); falls back
    to sklearn-style .feature_importances_ when the booster attribute is absent
    (AttributeError), e.g. in unit tests with mock estimators.

    R1102: raises ValueError if importance vector length != feature_cols length.
    R1103: only AttributeError triggers fallback; other exceptions propagate.
    """
    try:
        booster = model.booster_
        names: List[str] = booster.feature_name()
        gains = booster.feature_importance(importance_type="gain").tolist()
    except AttributeError:
        # Fallback for mock / non-LightGBM models (no booster_ attribute).
        names = list(feature_cols)
        gains = model.feature_importances_.tolist()
        # R1102: guard against silent truncation by zip when lengths differ
        if len(gains) != len(names):
            raise ValueError(
                f"_compute_feature_importance: feature_importances_ length ({len(gains)}) "
                f"!= feature_cols length ({len(names)}). "
                "Ensure the model was trained with the same feature list."
            )

    total_gain = sum(gains)
    ranked = sorted(zip(names, gains), key=lambda x: x[1], reverse=True)
    return [
        {
            "rank": i + 1,
            "feature": name,
            "importance_gain_pct": round(100.0 * float(gain) / total_gain, 2) if total_gain > 0 else 0.0,
        }
        for i, (name, gain) in enumerate(ranked)
    ]


def train_dual_model(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    feature_cols: List[str],
    run_optuna: bool = True,
    test_df: Optional[pd.DataFrame] = None,
) -> Tuple[Optional[dict], Optional[dict], dict]:
    """Train Rated + Non-rated LightGBM models.

    .. deprecated::
        v10 (DEC-021) uses only the rated model.  The pipeline calls
        ``train_single_rated_model`` instead.  This function is retained for
        backward compatibility with integration-test mocks; do not call it
        from production code.

    Parameters
    ----------
    train_df, valid_df : labelled DataFrames with is_rated column
    feature_cols       : screened feature list (all tracks)
    run_optuna         : whether to run Optuna HPO (skipped in fast-mode)
    test_df            : held-out test split; when provided, test metrics and
                         LightGBM gain feature importance are appended to each
                         model's metrics dict and written into training_metrics.json.

    Returns
    -------
    (rated_artifacts, nonrated_artifacts, combined_metrics)
        Each artifacts dict: {"model": LGBMClassifier, "threshold": float,
                              "features": list, "metrics": dict}
        metrics dict contains val_* and train_* keys (always), test_* keys (when
        test_df provided), val_random_ap/train_random_ap/test_random_ap (random-guess
        AP = positives/samples), feature_importance list (importance_gain_pct), and
        importance_method string.
    """
    def _split(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
        rated = df[df["is_rated"]].copy()
        nonrated = df[~df["is_rated"]].copy()
        return rated, nonrated

    train_rated, train_nonrated = _split(train_df)
    val_rated, val_nonrated = _split(valid_df)

    _test_rated: pd.DataFrame
    _test_nonrated: pd.DataFrame
    if test_df is not None and not test_df.empty:
        _test_rated, _test_nonrated = _split(test_df)
    else:
        _test_rated = pd.DataFrame()
        _test_nonrated = pd.DataFrame()

    sw_rated = compute_sample_weights(train_rated)
    sw_nonrated = compute_sample_weights(train_nonrated)

    results: dict[str, Any] = {}
    for name, tr_df, vl_df, te_df, sw in [
        ("rated",    train_rated,    val_rated,    _test_rated,    sw_rated),
        ("nonrated", train_nonrated, val_nonrated, _test_nonrated, sw_nonrated),
    ]:
        if tr_df.empty:
            logger.warning("%s model: no training rows, skipping", name)
            results[name] = None
            continue

        avail_cols = [c for c in feature_cols if c in tr_df.columns]
        if name == "nonrated":  # exclude PROFILE_FEATURE_COLS — profile features are rated-only (R80)
            avail_cols = [c for c in avail_cols if c not in PROFILE_FEATURE_COLS]
        X_tr, y_tr = tr_df[avail_cols], tr_df["label"]
        X_vl = vl_df[avail_cols] if not vl_df.empty else X_tr.head(0)
        y_vl = vl_df["label"] if not vl_df.empty else y_tr.head(0)

        if run_optuna and not vl_df.empty and y_vl.sum() > 0:
            hp = run_optuna_search(X_tr, y_tr, X_vl, y_vl, sw, label=name)
        else:
            # Default params when validation is empty or no positives
            hp = {
                "n_estimators": 400,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "max_depth": 8,
                "min_child_samples": 20,
            }

        model, metrics = _train_one_model(X_tr, y_tr, X_vl, y_vl, sw, hp, label=name)

        # Training set performance (for overfit / fit quality reporting).
        metrics.update(
            _compute_train_metrics(
                model,
                metrics["threshold"],
                X_tr,
                y_tr,
                label=name,
            )
        )

        # R1104: only evaluate on test set when a real test split was provided.
        # Skipping when te_df is empty avoids polluting the artifact with
        # all-zero test_* keys that are indistinguishable from "evaluated but poor".
        if not te_df.empty:
            X_te = te_df[avail_cols]
            y_te = te_df["label"]
            metrics.update(
                _compute_test_metrics(
                    model,
                    metrics["threshold"],
                    X_te,
                    y_te,
                    label=name,
                    # R1101: propagate whether the threshold was a fallback
                    _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                )
            )

        # Feature importance ranked by LightGBM gain.
        metrics["feature_importance"] = _compute_feature_importance(model, avail_cols)
        metrics["importance_method"] = "gain"

        results[name] = {
            "model": model,
            "threshold": metrics["threshold"],
            "features": avail_cols,
            "metrics": metrics,
        }

    combined_metrics = {
        k: (v["metrics"] if v else None) for k, v in results.items()
    }
    return results.get("rated"), results.get("nonrated"), combined_metrics


def train_single_rated_model(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    feature_cols: List[str],
    run_optuna: bool = True,
    test_df: Optional[pd.DataFrame] = None,
) -> Tuple[Optional[dict], Optional[dict], dict]:
    """v10 single-model (DEC-021): train rated model only; return (rated_art, None, metrics).

    Only rows where is_rated==True are used for training, validation, and test
    evaluation.  Non-rated observations are intentionally excluded (DEC-009/010).
    """
    train_rated = train_df[train_df["is_rated"]].copy() if not train_df.empty else train_df
    val_rated = valid_df[valid_df["is_rated"]].copy() if not valid_df.empty else valid_df
    test_rated: Optional[pd.DataFrame]
    if test_df is not None and not test_df.empty:
        test_rated = test_df[test_df["is_rated"]].copy()
    else:
        test_rated = test_df

    if train_rated.empty:
        logger.warning("rated model: no training rows, skipping")
        return None, None, {"rated": None}

    sw_rated = compute_sample_weights(train_rated)
    avail_cols = [c for c in feature_cols if c in train_rated.columns]
    X_tr, y_tr = train_rated[avail_cols], train_rated["label"]
    X_vl = val_rated[avail_cols] if not val_rated.empty else X_tr.head(0)
    y_vl = val_rated["label"] if not val_rated.empty else y_tr.head(0)

    if run_optuna and not val_rated.empty and y_vl.sum() > 0:
        hp = run_optuna_search(X_tr, y_tr, X_vl, y_vl, sw_rated, label="rated")
    else:
        hp = {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "max_depth": 8,
            "min_child_samples": 20,
        }

    model, metrics = _train_one_model(X_tr, y_tr, X_vl, y_vl, sw_rated, hp, label="rated")

    metrics.update(
        _compute_train_metrics(model, metrics["threshold"], X_tr, y_tr, label="rated")
    )

    if test_rated is not None and not test_rated.empty:
        X_te = test_rated[avail_cols]
        y_te = test_rated["label"]
        metrics.update(
            _compute_test_metrics(
                model,
                metrics["threshold"],
                X_te,
                y_te,
                label="rated",
                _uncalibrated=bool(metrics.get("_uncalibrated", False)),
            )
        )

    metrics["feature_importance"] = _compute_feature_importance(model, avail_cols)
    metrics["importance_method"] = "gain"

    rated_art = {
        "model": model,
        "threshold": metrics["threshold"],
        "features": avail_cols,
        "metrics": metrics,
    }
    return rated_art, None, {"rated": metrics}


# ---------------------------------------------------------------------------
# Artifact bundle
# ---------------------------------------------------------------------------

def save_artifact_bundle(
    rated: Optional[dict],
    feature_cols: List[str],
    combined_metrics: dict,
    model_version: str,
    fast_mode: bool = False,
    sample_rated_n: Optional[int] = None,
    feature_spec_path: Optional[Path] = None,
) -> None:
    """Write all model artifacts atomically (v10 single rated model, DEC-021).

    v10 single-model format
    -----------------------
    models/model.pkl               {"model", "threshold", "features"}
    models/feature_list.json       [{name, track}]
    models/reason_code_map.json   {feature_name: reason_code} for scorer SHAP lookup
    models/model_version          <version string>
    models/training_metrics.json  per-model metrics (rated only)
    models/feature_spec.yaml      frozen feature spec snapshot (DEC-024, R3501)

    Legacy single-model format (for backward compat with existing scorer)
    -----------------------------------------------------------------------
    models/walkaway_model.pkl     {"model", "features", "threshold"}
    """
    # DEC-024 / R3501: freeze a copy of the feature spec into the artifact bundle so
    # the scorer can load an exact match to training-time spec_hash for reproducibility.
    spec_hash: Optional[str] = None
    if feature_spec_path is not None:
        _fsp = Path(feature_spec_path)
        if _fsp.exists():
            import shutil as _shutil
            _shutil.copy2(_fsp, MODEL_DIR / "feature_spec.yaml")
            spec_hash = hashlib.md5(_fsp.read_bytes()).hexdigest()[:12]
    # v10 single-model format (DEC-021): one model.pkl only
    if rated:
        _pkl_path = MODEL_DIR / "model.pkl"
        _tmp = _pkl_path.with_suffix(".pkl.tmp")
        joblib.dump(
            {"model": rated["model"], "threshold": rated["threshold"], "features": rated["features"]},
            _tmp,
        )
        os.replace(_tmp, _pkl_path)

    _legacy_set = set(LEGACY_FEATURE_COLS)
    feature_list = [
        {
            "name": c,
            "track": (
                "profile" if c in PROFILE_FEATURE_COLS
                else "B" if c in TRACK_B_FEATURE_COLS
                else "legacy" if c in _legacy_set
                else "LLM"   # Track LLM (DuckDB + feature spec)
            ),
        }
        for c in feature_cols
    ]
    (MODEL_DIR / "feature_list.json").write_text(
        json.dumps(feature_list, indent=2), encoding="utf-8"
    )

    # reason_code_map.json: feature name -> short reason code for SHAP output.
    # Static entries for Track B + legacy features; Track LLM features fall back
    # to a generated code so the scorer never hits a missing-key error.
    _STATIC_REASON_CODES: dict[str, str] = {
        "loss_streak": "LOSS_STREAK",
        "minutes_since_run_start": "RUN_DURATION",
        "wager": "BET_SIZE",
        "payout_odds": "PAYOUT_ODDS",
        "base_ha": "HOUSE_EDGE",
        "is_back_bet": "BACK_BET",
        "position_idx": "TABLE_POSITION",
        "cum_bets": "CUM_BETS",
        "cum_wager": "CUM_WAGER",
        "avg_wager_sofar": "AVG_WAGER",
        "time_of_day_sin": "TIME_OF_DAY",
        "time_of_day_cos": "TIME_OF_DAY",
    }
    reason_code_map: dict[str, str] = {}
    for feat in feature_cols:
        if feat in _STATIC_REASON_CODES:
            reason_code_map[feat] = _STATIC_REASON_CODES[feat]
        elif feat in PROFILE_FEATURE_COLS:
            # R76: profile features use PROFILE_ prefix for scorer readability
            reason_code_map[feat] = f"PROFILE_{feat[:28].upper()}"
        else:
            reason_code_map[feat] = f"FEAT_{feat[:30].upper()}"
    (MODEL_DIR / "reason_code_map.json").write_text(
        json.dumps(reason_code_map, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    (MODEL_DIR / "model_version").write_text(model_version, encoding="utf-8")
    # R703: flag when the fallback (uncalibrated) 0.5 threshold was used.
    # R804: read from the _uncalibrated code-path flag set by _train_one_model,
    # not from `threshold == 0.5` — a legitimately-optimised threshold of 0.5
    # must not be falsely flagged as uncalibrated.
    # R2207: _uncalibrated is stored inside rated["metrics"], not at the top level.
    # v10 single-model: only rated threshold is relevant; nonrated removed (R1606/R1908).
    _uncalibrated_threshold = {
        "rated": rated is not None and bool(
            rated["metrics"].get("_uncalibrated", False)
            if isinstance(rated.get("metrics"), dict)
            else rated.get("_uncalibrated", False)
        ),
    }
    (MODEL_DIR / "training_metrics.json").write_text(
        json.dumps(
            {
                **combined_metrics,
                "model_version": model_version,
                "fast_mode": fast_mode,
                # R301: record sampling metadata so artifacts can be audited
                # even when loaded later.  None = full rated population was used.
                "sample_rated_n": sample_rated_n,
                # R703: uncalibrated_threshold=True means the 0.5 fallback was used.
                "uncalibrated_threshold": _uncalibrated_threshold,
                # DEC-024 / R3501: SHA-256 prefix of the frozen feature spec for audit.
                "spec_hash": spec_hash,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    # Legacy backward-compat: write rated model as walkaway_model.pkl
    if rated:
        joblib.dump(
            {
                "model": rated["model"],
                "features": rated["features"],
                "threshold": rated["threshold"],
            },
            MODEL_DIR / "walkaway_model.pkl",
        )

    logger.info("Artifacts saved to %s  (version=%s)", MODEL_DIR, model_version)


# ---------------------------------------------------------------------------
# Main training pipeline
# ---------------------------------------------------------------------------

def run_pipeline(args) -> None:
    """Phase-1 training pipeline entry point."""
    pipeline_start = time.perf_counter()
    start, end = parse_window(args)
    use_local = getattr(args, "use_local_parquet", False)
    force = getattr(args, "force_recompute", False)
    fast_mode = getattr(args, "fast_mode", False)
    # --fast-mode implies --skip-optuna; allow either flag independently.
    skip_optuna = getattr(args, "skip_optuna", False) or fast_mode
    # --no-afg (No Automatic Feature Generation, DEC-020): skip Track LLM generation.
    # --fast-mode implies --no-afg to reduce compute when iterating quickly.
    # when iterating quickly on a short data horizon.
    no_afg = getattr(args, "no_afg", False) or fast_mode
    # --fast-mode-no-preload: disable session full-table preload; use per-day
    # PyArrow pushdown reads instead.  Reduces peak RAM for 8 GB machines.
    no_preload = getattr(args, "fast_mode_no_preload", False)
    # --sample-rated N (DEC-017 / R205): orthogonal to --fast-mode.
    # None means "use all rated canonical_ids" (default).
    sample_rated_n: Optional[int] = getattr(args, "sample_rated", None)
    # R302: reject invalid sampling sizes early with an actionable error.
    if sample_rated_n is not None and sample_rated_n < 1:
        raise SystemExit(
            f"--sample-rated N must be >= 1, got {sample_rated_n}. "
            "Pass a positive integer or omit the flag to use all rated patrons."
        )
    # R118 / R303: warn if --fast-mode-no-preload is given without --fast-mode
    # AND without --sample-rated.  When --sample-rated is used, the in-process
    # backfill path is taken (canonical_map is not None), so the preload_sessions
    # flag IS forwarded — the warning would be incorrect in that case.
    if no_preload and not fast_mode and sample_rated_n is None:
        logger.warning(
            "--fast-mode-no-preload has no effect without --fast-mode or --sample-rated; ignoring. "
            "Combine with --fast-mode or --sample-rated for memory-safe backfill."
        )

    # Auto-adjust window to actual data end when using local Parquet without
    # explicit --start/--end, so --recent-chunks is relative to data, not today.
    if use_local and not (getattr(args, "start", None) or getattr(args, "end", None)):
        data_end = _detect_local_data_end()
        if data_end is not None:
            days = getattr(args, "days", TRAINER_DAYS)
            end = _to_hk(
                datetime.combine(
                    data_end, datetime.min.time()
                )
            )
            start = end - timedelta(days=days)
            logger.info(
                "Local Parquet data end: %s -> adjusted window: %s -> %s",
                data_end, start.date(), end.date(),
            )
        else:
            logger.warning(
                "Could not detect data range from local Parquet metadata; "
                "using default window relative to now. "
                "Consider --start/--end explicitly."
            )

    logger.info("Training window: %s -> %s  (local=%s)", start.date(), end.date(), use_local)

    # 1. Monthly chunks (DEC-008 / SSOT §4.3)
    print("[Step 1/10] Training window and monthly chunks…", flush=True)
    t0 = time.perf_counter()
    chunks = get_monthly_chunks(start, end)
    _el = time.perf_counter() - t0
    print("[Step 1/10] Training window and monthly chunks done in %.1fs" % _el, flush=True)
    logger.info("Chunks: %d  (%.1fs)", len(chunks), _el)

    # Debug/test mode: limit to most recent N chunks so data loading from both
    # ClickHouse and local Parquet is proportionally restricted.
    recent_chunks = getattr(args, "recent_chunks", None)
    if recent_chunks is not None and recent_chunks > 0:
        if recent_chunks < len(chunks):
            chunks = chunks[-recent_chunks:]
            logger.info(
                "DEBUG MODE (--recent-chunks %d): trimmed to %s -> %s",
                recent_chunks,
                chunks[0]["window_start"].date(),
                chunks[-1]["window_end"].date(),
            )
        else:
            logger.info(
                "DEBUG MODE (--recent-chunks %d): requested >= total chunks (%d), using all",
                recent_chunks,
                len(chunks),
            )

    # Effective window is derived from the chunk list after optional trimming.
    # All subsequent data loading (identity/profile checks/profile load) must
    # use this window so --recent-chunks applies consistently to all tables.
    effective_start = chunks[0]["window_start"] if chunks else start
    effective_end = chunks[-1]["window_end"] if chunks else end
    # DEC-018: normalize effective window to tz-naive so all downstream helpers
    # (ensure_player_profile_ready, load_player_profile, apply_dq
    # called from the canonical-map path) receive tz-naive datetime arguments.
    effective_start = effective_start.replace(tzinfo=None) if effective_start.tzinfo else effective_start
    effective_end   = effective_end.replace(tzinfo=None)   if effective_end.tzinfo   else effective_end

    # DEC-017: derive the data horizon (days of available history in this run).
    # Used to (a) cap profile snapshot range in fast-mode, and (b) select only
    # the profile feature subset that can actually be computed from available data.
    data_horizon_days = max(0, (effective_end - effective_start).days)
    # R203: warn early when the horizon is so small that all profile features will
    # be excluded.  Sessions span 7d before the smallest computable window; any
    # horizon below 7 days means get_profile_feature_cols() returns an empty list
    # and the rated model trains with no profile signal at all.
    if fast_mode and data_horizon_days < 7:
        logger.warning(
            "FAST MODE: data_horizon_days=%d is very small (< 7 days); "
            "all profile features will be excluded from active_feature_cols. "
            "Consider using --recent-chunks >= 2 for meaningful profile coverage.",
            data_horizon_days,
        )

    # 2. Chunk-level split — used ONLY to derive train_end for the canonical
    #    mapping cutoff (B1 / R25 identity-leakage guard).  The actual row
    #    assignment to train/valid/test happens later at row level (SSOT §9.2).
    print("[Step 2/10] Chunk-level split (train_end derivation)…", flush=True)
    t0 = time.perf_counter()
    split = get_train_valid_test_split(chunks)
    _el = time.perf_counter() - t0
    print("[Step 2/10] Chunk-level split done in %.1fs" % _el, flush=True)
    logger.info("Chunk-level split (train_end derivation): %.1fs", _el)
    train_end = (
        max(c["window_end"] for c in split["train_chunks"])
        if split["train_chunks"] else end
    )
    if hasattr(train_end, "tzinfo") and train_end.tzinfo:
        # DEC-018: tz_convert to HK first, then strip tz, matching labels.py semantics.
        train_end = pd.Timestamp(train_end).tz_convert("Asia/Hong_Kong")
        train_end = train_end.replace(tzinfo=None)

    # 3. Build canonical mapping with TRAINING window cutoff (B1 — prevents
    #    identity links that arose after training from leaking into training data).
    #    Also get FND-12 dummy player_ids so we drop them from training (TRN-04).
    print("[Step 3/10] Build canonical identity mapping…", flush=True)
    t0 = time.perf_counter()
    logger.info("Building canonical identity mapping (cutoff=%s)…", train_end)
    dummy_player_ids: set = set()
    if use_local:
        # sessions_only=True: canonical map only needs sessions; skipping the
        # 400M+ row bet parquet avoids OOM on low-RAM machines.
        _, sessions_all = load_local_parquet(
            effective_start,
            effective_end + timedelta(days=1),
            sessions_only=True,
        )
        _, sessions_all = apply_dq(
            pd.DataFrame(columns=["bet_id"]),  # dummy bets
            sessions_all,
            effective_start,
            effective_end + timedelta(days=1),
        )
        canonical_map = build_canonical_mapping_from_df(sessions_all, cutoff_dtm=train_end)
        try:
            dummy_player_ids = get_dummy_player_ids_from_df(sessions_all, cutoff_dtm=train_end)
        except Exception as exc:
            logger.warning("get_dummy_player_ids_from_df failed (%s); not filtering dummies", exc)
        sessions_all = None
    else:
        try:
            client = get_clickhouse_client()
            canonical_map = build_canonical_mapping(client, cutoff_dtm=train_end)
            dummy_player_ids = get_dummy_player_ids(client, cutoff_dtm=train_end)
        except Exception as exc:
            logger.warning("ClickHouse canonical mapping failed (%s); using empty map", exc)
            canonical_map = pd.DataFrame(columns=["player_id", "canonical_id"])
        sessions_all = None

    _el = time.perf_counter() - t0
    print("[Step 3/10] Build canonical identity mapping done in %.1fs" % _el, flush=True)
    logger.info(
        "Canonical mapping: %d rows; FND-12 dummy player_ids to exclude: %d  (%.1fs)",
        len(canonical_map), len(dummy_player_ids), _el,
    )

    # DEC-017 / R205: rated-patron sampling is now an independent, orthogonal option
    # controlled by --sample-rated N.  fast-mode alone does NOT imply sampling —
    # it restricts the *data horizon* only.  This decouples fast iteration (horizon)
    # from dataset-size reduction (patron count).
    rated_whitelist: Optional[set] = None
    if sample_rated_n is not None and not canonical_map.empty:
        _sample = (
            canonical_map["canonical_id"]
            .astype(str)
            .drop_duplicates()
            .sort_values()
            .head(sample_rated_n)
        )
        rated_whitelist = set(_sample.tolist())
        logger.info(
            "--sample-rated: sampled %d / %d rated canonical_ids (deterministic sort+head)",
            len(rated_whitelist), canonical_map["canonical_id"].nunique(),
        )

    # 3b. Auto-check local player_profile freshness and backfill missing
    #     ranges before training starts (one-command flow, OOM-safe helper).
    print("[Step 4/10] Ensure player_profile ready (backfill if needed)…", flush=True)
    t0 = time.perf_counter()
    ensure_player_profile_ready(
        effective_start,
        effective_end,
        use_local_parquet=use_local,
        canonical_id_whitelist=rated_whitelist,
        snapshot_interval_days=1,
        preload_sessions=not no_preload,
        canonical_map=canonical_map,
        fast_mode=fast_mode,
        max_lookback_days=data_horizon_days if fast_mode else 365,
        use_month_end_snapshots=True,
    )
    _el = time.perf_counter() - t0
    print("[Step 4/10] Ensure player_profile ready done in %.1fs" % _el, flush=True)
    logger.info("ensure_player_profile_ready: %.1fs", _el)

    # 3c. Load player_profile once for the entire training window (PLAN Step 4).
    #     Pass the resulting DataFrame to every process_chunk call so each chunk
    #     can do the PIT/as-of join without re-querying.  If load fails, profile
    #     features are 0 for all rows (graceful degradation).
    # R109: in fast-mode, pass whitelist only (profile has 1k players, not full map)
    _rated_cids: Optional[List[str]] = (
        list(rated_whitelist)
        if rated_whitelist
        else (
            canonical_map["canonical_id"].astype(str).tolist()
            if not canonical_map.empty
            else None
        )
    )
    print("[Step 5/10] Load player_profile for PIT join…", flush=True)
    t0 = time.perf_counter()
    profile_df = load_player_profile(
        effective_start,
        effective_end,
        use_local_parquet=use_local,
        canonical_ids=_rated_cids,
    )
    _el = time.perf_counter() - t0
    if profile_df is not None:
        print("[Step 5/10] Load player_profile done in %.1fs (%d rows)" % (_el, len(profile_df)), flush=True)
        logger.info("player_profile: loaded %d snapshot rows for PIT join (%.1fs)", len(profile_df), _el)
    else:
        print("[Step 5/10] Load player_profile done in %.1fs (not available)" % _el, flush=True)
        logger.info("player_profile: not available — profile features will be NaN (%.1fs)", _el)

    feature_spec: Optional[dict] = None
    if not no_afg:
        feature_spec = load_feature_spec(FEATURE_SPEC_PATH)
        logger.info("Track LLM: loaded feature spec from %s", FEATURE_SPEC_PATH)

    # 4. Process chunks -> write parquet
    print("[Step 6/10] Process chunks (DQ, labels, Track Human, Track LLM)…", flush=True)
    t0 = time.perf_counter()
    chunk_paths = []
    for i, chunk in enumerate(chunks):
        path = process_chunk(
            chunk,
            canonical_map,
            dummy_player_ids=dummy_player_ids,
            use_local_parquet=use_local,
            force_recompute=force,
            profile_df=profile_df,
            feature_spec=feature_spec,
            no_afg=no_afg,
        )
        if path is not None:
            chunk_paths.append(path)

    _el = time.perf_counter() - t0
    print("[Step 6/10] Process chunks done in %.1fs (%d chunks)" % (_el, len(chunk_paths)), flush=True)
    logger.info("Process chunks: %d produced  (%.1fs)", len(chunk_paths), _el)
    if not chunk_paths:
        raise SystemExit("No chunks produced any usable data — check data source / time window")

    # 5. Load all chunks, concatenate (OOM guard: warn if chunk data is large)
    print("[Step 7/10] Load all chunks, concat, row-level train/valid/test split…", flush=True)
    t0 = time.perf_counter()
    _chunk_total_bytes = sum(Path(p).stat().st_size for p in chunk_paths)
    _est_ram_gb = (_chunk_total_bytes * CHUNK_CONCAT_RAM_FACTOR) / (1024**3)
    if _chunk_total_bytes >= CHUNK_CONCAT_MEMORY_WARN_BYTES:
        logger.warning(
            "Chunk Parquets total %.2f GB on disk -> estimated %.1f GB RAM for concat + train/valid split. "
            "Reduce training window (--days / --start --end) or ensure sufficient RAM to avoid OOM.",
            _chunk_total_bytes / (1024**3),
            _est_ram_gb,
        )
    all_dfs = [pd.read_parquet(p) for p in chunk_paths]
    full_df = pd.concat(all_dfs, ignore_index=True)
    logger.info("Total rows: %d  (label=1: %d)", len(full_df), int(full_df["label"].sum()))

    # 6. Row-level time-ordered split (SSOT §9.2, todo-row-level-time-split).
    #    Sort the concatenated dataset strictly by time, then assign the first
    #    TRAIN_SPLIT_FRAC rows to "train", the next VALID_SPLIT_FRAC to "valid",
    #    and the remainder to "test".  This guarantees non-empty valid/test sets
    #    regardless of how many monthly chunks are available.
    #
    #    DEC-018: payout_complete_dtm is tz-naive datetime64[ns] after apply_dq().
    #    The defensive tz-strip below handles externally-sourced Parquet that may
    #    not have gone through apply_dq().
    # R803: validate fractions at runtime so misconfiguration is caught early.
    assert TRAIN_SPLIT_FRAC + VALID_SPLIT_FRAC < 1.0, (
        f"TRAIN_SPLIT_FRAC ({TRAIN_SPLIT_FRAC}) + VALID_SPLIT_FRAC ({VALID_SPLIT_FRAC}) "
        f"must be < 1.0 to leave room for the test set"
    )
    _payout_ts = pd.to_datetime(full_df["payout_complete_dtm"])
    if _payout_ts.dt.tz is not None:
        _payout_ts = _payout_ts.dt.tz_localize(None)

    # Stable sort: primary = payout time, tiebreakers = canonical_id, bet_id.
    # R704: use inplace operations to avoid intermediate DataFrame copies and reduce
    # peak RAM during the sort step.
    _sort_cols = ["_sort_ts_tmp"] + [
        c for c in ("canonical_id", "bet_id") if c in full_df.columns
    ]
    full_df["_sort_ts_tmp"] = _payout_ts
    full_df.sort_values(_sort_cols, kind="stable", na_position="last", inplace=True)
    full_df.drop(columns=["_sort_ts_tmp"], inplace=True)
    full_df.reset_index(drop=True, inplace=True)

    n_rows = len(full_df)
    _train_end_idx = int(n_rows * TRAIN_SPLIT_FRAC)
    _valid_end_idx = int(n_rows * (TRAIN_SPLIT_FRAC + VALID_SPLIT_FRAC))
    _row_pos = np.arange(n_rows)
    full_df["_split"] = np.select(
        [_row_pos < _train_end_idx, _row_pos < _valid_end_idx],
        ["train", "valid"],
        default="test",
    )

    train_df = full_df[full_df["_split"] == "train"].copy()
    valid_df  = full_df[full_df["_split"] == "valid"].copy()
    test_df   = full_df[full_df["_split"] == "test"].copy()
    del full_df  # R802: release concat buffer; ~halves peak RAM after split

    # R700: compare row-level _actual_train_end against chunk-level train_end.
    # The canonical mapping cutoff (B1/R25 guard) always uses chunk-level train_end;
    # this log makes any semantic drift between the two boundaries observable.
    # R701 (known limitation): same run rows may be assigned to different split sets
    # at row-level boundaries — group-aware split is a long-term improvement.
    _actual_train_end = train_df["payout_complete_dtm"].max() if not train_df.empty else None
    if _actual_train_end is not None and pd.notnull(_actual_train_end):
        _te_chunk = pd.Timestamp(train_end) if train_end else None
        # DEC-018: strip tz from _te_chunk so both sides are tz-naive for comparison
        # (train_end comes from chunk["window_end"] which is tz-aware; _actual_train_end
        # comes from payout_complete_dtm which is tz-naive after apply_dq).
        if _te_chunk is not None and _te_chunk.tzinfo is not None:
            _te_chunk = _te_chunk.replace(tzinfo=None)
        _te_row = pd.Timestamp(str(_actual_train_end))
        # DEC-018: strip tz from _te_row for the same reason as _te_chunk —
        # payout_complete_dtm may be tz-aware when sourced from test mocks or
        # external Parquet that skipped apply_dq().
        if _te_row.tzinfo is not None:
            _te_row = _te_row.replace(tzinfo=None)
        if _te_chunk is not None and _te_row != _te_chunk:
            logger.warning(
                "R700: chunk-level train_end (%s) differs from row-level "
                "_actual_train_end (%s) by %s — "
                "B1/R25 canonical mapping cutoff uses chunk-level train_end.",
                _te_chunk.date(), _te_row.date(),
                abs(_te_row - _te_chunk),
            )
        else:
            logger.info(
                "R700: chunk-level train_end (%s) matches row-level _actual_train_end (%s).",
                _te_chunk, _te_row,
            )
    _el = time.perf_counter() - t0
    print("[Step 7/10] Load all chunks, concat, row-level split done in %.1fs (train=%d valid=%d test=%d)" % (_el, len(train_df), len(valid_df), len(test_df)), flush=True)
    logger.info(
        "Row-level split (%.0f/%.0f/%.0f) — train: %d  valid: %d  test: %d  (load+sort+split: %.1fs)",
        TRAIN_SPLIT_FRAC * 100,
        VALID_SPLIT_FRAC * 100,
        (1.0 - TRAIN_SPLIT_FRAC - VALID_SPLIT_FRAC) * 100,
        len(train_df), len(valid_df), len(test_df),
        _el,
    )
    if len(valid_df) < MIN_VALID_TEST_ROWS:
        logger.warning(
            "Validation set has only %d rows (MIN_VALID_TEST_ROWS=%d); "
            "PR-AUC and Optuna results will be unreliable. "
            "Consider adding more --recent-chunks.",
            len(valid_df), MIN_VALID_TEST_ROWS,
        )
    if len(test_df) < MIN_VALID_TEST_ROWS:
        logger.warning(
            "Test set has only %d rows (MIN_VALID_TEST_ROWS=%d); "
            "backtester metrics will be unreliable.",
            len(test_df), MIN_VALID_TEST_ROWS,
        )

    # DEC-017: in fast-mode, restrict profile features to those computable within
    # the available data horizon; non-profile features (Track B + legacy) are
    # always included.  In normal mode, use the full ALL_FEATURE_COLS list.
    if fast_mode:
        _active_profile_cols = get_profile_feature_cols(data_horizon_days)
        active_feature_cols: List[str] = (
            TRACK_B_FEATURE_COLS + LEGACY_FEATURE_COLS + _active_profile_cols
        )
        logger.info(
            "FAST MODE: active profile features = %d / %d "
            "(data_horizon_days=%d)",
            len(_active_profile_cols), len(PROFILE_FEATURE_COLS), data_horizon_days,
        )
    else:
        active_feature_cols = ALL_FEATURE_COLS

    # 5b. Full-feature screening (DEC-020).
    # Runs on the TRAINING SET ONLY to comply with TRN-09 anti-leakage rules.
    #
    # Candidate set = active_feature_cols (Track Human + Legacy + Profile) PLUS
    # Track LLM candidate columns declared in feature spec and present in train_df.
    if not no_afg and feature_spec is not None:
        _track_llm_cols = [
            cand.get("feature_id")
            for cand in (feature_spec.get("track_llm", {}) or {}).get("candidates", [])
            if cand.get("feature_id") in train_df.columns
        ]
        if _track_llm_cols:
            logger.info(
                "screen_features: loaded %d Track LLM candidate columns from feature spec",
                len(_track_llm_cols),
            )
        _all_candidate_cols: List[str] = list(dict.fromkeys(active_feature_cols + _track_llm_cols))
    else:
        _all_candidate_cols = active_feature_cols

    # Only screen columns that actually exist in train_df (graceful degradation
    # when tests or data sources don't produce all expected feature columns).
    _present_candidate_cols = [c for c in _all_candidate_cols if c in train_df.columns]
    if not _present_candidate_cols:
        logger.warning(
            "screen_features: no candidate columns found in train_df — skipping screening"
        )
        # R1004: restrict active_feature_cols to columns actually present in train_df
        # so downstream training does not attempt to select absent columns.
        active_feature_cols = [c for c in active_feature_cols if c in train_df.columns]
        print("[Step 8/10] Feature screening skipped (no candidates)", flush=True)
    else:
        print("[Step 8/10] Feature screening…", flush=True)
        t0 = time.perf_counter()
        screened_cols = screen_features(
            feature_matrix=train_df,
            labels=train_df["label"],
            feature_names=_present_candidate_cols,
        )
        _el = time.perf_counter() - t0
        print("[Step 8/10] Feature screening done in %.1fs (%d -> %d features)" % (_el, len(_present_candidate_cols), len(screened_cols)), flush=True)
        logger.info(
            "screen_features: %d -> %d features retained  (%.1fs)",
            len(_present_candidate_cols), len(screened_cols), _el,
        )
        # R1001: post-screening sanity — ensure at least one Track-B feature survives.
        # Re-add any missing Track-B features from train_df as a fallback rather than
        # failing silently.
        _screened_set = set(screened_cols)
        if not _screened_set.intersection(TRACK_B_FEATURE_COLS):
            _missing_track_b = [c for c in TRACK_B_FEATURE_COLS if c in train_df.columns]
            if _missing_track_b:
                logger.warning(
                    "screen_features: no TRACK_B_FEATURE_COLS survived screening — "
                    "re-appending %d Track-B features as fallback (R1001)",
                    len(_missing_track_b),
                )
                screened_cols = screened_cols + [
                    c for c in _missing_track_b if c not in _screened_set
                ]
        active_feature_cols = screened_cols

    if not active_feature_cols:
        # R1613: explicit guardrail message for zero-feature situations.  In
        # integration / debug contexts (e.g. heavily mocked tests) we still
        # want the pipeline to run so that wiring between stages can be
        # exercised, so we fall back to a single constant "bias" feature
        # instead of terminating the process.
        msg = (
            "screen_features + Track B fallback both returned empty feature list. "
            "Cannot train any model. Check data quality and feature definitions."
        )
        logger.warning(msg)
        print(msg, flush=True)
        _placeholder_col = "bias"  # constant feature for integration/debug runs (R1605: named via explicit variable)
        if _placeholder_col not in train_df.columns:
            train_df[_placeholder_col] = 0.0
        if not valid_df.empty and _placeholder_col not in valid_df.columns:
            valid_df[_placeholder_col] = 0.0
        if test_df is not None and not test_df.empty and _placeholder_col not in test_df.columns:
            test_df[_placeholder_col] = 0.0
        active_feature_cols = [_placeholder_col]

    # 6. Train dual model (Optuna + run-level sample_weight, DEC-013)
    #    test_df is passed so test-set metrics and feature importance are
    #    computed immediately after training and included in the artifact.
    print("[Step 9/10] Train single rated model (Optuna + LightGBM) + test-set eval…", flush=True)
    t0 = time.perf_counter()
    model_version = get_model_version()
    rated_art, _, combined_metrics = train_single_rated_model(
        train_df,
        valid_df,
        active_feature_cols,
        run_optuna=not skip_optuna,
        test_df=test_df,
    )
    _el = time.perf_counter() - t0
    print("[Step 9/10] Train single rated model + test-set eval done in %.1fs" % _el, flush=True)
    logger.info("train_single_rated_model + test eval: %.1fs", _el)

    # 7. Save artifacts
    print("[Step 10/10] Save artifact bundle…", flush=True)
    t0 = time.perf_counter()
    save_artifact_bundle(
        rated_art, active_feature_cols, combined_metrics, model_version,
        fast_mode=fast_mode,
        sample_rated_n=sample_rated_n,
        feature_spec_path=FEATURE_SPEC_PATH if not no_afg else None,
    )
    _el = time.perf_counter() - t0
    print("[Step 10/10] Save artifact bundle done in %.1fs" % _el, flush=True)
    logger.info("save_artifact_bundle: %.1fs", _el)

    # Remove stale nonrated_model.pkl / rated_model.pkl left over from previous
    # dual-model runs so scorer/backtester cannot accidentally fall back to a
    # v9 artifact (v10 uses model.pkl only).
    for _stale in ["nonrated_model.pkl", "rated_model.pkl"]:
        _stale_path = MODEL_DIR / _stale
        if _stale_path.exists():
            _stale_path.unlink()
            logger.info("Removed stale artifact: %s", _stale)

    total_sec = time.perf_counter() - pipeline_start
    print("All steps completed. Pipeline total: %.1fs (%.1f min)" % (total_sec, total_sec / 60.0), flush=True)
    logger.info("Pipeline total: %.1fs (%.1f min)", total_sec, total_sec / 60.0)

    summary = {
        "model_version": model_version,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "total_rows": n_rows,
        "metrics": combined_metrics,
    }
    print(json.dumps(summary, indent=2, default=str))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Patron Walkaway — Phase 1 Trainer")
    parser.add_argument("--start", default=None, help="Training window start (YYYY-MM-DD or ISO)")
    parser.add_argument("--end",   default=None, help="Training window end")
    parser.add_argument(
        "--days", type=int, default=TRAINER_DAYS,
        help="Last N days ending 30m ago (used when --start/--end are not given)",
    )
    parser.add_argument(
        "--use-local-parquet", action="store_true",
        help="Read from data/ Parquet instead of ClickHouse",
    )
    parser.add_argument(
        "--force-recompute", action="store_true",
        help="Ignore cached chunk Parquet files and recompute",
    )
    parser.add_argument(
        "--skip-optuna", action="store_true",
        help="Skip Optuna search and use default LightGBM hyperparameters",
    )
    parser.add_argument(
        "--recent-chunks", type=int, default=None, metavar="N",
        help=(
            "Debug/test mode: use only the last N monthly chunks from the training "
            "window. Limits data loaded from both ClickHouse and local Parquet. "
            "Recommended N>=3 to keep train/valid/test all non-empty. "
            "E.g. --recent-chunks 3 uses roughly the last 3 months of data."
        ),
    )
    parser.add_argument(
        "--fast-mode", action="store_true",
        help=(
            "Fast mode (DEC-017 Data-Horizon): restrict all data access to the "
            "effective training window — no 365-day lookback pushed back for profiles. "
            "Profile features are dynamically layered based on the available data horizon. "
            "Profile snapshots follow month-end schedule (same as full mode). "
            "Implies --skip-optuna and --no-afg (skips Track LLM generation). "
            "Use --sample-rated N (separate flag) to also sample rated patrons. "
            "NEVER use artifacts from this mode in production — "
            "training_metrics.json will be flagged with fast_mode=true."
        ),
    )
    parser.add_argument(
        "--no-afg", action="store_true",
        help=(
            "No Automatic Feature Generation (DEC-020): skip Track LLM (DuckDB) "
            "feature generation. Feature screening still runs on Track Human + "
            "player-level/profile + legacy features. "
            "Scorer then computes only Track Human + profile (+ legacy). "
            "Orthogonal to --fast-mode (--fast-mode implies --no-afg). "
            "Use for faster iteration or to validate the non-LLM path independently."
        ),
    )
    parser.add_argument(
        "--fast-mode-no-preload", action="store_true",
        help=(
            "Disable full-table session Parquet preload during profile backfill. "
            "Instead, each snapshot day reads only the relevant time window via "
            "PyArrow pushdown filters. Recommended for machines with <=8 GB RAM "
            "where the full session Parquet (~74M rows) would cause OOM. "
            "Trade-off: backfill is slower but memory-safe. "
            "Combine with --fast-mode for best effect on low-RAM machines."
        ),
    )
    parser.add_argument(
        "--sample-rated", type=int, default=None, metavar="N",
        help=(
            "Deterministically sample N rated canonical_ids (sorted lexicographically, "
            "head N). Orthogonal to --fast-mode: can be combined or used independently. "
            "Default: no sampling (all rated canonical_ids are used). "
            "Example: --sample-rated 1000 to train on a 1k patron subset."
        ),
    )
    parser.add_argument(
        "--no-month-end-snapshots", action="store_false", dest="month_end_snapshots",
        help=(
            "Deprecated compatibility flag. Month-end profile snapshot scheduling "
            "is now always enforced in all modes (including --fast-mode), so this "
            "option has no effect."
        ),
    )
    parser.set_defaults(month_end_snapshots=True)
    args = parser.parse_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()
