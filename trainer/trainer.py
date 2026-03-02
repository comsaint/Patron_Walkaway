"""trainer/trainer.py — Phase 1 Refactor
=========================================
Patron Walkaway Prediction — Training Pipeline

Pipeline (SSOT §4.3 / §9)
--------------------------
1. time_fold.get_monthly_chunks(start, end)  → month boundaries
2. Per chunk: load bets + sessions → DQ → identity → labels → Track-B features
   - Data source: ClickHouse (production) OR local Parquet (dev iteration)
   - Labels use C1 extended pull; bets in (window_end, extended_end] are
     used only for label computation, NOT added to training rows.
3. Write each processed chunk to .data/chunks/ as Parquet.
4. Concatenate all chunks; split train / valid / test at chunk granularity.
5. sample_weight = 1 / N_visit  (canonical_id × gaming_day), train set only.
6. Optuna TPE hyperparameter search on validation set (per model type).
7. Train Rated + Non-rated LightGBM with class_weight='balanced' + sample_weight.
8. Atomic artifact bundle → trainer/models/.

Artifact format (version-tagged)
---------------------------------
models/
  rated_model.pkl           LightGBM model for casino-card players
  nonrated_model.pkl        LightGBM model for anonymous players
  feature_list.json         [{name, track}]  track ∈ {"B", "legacy"}
  model_version             YYYYMMDD-HHMMSS-<git7>  (plain text)
  training_metrics.json     per-model validation metrics + Optuna best params

Backward compatibility
----------------------
The legacy artifact walkaway_model.pkl (single-model dict) is ALSO written
alongside the new dual-model bundle so that the existing scorer/validator can
keep running until they are refactored in Steps 7–8.

Data source switching
---------------------
  --use-local-parquet   Read from .data/local/ Parquet files instead of
                        ClickHouse.  Same DQ filters + time semantics apply.
  Default: ClickHouse for production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple

import joblib
import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import average_precision_score, precision_score
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
    G1_PRECISION_MIN = _cfg.G1_PRECISION_MIN
    G1_ALERT_VOLUME_MIN_PER_HOUR = _cfg.G1_ALERT_VOLUME_MIN_PER_HOUR
    G1_FBETA = _cfg.G1_FBETA
    PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
    SOURCE_DB = _cfg.SOURCE_DB
    TBET = _cfg.TBET
    TSESSION = _cfg.TSESSION
    HK_TZ_STR: str = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    TRAINER_DAYS: int = getattr(_cfg, "TRAINER_DAYS", 30)
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

    WALKAWAY_GAP_MIN = _cfg.WALKAWAY_GAP_MIN
    ALERT_HORIZON_MIN = _cfg.ALERT_HORIZON_MIN
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    BET_AVAIL_DELAY_MIN = _cfg.BET_AVAIL_DELAY_MIN
    SESSION_AVAIL_DELAY_MIN = _cfg.SESSION_AVAIL_DELAY_MIN
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    G1_PRECISION_MIN = _cfg.G1_PRECISION_MIN
    G1_ALERT_VOLUME_MIN_PER_HOUR = _cfg.G1_ALERT_VOLUME_MIN_PER_HOUR
    G1_FBETA = _cfg.G1_FBETA
    PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
    SOURCE_DB = _cfg.SOURCE_DB
    TBET = _cfg.TBET
    TSESSION = _cfg.TSESSION
    HK_TZ_STR = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    TRAINER_DAYS = getattr(_cfg, "TRAINER_DAYS", 30)

# Module-level pipeline imports (same try/except pattern)
try:
    from time_fold import get_monthly_chunks, get_train_valid_test_split  # type: ignore[import]
    from identity import build_canonical_mapping_from_df  # type: ignore[import]
    from labels import compute_labels  # type: ignore[import]
    from features import (  # type: ignore[import]
        compute_loss_streak,
        compute_run_boundary,
        compute_table_hc,
        build_entity_set,
        run_dfs_exploration,
        save_feature_defs,
        load_feature_defs,
        compute_feature_matrix,
    )
    from db_conn import get_clickhouse_client  # type: ignore[import]
except ModuleNotFoundError:
    from trainer.time_fold import get_monthly_chunks, get_train_valid_test_split  # type: ignore[import]
    from trainer.identity import build_canonical_mapping_from_df  # type: ignore[import]
    from trainer.labels import compute_labels  # type: ignore[import]
    from trainer.features import (  # type: ignore[import]
        compute_loss_streak,
        compute_run_boundary,
        compute_table_hc,
        build_entity_set,
        run_dfs_exploration,
        save_feature_defs,
        load_feature_defs,
        compute_feature_matrix,
    )
    from trainer.db_conn import get_clickhouse_client  # type: ignore[import]

HK_TZ = ZoneInfo(HK_TZ_STR)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / ".data"
CHUNK_DIR = DATA_DIR / "chunks"
LOCAL_PARQUET_DIR = DATA_DIR / "local"
MODEL_DIR = BASE_DIR / "models"
FEATURE_DEFS_DIR = MODEL_DIR / "saved_feature_defs"  # Track A feature definitions (DEC-002)
OUT_DIR = BASE_DIR / "out_trainer"

for _d in (DATA_DIR, CHUNK_DIR, LOCAL_PARQUET_DIR, MODEL_DIR, OUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Track-B feature column list (shared with scorer via feature_list.json)
# ---------------------------------------------------------------------------
TRACK_B_FEATURE_COLS: List[str] = [
    "loss_streak",
    "run_id",
    "minutes_since_run_start",
    "table_hc",
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

ALL_FEATURE_COLS: List[str] = TRACK_B_FEATURE_COLS + LEGACY_FEATURE_COLS

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
    COALESCE(num_games_with_wager, 0) AS num_games_with_wager
""".strip()


def load_clickhouse_data(
    window_start: datetime,
    extended_end: datetime,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Query ClickHouse for bets in [window_start, extended_end] and matching sessions."""
    logger.info("ClickHouse pull: %s → %s", window_start, extended_end)
    client = get_clickhouse_client()
    params = {"start": window_start, "end": extended_end}

    # Pull extra history so Track-B state machines (loss_streak, run_boundary)
    # have cross-chunk context.  process_chunk filters training rows to
    # [window_start, window_end) after Track-B features are computed.
    bets_query = f"""
        SELECT {_BET_SELECT_COLS}
        FROM {SOURCE_DB}.{TBET}
        WHERE payout_complete_dtm >= %(start)s - INTERVAL {HISTORY_BUFFER_DAYS} DAY
          AND payout_complete_dtm < %(end)s
          AND wager > 0
          AND payout_complete_dtm IS NOT NULL
    """

    # No FINAL on t_session (G1). FND-01 dedup handled downstream by identity.py.
    # Pull sessions overlapping the window with a ±1-day buffer.
    # FND-02: is_manual=1 rows are accounting adjustments, not real play (R38 parity fix)
    session_query = f"""
        SELECT {_SESSION_SELECT_COLS}
        FROM {SOURCE_DB}.{TSESSION}
        WHERE session_start_dtm >= %(start)s - INTERVAL 1 DAY
          AND session_start_dtm < %(end)s + INTERVAL 1 DAY
          AND is_deleted = 0
          AND is_canceled = 0
          AND is_manual = 0
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
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load bets + sessions from local Parquet files, filtered to the window.

    Expects:
      .data/local/bets.parquet     — full t_bet export with the same columns
      .data/local/sessions.parquet — full t_session export with the same columns

    Applies the same DQ filters (wager > 0, payout_complete_dtm IS NOT NULL)
    and time window restriction as the ClickHouse path.
    """
    bets_path = LOCAL_PARQUET_DIR / "bets.parquet"
    sess_path = LOCAL_PARQUET_DIR / "sessions.parquet"

    if not bets_path.exists() or not sess_path.exists():
        raise FileNotFoundError(
            f"Local Parquet files not found in {LOCAL_PARQUET_DIR}. "
            "Export ClickHouse tables first or run without --use-local-parquet."
        )

    logger.info("Reading local Parquet: %s", LOCAL_PARQUET_DIR)

    def _naive_ts(dt) -> pd.Timestamp:
        """Strip timezone for pyarrow filter compatibility (R28).

        pyarrow raises ArrowNotImplementedError when filter bounds are tz-aware
        but the Parquet column schema is tz-naive (the common ClickHouse export
        format).  For a tz-naive Timestamp tz_localize(None) is a no-op; for a
        tz-aware Timestamp we use replace(tzinfo=None) to preserve local-time
        representation without unit conversion.
        """
        ts = pd.Timestamp(dt)
        return ts.tz_localize(None) if ts.tzinfo is None else ts.replace(tzinfo=None)

    # Use pyarrow pushdown filters to avoid loading the full table per chunk (R26).
    bets_lo = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
    bets = pd.read_parquet(
        bets_path,
        filters=[
            ("payout_complete_dtm", ">=", _naive_ts(bets_lo)),
            ("payout_complete_dtm", "<",  _naive_ts(extended_end)),
        ],
    )
    sessions = pd.read_parquet(
        sess_path,
        filters=[
            ("session_start_dtm", ">=", _naive_ts(window_start - timedelta(days=1))),
            ("session_start_dtm", "<",  _naive_ts(extended_end + timedelta(days=1))),
        ],
    )

    # DQ filters are applied fully in apply_dq; do a quick wager guard here
    bets = bets[bets.get("wager", pd.Series(dtype=float)).fillna(0) > 0].copy() if "wager" in bets.columns else bets

    sessions = sessions[
        (sessions.get("is_deleted", pd.Series(0, index=sessions.index)) == 0)
        & (sessions.get("is_canceled", pd.Series(0, index=sessions.index)) == 0)
    ].copy() if len(sessions) > 0 else sessions

    logger.info("Local Parquet: %d bets, %d sessions", len(bets), len(sessions))
    return bets, sessions


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
    """
    # --- bets ---
    bets = bets.copy()
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

    # Boundary comparison (tz-naive on both sides after normalization above)
    _lo = bets_history_start if bets_history_start is not None else window_start
    _lo = _lo.replace(tzinfo=None) if getattr(_lo, "tzinfo", None) else _lo
    _hi = extended_end.replace(tzinfo=None) if getattr(extended_end, "tzinfo", None) else extended_end

    bets = bets[
        bets["payout_complete_dtm"].between(_lo, _hi, inclusive="left")
        & (bets["wager"].fillna(0) > 0)
        & bets["payout_complete_dtm"].notna()
    ].copy()

    for col in ("bet_id", "session_id", "player_id", "table_id"):
        bets[col] = pd.to_numeric(bets.get(col), errors="coerce")
    bets = bets.dropna(subset=["bet_id", "session_id"]).copy()

    # E4/F1: drop sentinel placeholder player_id rows (R37)
    if "player_id" in bets.columns:
        bets = bets[bets["player_id"] != PLACEHOLDER_PLAYER_ID].copy()

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

    # --- sessions ---
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

    # Ensure num_games_with_wager exists
    if "num_games_with_wager" not in sessions.columns:
        sessions["num_games_with_wager"] = 0
    # Sentinel boolean flags
    for flag in ("is_manual", "is_deleted", "is_canceled"):
        if flag not in sessions.columns:
            sessions[flag] = 0

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
        bets["table_hc"] = 0
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

    # table_hc (each row's own payout time is the per-row cutoff; pass None
    # so the function uses per-row times; the global cutoff is enforced via
    # the bets having already been filtered to <= extended_end upstream)
    df["table_hc"] = compute_table_hc(df, cutoff_time=window_end)

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


def _chunk_cache_key(chunk: dict, bets: pd.DataFrame) -> str:
    """Hash to detect stale parquet cache (TRN-07)."""
    ws = chunk["window_start"].isoformat()
    we = chunk["window_end"].isoformat()
    data_hash = hashlib.md5(
        pd.util.hash_pandas_object(bets, index=False).values.tobytes()
    ).hexdigest()[:8]
    return f"{ws}|{we}|{data_hash}"


def run_track_a_dfs(
    bets: pd.DataFrame,
    sessions: pd.DataFrame,
    canonical_map: pd.DataFrame,
    window_end: datetime,
    sample_frac: float = 0.1,
    max_depth: int = 2,
) -> None:
    """Run Featuretools DFS on sampled bets and persist feature definitions (DEC-002 Phase 1).

    Call once before the main process_chunk loops to produce saved_feature_defs.
    Subsequent process_chunk calls automatically pick up the saved definitions via
    compute_feature_matrix and merge Track A features into the labeled DataFrame.
    """
    FEATURE_DEFS_DIR.mkdir(parents=True, exist_ok=True)
    sample = bets.sample(frac=min(sample_frac, 1.0), random_state=42) if len(bets) > 1 else bets
    cutoff_df = sample[["bet_id"]].copy()
    cutoff_df["cutoff_time"] = window_end
    es = build_entity_set(sample, sessions, canonical_map)
    _, feature_defs = run_dfs_exploration(es, cutoff_df, max_depth=max_depth)
    save_feature_defs(feature_defs, FEATURE_DEFS_DIR / "feature_defs.json")
    logger.info(
        "Track A: saved %d feature definitions to %s",
        len(feature_defs),
        FEATURE_DEFS_DIR,
    )


def process_chunk(
    chunk: dict,
    canonical_map: pd.DataFrame,
    use_local_parquet: bool = False,
    force_recompute: bool = False,
) -> Optional[Path]:
    """Process one monthly chunk; return path to written Parquet or None if empty.

    The canonical_map is built once at the global level (cutoff = training end)
    and passed in here.  Phase 2 should use per-chunk PIT mapping.
    """
    window_start = chunk["window_start"]
    window_end = chunk["window_end"]
    extended_end = chunk["extended_end"]
    chunk_path = _chunk_parquet_path(chunk)

    # --- Load data ---
    if use_local_parquet:
        bets_raw, sessions_raw = load_local_parquet(window_start, extended_end)
    else:
        bets_raw, sessions_raw = load_clickhouse_data(window_start, extended_end)

    if bets_raw.empty:
        logger.warning("Chunk %s–%s: no bets, skipping", window_start.date(), window_end.date())
        return None

    # --- TRN-07: cache validity ---
    if not force_recompute and chunk_path.exists():
        try:
            cached = pd.read_parquet(chunk_path)
            logger.info(
                "Chunk %s–%s: cache hit (%d rows)",
                window_start.date(), window_end.date(), len(cached),
            )
            return chunk_path
        except Exception:
            logger.warning("Chunk %s–%s: cache corrupt, recomputing", window_start.date(), window_end.date())

    # --- DQ --- (bets_history_start pulls HISTORY_BUFFER_DAYS of extra context for Track-B)
    history_start = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
    bets, sessions = apply_dq(
        bets_raw, sessions_raw, window_start, extended_end,
        bets_history_start=history_start,
    )
    if bets.empty:
        logger.warning("Chunk %s–%s: empty after DQ", window_start.date(), window_end.date())
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

    # --- Labels (C1 extended pull) ---
    labeled = compute_labels(
        bets_df=bets,
        window_end=window_end,
        extended_end=extended_end,
    )
    # H1: drop censored terminal bets — they cannot be reliably labelled
    labeled = labeled[~labeled["censored"]].copy()

    # Filter to training window — exclude historical context rows AND extended zone
    labeled = labeled[
        (labeled["payout_complete_dtm"] >= window_start)
        & (labeled["payout_complete_dtm"] < window_end)
    ].copy()
    if labeled.empty:
        logger.warning("Chunk %s–%s: empty after label filtering", window_start.date(), window_end.date())
        return None

    # --- Legacy (Track B) features ---
    labeled = add_legacy_features(labeled, sessions)

    # --- Track A: Featuretools DFS features (DEC-002/R45) ---
    # Applied only when saved_feature_defs are present (produced by run_track_a_dfs).
    # Missing defs are silently skipped so Track B can run independently.
    _feature_defs_path = FEATURE_DEFS_DIR / "feature_defs.json"
    if FEATURE_DEFS_DIR.exists() and _feature_defs_path.exists():
        try:
            _saved_defs = load_feature_defs(_feature_defs_path)
            _cutoff_df = labeled[["bet_id"]].copy()
            _cutoff_df["cutoff_time"] = window_end
            _es = build_entity_set(labeled, sessions, canonical_map)
            _fm = compute_feature_matrix(_es, _saved_defs, _cutoff_df)
            labeled = labeled.merge(
                _fm.reset_index(), on="bet_id", how="left", suffixes=("", "_track_a")
            )
            logger.info(
                "Chunk %s–%s: Track A merged (%d extra features)",
                window_start.date(),
                window_end.date(),
                len(_fm.columns),
            )
        except Exception as exc:
            logger.warning(
                "Chunk %s–%s: Track A skipped — %s",
                window_start.date(),
                window_end.date(),
                exc,
            )

    # Ensure all feature columns exist with numeric defaults
    for col in ALL_FEATURE_COLS:
        if col not in labeled.columns:
            labeled[col] = 0
    labeled[ALL_FEATURE_COLS] = labeled[ALL_FEATURE_COLS].fillna(0)

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
    return chunk_path


# ---------------------------------------------------------------------------
# Visit-level sample weights (SSOT §9.3)
# ---------------------------------------------------------------------------

def compute_sample_weights(df: pd.DataFrame) -> pd.Series:
    """Return sample_weight = 1 / N_visit for each row.

    N_visit = number of observations per (canonical_id, gaming_day) in ``df``.
    Only call this on the TRAINING set; never on valid/test (leakage guard).
    """
    if "gaming_day" not in df.columns or "canonical_id" not in df.columns:
        logger.warning("Cannot compute visit weights — missing canonical_id or gaming_day; using 1.0")
        return pd.Series(1.0, index=df.index)

    visit_key = df["canonical_id"].astype(str) + "_" + df["gaming_day"].astype(str)
    n_visit = visit_key.map(visit_key.value_counts())
    weights = (1.0 / n_visit).fillna(1.0)
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
    params = {**_base_lgb_params(), **hyperparams}
    model = lgb.LGBMClassifier(**params)
    model.fit(
        X_train,
        y_train,
        sample_weight=sw_train,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
    )
    val_scores = model.predict_proba(X_val)[:, 1]
    prauc = float(average_precision_score(y_val, val_scores)) if y_val.sum() > 0 else 0.0

    # Simple best-precision threshold search on validation set
    thresholds = np.unique(val_scores)
    best_t, best_prec, best_rec = 0.5, 0.0, 0.0
    for t in thresholds:
        preds = (val_scores >= t)
        if preds.sum() < 5:
            continue
        prec = float(precision_score(y_val, preds, zero_division=0))
        rec_val = float((preds & (y_val == 1)).sum()) / max(1, int(y_val.sum()))
        if rec_val < 0.02:
            continue
        if prec > best_prec or (prec == best_prec and rec_val > best_rec):
            best_prec, best_rec, best_t = prec, rec_val, float(t)

    metrics = {
        "label": label,
        "val_prauc": prauc,
        "val_precision": best_prec,
        "val_recall": best_rec,
        "threshold": best_t,
        "val_samples": int(len(y_val)),
        "val_positives": int(y_val.sum()),
        "best_hyperparams": hyperparams,
    }
    logger.info(
        "%s: PR-AUC=%.4f  prec=%.4f  rec=%.4f  thr=%.4f",
        label, prauc, best_prec, best_rec, best_t,
    )
    return model, metrics


def train_dual_model(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    feature_cols: List[str],
    run_optuna: bool = True,
) -> Tuple[Optional[dict], Optional[dict], dict]:
    """Train Rated + Non-rated LightGBM models.

    Returns
    -------
    (rated_artifacts, nonrated_artifacts, combined_metrics)
        Each artifacts dict: {"model": LGBMClassifier, "threshold": float, "metrics": dict}
    """
    def _split(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
        rated = df[df["is_rated"]].copy()
        nonrated = df[~df["is_rated"]].copy()
        return rated, nonrated

    train_rated, train_nonrated = _split(train_df)
    val_rated, val_nonrated = _split(valid_df)

    sw_rated = compute_sample_weights(train_rated)
    sw_nonrated = compute_sample_weights(train_nonrated)

    results: dict[str, Any] = {}
    for name, tr_df, vl_df, sw in [
        ("rated", train_rated, val_rated, sw_rated),
        ("nonrated", train_nonrated, val_nonrated, sw_nonrated),
    ]:
        if tr_df.empty:
            logger.warning("%s model: no training rows, skipping", name)
            results[name] = None
            continue

        avail_cols = [c for c in feature_cols if c in tr_df.columns]
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


# ---------------------------------------------------------------------------
# Artifact bundle
# ---------------------------------------------------------------------------

def save_artifact_bundle(
    rated: Optional[dict],
    nonrated: Optional[dict],
    feature_cols: List[str],
    combined_metrics: dict,
    model_version: str,
) -> None:
    """Write all model artifacts atomically.

    New dual-model format
    ---------------------
    models/rated_model.pkl        {"model", "threshold", "features"}
    models/nonrated_model.pkl     {"model", "threshold", "features"}
    models/feature_list.json      [{name, track}]
    models/model_version          <version string>
    models/training_metrics.json  per-model metrics

    Legacy single-model format (for backward compat with existing scorer)
    -----------------------------------------------------------------------
    models/walkaway_model.pkl     {"model", "features", "threshold"}
    """
    # New format
    if rated:
        joblib.dump(
            {"model": rated["model"], "threshold": rated["threshold"], "features": rated["features"]},
            MODEL_DIR / "rated_model.pkl",
        )
    if nonrated:
        joblib.dump(
            {"model": nonrated["model"], "threshold": nonrated["threshold"], "features": nonrated["features"]},
            MODEL_DIR / "nonrated_model.pkl",
        )

    feature_list = [
        {"name": c, "track": "B" if c in TRACK_B_FEATURE_COLS else "legacy"}
        for c in feature_cols
    ]
    (MODEL_DIR / "feature_list.json").write_text(
        json.dumps(feature_list, indent=2), encoding="utf-8"
    )
    (MODEL_DIR / "model_version").write_text(model_version, encoding="utf-8")
    (MODEL_DIR / "training_metrics.json").write_text(
        json.dumps({**combined_metrics, "model_version": model_version}, indent=2, default=str),
        encoding="utf-8",
    )

    # Legacy backward-compat: pick the better model (rated if available)
    legacy_source = rated or nonrated
    if legacy_source:
        joblib.dump(
            {
                "model": legacy_source["model"],
                "features": legacy_source["features"],
                "threshold": legacy_source["threshold"],
            },
            MODEL_DIR / "walkaway_model.pkl",
        )

    logger.info("Artifacts saved to %s  (version=%s)", MODEL_DIR, model_version)


# ---------------------------------------------------------------------------
# Main training pipeline
# ---------------------------------------------------------------------------

def run_pipeline(args) -> None:
    """Phase-1 training pipeline entry point."""
    start, end = parse_window(args)
    use_local = getattr(args, "use_local_parquet", False)
    force = getattr(args, "force_recompute", False)
    skip_optuna = getattr(args, "skip_optuna", False)

    logger.info("Training window: %s → %s  (local=%s)", start.date(), end.date(), use_local)

    # 1. Monthly chunks (DEC-008 / SSOT §4.3)
    chunks = get_monthly_chunks(start, end)
    logger.info("Chunks: %d", len(chunks))

    # 2. Determine train / valid / test split at chunk level FIRST (needed to
    #    derive train_end for canonical mapping cutoff — R25 / B1 leakage guard).
    split = get_train_valid_test_split(chunks)
    train_end = (
        max(c["window_end"] for c in split["train_chunks"])
        if split["train_chunks"] else end
    )
    train_ws = {c["window_start"] for c in split["train_chunks"]}
    valid_ws = {c["window_start"] for c in split["valid_chunks"]}
    test_ws  = {c["window_start"] for c in split["test_chunks"]}

    # 3. Build canonical mapping with TRAINING window cutoff (B1 — prevents
    #    identity links that arose after training from leaking into training data).
    logger.info("Building canonical identity mapping (cutoff=%s)…", train_end)
    if use_local:
        _, sessions_all = load_local_parquet(start, end + timedelta(days=1))
        _, sessions_all = apply_dq(
            pd.DataFrame(columns=["bet_id"]),  # dummy bets
            sessions_all, start, end + timedelta(days=1),
        )
    else:
        try:
            client = get_clickhouse_client()
            from identity import build_canonical_mapping  # type: ignore[import]
            canonical_map = build_canonical_mapping(client, cutoff_dtm=train_end)
        except Exception as exc:
            logger.warning("ClickHouse canonical mapping failed (%s); using empty map", exc)
            canonical_map = pd.DataFrame(columns=["player_id", "canonical_id"])
        sessions_all = None

    if sessions_all is not None:
        canonical_map = build_canonical_mapping_from_df(sessions_all, cutoff_dtm=train_end)

    logger.info("Canonical mapping: %d rows", len(canonical_map))

    # 4. Process chunks → write parquet
    chunk_paths = []
    for chunk in chunks:
        path = process_chunk(chunk, canonical_map, use_local_parquet=use_local, force_recompute=force)
        if path is not None:
            chunk_paths.append(path)

    if not chunk_paths:
        raise SystemExit("No chunks produced any usable data — check data source / time window")

    # 5. Load all chunks, concatenate
    all_dfs = [pd.read_parquet(p) for p in chunk_paths]
    full_df = pd.concat(all_dfs, ignore_index=True)
    logger.info("Total rows: %d  (label=1: %d)", len(full_df), int(full_df["label"].sum()))

    # 6. Assign train / valid / test split label to each row.
    #    R24: use year + month integer matching rather than Period conversion,
    #    which raises ValueError when payout_complete_dtm is tz-aware.
    _payout_ts = pd.to_datetime(full_df["payout_complete_dtm"])
    if _payout_ts.dt.tz is not None:
        _payout_ts = _payout_ts.dt.tz_localize(None)
    _chunk_year = _payout_ts.dt.year
    _chunk_month = _payout_ts.dt.month

    def _assign_split(year_s: pd.Series, month_s: pd.Series) -> pd.Series:
        def _label(ym: tuple) -> str:
            y, m = ym
            for s, tag in [(train_ws, "train"), (valid_ws, "valid"), (test_ws, "test")]:
                if any(y == x.year and m == x.month for x in s):
                    return tag
            return "train"  # fallback — should not happen with correct chunk coverage
        return pd.Series(
            [_label((y, m)) for y, m in zip(year_s, month_s)],
            index=year_s.index,
        )

    full_df["_split"] = _assign_split(_chunk_year, _chunk_month)

    train_df = full_df[full_df["_split"] == "train"].copy()
    valid_df  = full_df[full_df["_split"] == "valid"].copy()
    test_df   = full_df[full_df["_split"] == "test"].copy()
    logger.info(
        "Split — train: %d  valid: %d  test: %d",
        len(train_df), len(valid_df), len(test_df),
    )

    # 6. Train dual model (Optuna + visit-level sample_weight)
    model_version = get_model_version()
    rated_art, nonrated_art, combined_metrics = train_dual_model(
        train_df,
        valid_df,
        ALL_FEATURE_COLS,
        run_optuna=not skip_optuna,
    )

    # 7. Save artifacts
    save_artifact_bundle(rated_art, nonrated_art, ALL_FEATURE_COLS, combined_metrics, model_version)

    summary = {
        "model_version": model_version,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "total_rows": len(full_df),
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
        help="Read from .data/local/ Parquet instead of ClickHouse",
    )
    parser.add_argument(
        "--force-recompute", action="store_true",
        help="Ignore cached chunk Parquet files and recompute",
    )
    parser.add_argument(
        "--skip-optuna", action="store_true",
        help="Skip Optuna search and use default LightGBM hyperparameters",
    )
    args = parser.parse_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()
