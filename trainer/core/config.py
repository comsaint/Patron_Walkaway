import logging
import os
from pathlib import Path
from typing import Literal, Optional, Tuple

from dotenv import load_dotenv

_log = logging.getLogger(__name__)

# Repo root (this file lives in trainer/core/). Used for .env and default paths.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Unify .env loading for trainer, scorer, validator: always try to load so CH_USER/CH_PASS
# are available in production when STATE_DB_PATH/MODEL_DIR are set. override=False so we
# never overwrite existing env (e.g. deploy main.py already loaded from deploy root).
# Credential folder (PLAN): try credential/.env first, then repo root .env, then cwd.
# Code Review §1: wrap in try/except so process does not crash on permission/I/O errors.
try:
    _env_credential = _REPO_ROOT / "credential" / ".env"
    if _env_credential.is_file():
        load_dotenv(str(_env_credential), override=False)
    load_dotenv(_REPO_ROOT / ".env", override=False)
    load_dotenv(override=False)  # cwd (e.g. deploy root when started from there)
except Exception as e:
    _log.warning("could not load .env (credential/repo/cwd): %s", type(e).__name__)

NUMEXPR_MAX_THREADS = 12

# ------------------ ClickHouse (READ ONLY for now) ------------------
CH_HOST    = os.getenv("CH_HOST", "gdpedw")
CH_TEAMDB_HOST = os.getenv("CH_TEAMDB_HOST", "GAD10DMTDBSP21")
CH_PORT    = int(os.getenv("CH_PORT", 8123))
CH_USER    = os.getenv("CH_USER", "")
CH_PASS    = os.getenv("CH_PASS", "")
CH_PASSWORD = CH_PASS
CH_SECURE  = os.getenv("CH_SECURE", "False").lower() in ("true", "1", "t")
SOURCE_DB  = os.getenv("SOURCE_DB", "GDP_GMWDS_Raw")

# ------------------ Source tables ---------------------------------
TBET     = "t_bet"
TSESSION = "t_session"
TGAME    = "t_game"
TPROFILE = "player_profile"  # PIT/as-of profile snapshot table (DEC-011)

# ------------------ Time / locality -------------------------------
HK_TZ = "Asia/Hong_Kong"

# ------------------ Model/Backtest Time Window Defaults -----------
# All times are in HK time
TRAINER_DAYS = 7  # How many days back for training window (default)
BACKTEST_HOURS = 6  # How many hours back for backtest window (default)
BACKTEST_OFFSET_HOURS = 1  # How many hours before now to end backtest window
# DEC-027: Extra days of bet history before each chunk window_start (Track Human cross-chunk context).
HISTORY_BUFFER_DAYS: int = 2

# ------------------ Output paths (PLAN § Phase 2 前結構整理 項目 4) -----------------
# Default model and backtest output dirs under repo root out/ (PROJECT.md convention).
DEFAULT_MODEL_DIR: Path = _REPO_ROOT / "out" / "models"
DEFAULT_BACKTEST_OUT: Path = _REPO_ROOT / "out" / "backtest"

# ------------------ Phase 2 P1.1 Prediction log (PLAN T4) -----------------
# Independent SQLite for prediction log; set to empty string to disable.
PREDICTION_LOG_DB_PATH: str = os.getenv(
    "PREDICTION_LOG_DB_PATH",
    str(_REPO_ROOT / "local_state" / "prediction_log.db"),
)

# ------------------ Phase 2 P1.1 Export (PLAN T5) -----------------
# Only export rows with scored_at <= now - this lag (avoid in-flight data).
PREDICTION_EXPORT_SAFETY_LAG_MINUTES: int = int(
    os.getenv("PREDICTION_EXPORT_SAFETY_LAG_MINUTES", "5"),
)
# Max rows per export run (batch size).
PREDICTION_EXPORT_BATCH_ROWS: int = int(
    os.getenv("PREDICTION_EXPORT_BATCH_ROWS", "10000"),
)

# ------------------ Phase 2 P1.1 Prediction log retention (PLAN T6) -----------------
# After export, delete rows where prediction_id <= watermark AND scored_at < (now - this many days).
# Set to 0 to disable retention cleanup.
PREDICTION_LOG_RETENTION_DAYS: int = int(
    os.getenv("PREDICTION_LOG_RETENTION_DAYS", "30"),
)
# Batch size for each DELETE round (avoid long transaction).
PREDICTION_LOG_RETENTION_DELETE_BATCH: int = int(
    os.getenv("PREDICTION_LOG_RETENTION_DELETE_BATCH", "5000"),
)

# Sliding window (minutes) for prediction_log_summary aggregates (Unified Plan v2 T4). Set <= 0 to skip export.
PREDICTION_LOG_SUMMARY_WINDOW_MINUTES: int = int(
    os.getenv("PREDICTION_LOG_SUMMARY_WINDOW_MINUTES", "60"),
)

# ------------------ Runtime Retention -----------------------------
# Trim local CSV buffers to avoid unbounded growth
SCORER_ALERT_RETENTION_DAYS = 30      # Keep scorer alerts for last N days
SCORER_STATE_RETENTION_HOURS = 24     # Keep scorer SQLite state for this many hours
VALIDATOR_ALERT_RETENTION_DAYS = 30   # Keep alerts visible to validator for last N days
VALIDATION_RESULTS_RETENTION_DAYS = 180  # Keep validation_results history for last N days

# ------------------ Scorer poll defaults (SSOT for scorer CLI) -----------------
# Used by scorer.py --lookback-hours / --interval.
# Single source for Track Human lookback: trainer, backtester, and serving all use this
# (train–serve parity). TRAINER_USE_LOOKBACK has been removed (PLAN step 5).
SCORER_LOOKBACK_HOURS = 8       # Hours of bet history to pull each cycle (default 8)
SCORER_POLL_INTERVAL_SECONDS = 45  # Polling interval in seconds (includes run time)

# ------------------ Progress / UI (PLAN § progress-bars-long-steps) -----
# When True, disable tqdm progress bars (Step 6 chunks, Step 9 Optuna, etc.) for CI / non-TTY.
DISABLE_PROGRESS_BAR = False

# ------------------ Status Server -----------------------------
TABLE_STATUS_REFRESH_SECONDS = 45  # How often to refresh table occupancy snapshot
TABLE_STATUS_LOOKBACK_HOURS = 12   # Only consider sessions started within this many hours for status
TABLE_STATUS_RETENTION_HOURS = 24  # Keep status snapshots for this many hours in SQLite
TABLE_STATUS_HC_RETENTION_DAYS = 30  # Keep headcount history for this many days

# ------------------ Validator behavior -------------------------
# Whether to finalize a "PENDING" alert as a "MISS" once it reaches the
# horizon (default 45 minutes after the bet). This prevents alerts from
# remaining in PENDING indefinitely when no evidence of a walkaway arrives.
VALIDATOR_FINALIZE_ON_HORIZON = True
VALIDATOR_FINALIZE_MINUTES = 45  # Horizon minutes to finalize as MISS when enabled
# DEC-027: SSOT for validator timing (validator.py no longer relies on getattr defaults only).
VALIDATOR_FRESHNESS_BUFFER_MINUTES: int = 2   # Freshness buffer (minutes)
VALIDATOR_EXTENDED_WAIT_MINUTES: int = 15     # Extended wait before finalize (minutes)
VALIDATOR_FINALITY_HOURS: int = 1             # Cutoff (hours) for finality

# ============================================================
# Phase 1 — Walkaway Model Constants (SSOT v10)
# ============================================================
# v10: Single Rated model only. No nonrated_threshold constant
# (nonrated observations are volume-logged only; DEC-009/010).

# --- Business parameters ---
WALKAWAY_GAP_MIN = 30            # X: gap duration that qualifies as a walkaway (min)
ALERT_HORIZON_MIN = 15           # Y: prediction window before gap start (min)
LABEL_LOOKAHEAD_MIN = WALKAWAY_GAP_MIN + ALERT_HORIZON_MIN  # X + Y; C1 extended pull

# --- Data availability delays (SSOT §4.2) ---
BET_AVAIL_DELAY_MIN = 1          # t_bet available ~1 min after payout_complete_dtm
SESSION_AVAIL_DELAY_MIN = 7      # t_session: ~7 min after session_end (SSOT §4.2; use 15 for conservative)

# --- Run boundary ---
RUN_BREAK_MIN = WALKAWAY_GAP_MIN  # Gap >= this value starts a new betting run

# --- Gaming day / run dedup (G4) ---
# Primary: use the gaming_day column from t_bet/t_session.
# This constant is a fallback only (sensitivity analysis or missing gaming_day).
GAMING_DAY_START_HOUR = 6

# --- G1 threshold gate (DEPRECATED — rollback path only; DEC-009/010) ---
# Threshold selection now uses F-beta maximization (precision-weighted); G1 constraints removed.
# Do NOT import these in trainer.py or backtester.py.
# Restore only if explicitly rolling back to G1 strategy (see DEC-009 rollback note).
G1_PRECISION_MIN = 0.70          # [DEPRECATED] Minimum per-model precision
G1_ALERT_VOLUME_MIN_PER_HOUR = 5 # [DEPRECATED] Minimum combined alert volume/hour
G1_FBETA = 0.5                   # [DEPRECATED] F-beta weight (beta < 1 → precision-weighted)
OPTUNA_N_TRIALS = 150            # Optuna TPE trials for threshold search (DEC-009/010). A27: total Step 9 time scales with this.
# Optional Optuna time budget (seconds) for study.optimize. A27: tune to cap total HPO time.
# Disable timeout by setting to None or a non-positive value (e.g. -1).
OPTUNA_TIMEOUT_SECONDS: Optional[int] = 60 * 60 * 1 # -1 = no timeout, 10 * 60 = 10 minutes
# Optional study-level early stop: stop when best validation AP has not improved for
# this many consecutive trials. None = disabled (run full n_trials; default for reproducibility).
# Positive int (e.g. 40–60) = stop early to save time; recommend 40–60 to avoid stopping
# too soon when TPE has a dry spell (PLAN "Optuna 整份 study 的 early stop").
OPTUNA_EARLY_STOP_PATIENCE: Optional[int] = 40
# HPO subsampling (PLAN "Optuna HPO 階段 train/valid 抽樣"): max rows used for Optuna search only.
# A26: Set to subsample train/valid for HPO to reduce peak memory and trial time; final training uses full data.
# None = no subsampling. Positive int (e.g. 1_500_000) = cap train rows; valid same proportion.
OPTUNA_HPO_SAMPLE_ROWS: Optional[int] = 1500000
# Threshold selection objective (DEC-026): Optimize Precision at recall >= THRESHOLD_MIN_RECALL
# (trainer: argmax(pr_prec) over valid_mask; backtester: Optuna maximises precision).
# THRESHOLD_FBETA is still used for val_fbeta_05 / reporting only, not for choosing threshold.
THRESHOLD_FBETA: float = 0.5
THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL: float = 0.01  # DEC-026: target recall for precision optimisation

# --- Feature screening (DEC-020, PLAN screen-lgbm-default) ---
# Maximum number of features to keep after screen_features().
# None = no cap (all Stage-1 survivors kept); integer N = hard upper limit applied
# after Stage-1 (MI or LGBM ranking) and, if screen_method=mi_then_lgbm, after Stage-2 (LGBM ranking).
SCREEN_FEATURES_TOP_K: Optional[int] = 50
# Screening method: "lgbm" = LGBM-only (fast, no MI). A24: "mi"/"mi_then_lgbm" add mutual_info_classif (slower, more memory); prefer lgbm.
SCREEN_FEATURES_METHOD: Literal["lgbm", "mi", "mi_then_lgbm"] = "lgbm"

# --- Threshold selection guardrails (DEC-027: THRESHOLD_MIN_* naming) ---
# Minimum number of validation alerts required for a candidate threshold to be
# considered (DEC-026: during precision maximisation at recall >= MIN_RECALL).
# Small validation sets (e.g. --sample-rated) may require a lower value.
THRESHOLD_MIN_ALERT_COUNT: int = 5
# Optional threshold constraints (None disables each constraint).
# MIN_RECALL: both trainer and backtester require recall >= this when choosing threshold (DEC-026).
# Default 0.01 aligns with THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL (optimise Precision at recall=0.01).
# MIN_ALERTS_PER_HOUR is only meaningful in backtester where window_hours exists.
THRESHOLD_MIN_RECALL: Optional[float] = 0.01
THRESHOLD_MIN_ALERTS_PER_HOUR: Optional[float] = 1.0

# --- Track Human constants ---
TABLE_HC_WINDOW_MIN = 30         # Lookback window for table headcount feature (D1)
PLACEHOLDER_PLAYER_ID = -1       # Invalid player_id sentinel in t_bet (E4/F1)
UNRATED_VOLUME_LOG = True        # DEC-021: log unrated player/bet counts per poll cycle
LOSS_STREAK_PUSH_RESETS = False  # Whether PUSH resets the loss-streak counter (F4)
HIST_AVG_BET_CAP = 500_000       # Winsorization cap for avg_bet (F2; validate with EDA)

# --- Step 7 / Chunk 記憶體估計 (DEC-027 OOM 區塊) ---
# See doc/training_oom_and_runtime_audit.md for peak RAM formula and A19/A20.
# If total size of chunk Parquet files exceeds this, pipeline logs a RAM warning.
# Parquet files are heavily compressed; observed expansion from disk to in-memory
# DataFrame is ~10–15x (e.g. 1.2 GB on-disk → ~15 GB RAM for 27M rows × 73 cols).
# The Step 7 split further allocates the train split (~70%) while full_df is alive,
# pushing the true peak to ~20x on-disk.  Factor 15 is a conservative lower-bound.
# 1 GB: fire warning earlier so desktop users can reduce --days before Step 7 OOM.
CHUNK_CONCAT_MEMORY_WARN_BYTES = int(1 * (1024**3))  # 1 GB on-disk total
CHUNK_CONCAT_RAM_FACTOR = 15  # on-disk size × this × (1 + TRAIN_SPLIT_FRAC) ≈ Step 7 peak RAM

# --- Neg sampling 與 OOM 預檢 (DEC-027 OOM 區塊) ---
# When < 1.0: ALL label=1 rows are kept; label=0 rows are randomly downsampled
# to this fraction before writing each chunk Parquet.  Combined with
# class_weight='balanced' and per-run sample_weight, LightGBM compensates for
# the reduced negative count automatically.
# 1.0 = disabled (keep all negatives, original behaviour). When 1.0, the OOM
# pre-check (see below) may still auto-reduce the effective fraction if RAM looks tight.
# Recommended: 0.3 when training on 90+ days of data to avoid Step 7 OOM,
# or just leave it at 1.0 and let the OOM pre-check auto-adjust.
# Example: 30 days × 27M rows → ~10M rows with NEG_SAMPLE_FRAC=0.3.
NEG_SAMPLE_FRAC: float = 0.30

# --- Production class-ratio assumption (for adjusted test precision reporting) ---
# Expected negative-to-positive ratio in production (serving), used to adjust
# test set precision to a realistic estimate of serving precision when negatives
# have been downsampled during training.
# Set to None to disable adjusted-precision reporting.
# Example: 15.0 means production has ~15 negative observations per 1 positive.
PRODUCTION_NEG_POS_RATIO: Optional[float] = 87.0/13.0

# --- OOM pre-check: auto-adjust NEG_SAMPLE_FRAC after Step 1 if RAM looks tight ---
# After the chunk list is built, the pipeline estimates Step 7 peak RAM as
#   on_disk_total × CHUNK_CONCAT_RAM_FACTOR × (1 + TRAIN_SPLIT_FRAC)
# (full_df and train split coexist at peak). If that exceeds
#   ram_budget = available_ram × NEG_SAMPLE_RAM_SAFETY,
# it auto-computes a negative fraction: frac = (ram_budget/peak - pos_rate)/(1 - pos_rate),
# then clamps to [NEG_SAMPLE_FRAC_MIN, 1.0]. So "how much" is controlled by the
# constants below (and by CHUNK_CONCAT_RAM_FACTOR / TRAIN_SPLIT_FRAC), not a fixed value.
# Only triggers when NEG_SAMPLE_FRAC == 1.0 (user-configured values are respected).
NEG_SAMPLE_FRAC_AUTO: bool = True   # set False to disable auto-adjustment entirely
NEG_SAMPLE_FRAC_MIN: float = 0.05  # hard floor: auto-reduce will never go below this
# Assumed positive rate used in the auto-adjustment formula.
# Default 0.15 (15%) is conservative; lower your actual positive rate for a tighter bound.
NEG_SAMPLE_FRAC_ASSUMED_POS_RATE: float = 0.15
# Target: keep Step 7 estimated peak RAM within this fraction of *available* RAM.
# 0.75 = aim to use at most 75% of currently free RAM (leaves headroom for OS + other).
NEG_SAMPLE_RAM_SAFETY: float = 0.75
# Fallback per-chunk on-disk size estimate when no cached chunk Parquets exist (bytes).
# 200 MB is a rough estimate for a ~2–3M-row monthly chunk.
NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT: int = 200 * 1024 * 1024

# --- Row-level train/valid/test split ratios (SSOT §9.2, todo-row-level-time-split) ---
# Chunks control ETL/cache volume only; the actual split is applied at row level
# after concatenating all chunk Parquets, sorted by payout_complete_dtm.
TRAIN_SPLIT_FRAC = 0.70   # fraction of rows allocated to training
VALID_SPLIT_FRAC = 0.15   # fraction of rows allocated to validation; test = remainder
MIN_VALID_TEST_ROWS = 50  # warn if valid or test set falls below this count

# --- Profile ETL 記憶體 / Preload (DEC-027 OOM 區塊) ─────────────────────────
# When True, build_player_profile() reads the session Parquet directly in DuckDB
# instead of loading the full table into pandas memory.  Set False to force the
# pandas fallback path (useful for debugging or environments without DuckDB).
PROFILE_USE_DUCKDB: bool = True

# Threshold for the preload OOM guard in backfill().
# If the session Parquet on-disk size exceeds this, the preload is skipped and
# per-snapshot PyArrow pushdown is used instead.  Only applies to the pandas
# fallback path; the DuckDB path (PROFILE_USE_DUCKDB=True) never preloads.
# On 8/32GB machines use --no-preload or lower this value to avoid OOM (A05).
PROFILE_PRELOAD_MAX_BYTES: int = int(1.5 * 1024**3)  # 1.5 GB on disk

# --- DuckDB 共用（SSOT, DEC-027）---
# All DuckDB-using stages (profile ETL, Step 7, canonical mapping) share these
# defaults. Stage overrides below. Formula: limit = clamp(available_ram * FRACTION,
# MIN_GB, effective_MAX); effective_MAX = max(MAX_GB, available_ram * RAM_MAX_FRACTION)
# when RAM_MAX_FRACTION is set. See doc/training_oom_and_runtime_audit.md.
DUCKDB_RAM_FRACTION: float = 0.5
DUCKDB_MEMORY_LIMIT_MIN_GB: float = 1.0
DUCKDB_MEMORY_LIMIT_MAX_GB: float = 24.0
DUCKDB_RAM_MAX_FRACTION: Optional[float] = 0.45
DUCKDB_THREADS: int = 2
DUCKDB_PRESERVE_INSERTION_ORDER: bool = False

# Profile ETL override: heavier queries use a lower ceiling (others from DUCKDB_*).
PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB: float = 8.0
# Backward-compat aliases (DEC-027): tests/ETL may still read these; budget uses get_duckdb_memory_limit_bytes.
PROFILE_DUCKDB_RAM_FRACTION: float = DUCKDB_RAM_FRACTION
PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB: float = DUCKDB_MEMORY_LIMIT_MIN_GB
PROFILE_DUCKDB_RAM_MAX_FRACTION: Optional[float] = DUCKDB_RAM_MAX_FRACTION
PROFILE_DUCKDB_THREADS: int = DUCKDB_THREADS
PROFILE_DUCKDB_PRESERVE_INSERTION_ORDER: bool = DUCKDB_PRESERVE_INSERTION_ORDER

# --- Pipeline Step 7/8/9 與方案 B/B+ 開關 (DEC-027) ---
# DuckDB 記憶體預算改由 DUCKDB_* 共用常數 + get_duckdb_memory_config("step7") 控制。
# Step 7 DuckDB out-of-core sort (OOM-safe; PLAN Step 7 Out-of-Core, A19):
# When True, Step 7 uses DuckDB to sort and split chunk Parquets (spills to disk
# when over memory_limit). When False, pandas fallback (high OOM risk). See A19.
STEP7_USE_DUCKDB: bool = True
# Step 7 overrides: only temp dir and threads (memory from shared DUCKDB_*).
STEP7_DUCKDB_THREADS: int = 4
# Temp directory for DuckDB spill; None = caller uses DATA_DIR / "duckdb_tmp". A20.
STEP7_DUCKDB_TEMP_DIR: Optional[str] = None
# Backward-compat aliases (DEC-027): tests may still expect these on config.
STEP7_DUCKDB_RAM_FRACTION: float = DUCKDB_RAM_FRACTION
STEP7_DUCKDB_RAM_MIN_GB: float = DUCKDB_MEMORY_LIMIT_MIN_GB
STEP7_DUCKDB_RAM_MAX_GB: float = DUCKDB_MEMORY_LIMIT_MAX_GB
STEP7_DUCKDB_PRESERVE_INSERTION_ORDER: bool = DUCKDB_PRESERVE_INSERTION_ORDER


# --- Plan B+: Step 7 keep train on disk (PLAN 方案 B+ 階段 1–2) ---
# When True and DuckDB succeeds, Step 7 does not load train into memory; only valid/test
# are loaded. Step 8 then samples from train Parquet (first N rows) for screening; after
# screening train is loaded once for export/Step 9. Reduces peak RAM between Step 7 and Step 8.
# Requires STEP7_USE_DUCKDB=True; on DuckDB failure we raise (no pandas fallback) per PLAN.
STEP7_KEEP_TRAIN_ON_DISK: bool = True

# --- Plan B+: LibSVM export (PLAN 方案 B+ 階段 3) ---
# When True and step7_train_path is set (B+ path), stream export from Parquet to
# train_for_lgb.libsvm + train_for_lgb.libsvm.weight and valid_for_lgb.libsvm
# before loading train into memory. Requires STEP7_KEEP_TRAIN_ON_DISK and split paths.
STEP9_EXPORT_LIBSVM: bool = True

# --- Plan B: train from file (PLAN 方案 B) ---
# When True, Step 9 will train from on-disk CSV/TSV (or equivalent) instead of
# loading full train_df into memory. Requires export + Booster path (not yet implemented).
# Default False until the full path is implemented and validated.
STEP9_TRAIN_FROM_FILE: bool = True

# --- Plan B+ stage 5 (PLAN 方案 B+ 階段 5)：optional .bin for LibSVM path ---
# When True and training from LibSVM (train_libsvm_paths), save dtrain to
# train_for_lgb.bin after first build; on next run use .bin if present (faster I/O).
STEP9_SAVE_LGB_BINARY: bool = True
# When set (e.g. 2_000_000), Step 8 feature screening uses only this many rows from
# train (strategy A: sample-based screening to avoid loading full train). None = use
# full train for screening (current behaviour). A23: For in-memory path suggest 2_000_000;
# B+ path already defaults to 2M. If set to an integer, must be > 0; 0 or negative invalid.
STEP8_SCREEN_SAMPLE_ROWS: Optional[int] = None

# --- Canonical mapping: DuckDB path (PLAN Canonical mapping 全歷史 Step 1) ---
# Memory from DUCKDB_* shared; override threads only (low RAM priority for full-history scan).
CANONICAL_MAP_DUCKDB_THREADS: int = 1
# When True, skip DuckDB path and build mapping from full sessions in pandas (debug only; A03).
CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS: bool = False
# Backward-compat aliases (DEC-027): tests may still expect these on config.
CANONICAL_MAP_DUCKDB_RAM_FRACTION: float = DUCKDB_RAM_FRACTION
CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MIN_GB: float = DUCKDB_MEMORY_LIMIT_MIN_GB
CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB: float = DUCKDB_MEMORY_LIMIT_MAX_GB

# --- DuckDB helper: single SSOT for all stages (DEC-027) ---
def get_duckdb_memory_config(
    stage: Literal["profile", "step7", "canonical_map"],
) -> Tuple[float, float, float, Optional[float], int, bool, Optional[str]]:
    """Return (frac, min_gb, max_gb, ram_max_frac, threads, preserve_order, temp_dir).

    Callers use this + available_ram to compute memory_limit and SET runtime.
    """
    if stage not in ("profile", "step7", "canonical_map"):
        raise ValueError(
            "stage must be 'profile', 'step7', or 'canonical_map', got %r" % (stage,)
        )
    frac = DUCKDB_RAM_FRACTION
    min_gb = DUCKDB_MEMORY_LIMIT_MIN_GB
    max_gb = DUCKDB_MEMORY_LIMIT_MAX_GB
    ram_max_frac = DUCKDB_RAM_MAX_FRACTION
    threads = DUCKDB_THREADS
    preserve_order = DUCKDB_PRESERVE_INSERTION_ORDER
    temp_dir: Optional[str] = None
    if stage == "profile":
        max_gb = PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB
    elif stage == "step7":
        threads = STEP7_DUCKDB_THREADS
        temp_dir = STEP7_DUCKDB_TEMP_DIR
    elif stage == "canonical_map":
        threads = CANONICAL_MAP_DUCKDB_THREADS
    return (frac, min_gb, max_gb, ram_max_frac, threads, preserve_order, temp_dir)


def get_duckdb_memory_limit_bytes(
    stage: Literal["profile", "step7", "canonical_map"],
    available_bytes: Optional[int],
) -> int:
    """Compute DuckDB memory_limit in bytes. Uses get_duckdb_memory_config(stage).

    When available_bytes is None or negative, returns min in bytes (safe fallback).
    Validates frac in (0, 1]; if min_gb > max_gb they are swapped.
    DEC-027 Review: min/max forced positive; negative available_bytes treated as None; optional 1 TB cap.
    """
    frac, min_gb, max_gb, ram_max_frac, _t, _p, _d = get_duckdb_memory_config(stage)
    if not (0.0 < frac <= 1.0):
        _log.warning(
            "DUCKDB_RAM_FRACTION=%.3f out of valid range (0, 1]; using 0.5",
            frac,
        )
        frac = 0.5
    _min = int(min_gb * 1024**3)
    _max = int(max_gb * 1024**3)
    if _min <= 0:
        _log.warning(
            "DUCKDB MEMORY_LIMIT MIN_GB (%.2f) <= 0; using 0.1 GB floor",
            min_gb,
        )
        _min = max(1, int(0.1 * 1024**3))
    if _max <= 0:
        _log.warning(
            "DUCKDB MEMORY_LIMIT MAX_GB (%.2f) <= 0; using _min",
            max_gb,
        )
        _max = _min
    if _min > _max:
        _log.warning(
            "DUCKDB MEMORY_LIMIT MIN_GB (%.2f) > MAX_GB (%.2f); swapping",
            min_gb,
            max_gb,
        )
        _min, _max = _max, _min
    _max_cap = 1024 * 1024**3  # 1 TB (DEC-027 Review #8 optional cap)
    if _max > _max_cap:
        _log.warning(
            "DUCKDB MEMORY_LIMIT MAX_GB capped at 1024 (1 TB); was %.2f",
            _max / 1024**3,
        )
        _max = _max_cap
        _min = min(_min, _max)
    if available_bytes is None:
        return _min
    if available_bytes < 0:
        _log.warning(
            "get_duckdb_memory_limit_bytes: available_bytes < 0; treating as None (return _min)",
        )
        return _min
    effective_max = _max
    if ram_max_frac is not None and 0.0 < ram_max_frac <= 1.0:
        effective_max = max(_max, int(available_bytes * ram_max_frac))
    # DEC-027 Review #8: apply same 1 TB cap to effective_max (e.g. step7 ram_max_frac path)
    effective_max = min(effective_max, _max_cap)
    budget = int(available_bytes * frac)
    return max(_min, min(effective_max, budget))

# --- SQL fragment shared across all modules (FND-03) ---
CASINO_PLAYER_ID_CLEAN_SQL = (
    "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
    "THEN NULL ELSE trim(casino_player_id) END"
)