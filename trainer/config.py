import os
from typing import Literal, Optional

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

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

# ------------------ Runtime Retention -----------------------------
# Trim local CSV buffers to avoid unbounded growth
SCORER_ALERT_RETENTION_DAYS = 30      # Keep scorer alerts for last N days
SCORER_STATE_RETENTION_HOURS = 24     # Keep scorer SQLite state for this many hours
VALIDATOR_ALERT_RETENTION_DAYS = 30   # Keep alerts visible to validator for last N days
VALIDATION_RESULTS_RETENTION_DAYS = 180  # Keep validation_results history for last N days

# ------------------ Scorer poll defaults (SSOT for scorer CLI) -----------------
# Used by scorer.py --lookback-hours / --interval. Trainer can align Track Human/LLM
# to SCORER_LOOKBACK_HOURS for train–serve parity.
SCORER_LOOKBACK_HOURS = 8       # Hours of bet history to pull each cycle
SCORER_POLL_INTERVAL_SECONDS = 45  # Polling interval in seconds (includes run time)

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
OPTUNA_N_TRIALS = 300            # Optuna TPE trials for threshold search (DEC-009/010)
# Optional Optuna time budget (seconds) for study.optimize.
# Disable timeout by setting to None or a non-positive value (e.g. -1).
OPTUNA_TIMEOUT_SECONDS: Optional[int] = -1  # -1 = no timeout, 10 * 60 = 10 minutes
# Optional study-level early stop: stop when best validation AP has not improved for
# this many consecutive trials. None = disabled (run full n_trials; default for reproducibility).
# Positive int (e.g. 40–60) = stop early to save time; recommend 40–60 to avoid stopping
# too soon when TPE has a dry spell (PLAN "Optuna 整份 study 的 early stop").
OPTUNA_EARLY_STOP_PATIENCE: Optional[int] = None
# Threshold selection objective (DEC-026): Optimize Precision at recall >= THRESHOLD_MIN_RECALL
# (trainer: argmax(pr_prec) over valid_mask; backtester: Optuna maximises precision).
# THRESHOLD_FBETA is still used for val_fbeta_05 / reporting only, not for choosing threshold.
THRESHOLD_FBETA: float = 0.5
THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL: float = 0.01  # DEC-026: target recall for precision optimisation

# --- Feature screening (DEC-020, PLAN screen-lgbm-default) ---
# Maximum number of features to keep after screen_features().
# None = no cap (all Stage-1 survivors kept); integer N = hard upper limit applied
# after Stage-1 (MI or LGBM ranking) and, if screen_method=mi_then_lgbm, after Stage-2 (LGBM ranking).
SCREEN_FEATURES_TOP_K: Optional[int] = None
# Screening method: "lgbm" = LGBM-only (fast, no MI); "mi" = original MI path; "mi_then_lgbm" = MI then LGBM re-rank (original use_lgbm=True).
SCREEN_FEATURES_METHOD: Literal["lgbm", "mi", "mi_then_lgbm"] = "lgbm"

# --- Threshold selection guardrails ---
# Minimum number of validation alerts required for a candidate threshold to be
# considered (DEC-026: during precision maximisation at recall >= MIN_RECALL).
# Small validation sets (e.g. --sample-rated) may require a lower value.
MIN_THRESHOLD_ALERT_COUNT = 5
# Optional threshold constraints (None disables each constraint).
# MIN_RECALL: both trainer and backtester require recall >= this when choosing threshold (DEC-026).
# Default 0.01 aligns with THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL (optimise Precision at recall=0.01).
# MIN_ALERTS_PER_HOUR is only meaningful in backtester where window_hours exists.
THRESHOLD_MIN_RECALL: Optional[float] = 0.01
THRESHOLD_MIN_ALERTS_PER_HOUR: Optional[float] = 1.0

# --- Track B constants ---
TABLE_HC_WINDOW_MIN = 30         # Lookback window for table headcount feature (D1)
PLACEHOLDER_PLAYER_ID = -1       # Invalid player_id sentinel in t_bet (E4/F1)
UNRATED_VOLUME_LOG = True        # DEC-021: log unrated player/bet counts per poll cycle
LOSS_STREAK_PUSH_RESETS = False  # Whether PUSH resets the loss-streak counter (F4)
HIST_AVG_BET_CAP = 500_000       # Winsorization cap for avg_bet (F2; validate with EDA)

# --- Chunk concat memory guard (OOM risk when loading all chunk Parquets) ---
# If total size of chunk Parquet files exceeds this, pipeline logs a RAM warning.
# Parquet files are heavily compressed; observed expansion from disk to in-memory
# DataFrame is ~10–15x (e.g. 1.2 GB on-disk → ~15 GB RAM for 27M rows × 73 cols).
# The Step 7 split further allocates the train split (~70%) while full_df is alive,
# pushing the true peak to ~20x on-disk.  Factor 15 is a conservative lower-bound.
# 1 GB: fire warning earlier so desktop users can reduce --days before Step 7 OOM.
CHUNK_CONCAT_MEMORY_WARN_BYTES = int(1 * (1024**3))  # 1 GB on-disk total
CHUNK_CONCAT_RAM_FACTOR = 15  # on-disk size × this × (1 + TRAIN_SPLIT_FRAC) ≈ Step 7 peak RAM

# --- Per-chunk negative downsampling (OOM mitigation for long training windows) ---
# When < 1.0: ALL label=1 rows are kept; label=0 rows are randomly downsampled
# to this fraction before writing each chunk Parquet.  Combined with
# class_weight='balanced' and per-run sample_weight, LightGBM compensates for
# the reduced negative count automatically.
# 1.0 = disabled (keep all negatives, original behaviour). When 1.0, the OOM
# pre-check (see below) may still auto-reduce the effective fraction if RAM looks tight.
# Recommended: 0.3 when training on 90+ days of data to avoid Step 7 OOM,
# or just leave it at 1.0 and let the OOM pre-check auto-adjust.
# Example: 30 days × 27M rows → ~10M rows with NEG_SAMPLE_FRAC=0.3.
NEG_SAMPLE_FRAC: float = 1.0

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

# ── player_profile ETL (OPT-002) ──────────────────────────────────────────────
# When True, build_player_profile() reads the session Parquet directly in DuckDB
# instead of loading the full table into pandas memory.  Set False to force the
# pandas fallback path (useful for debugging or environments without DuckDB).
PROFILE_USE_DUCKDB: bool = True

# Threshold for the preload OOM guard in backfill().
# If the session Parquet on-disk size exceeds this, the preload is skipped and
# per-snapshot PyArrow pushdown is used instead.  Only applies to the pandas
# fallback path; the DuckDB path (PROFILE_USE_DUCKDB=True) never preloads.
PROFILE_PRELOAD_MAX_BYTES: int = int(1.5 * 1024**3)  # 1.5 GB on disk

# --- DuckDB runtime memory budget (player_profile ETL DuckDB path) ---
# _configure_duckdb_runtime() in etl_player_profile.py reads these constants
# at execution time and applies them via SET statements immediately after the
# DuckDB connection is opened.  The memory limit is computed dynamically:
#   limit = clamp(available_ram * FRACTION, MIN_GB, MAX_GB)
# This avoids a hard-coded value while still being portable across machines.
#
# FRACTION  – how much of currently available RAM DuckDB may use (0–1).
#             0.5 leaves the other half for Python, OS, and the pandas fallback.
# MIN_GB    – floor: prevents an absurdly small limit on very low-RAM machines
#             (the query will OOM anyway, but at least it fails fast).
# MAX_GB    – ceiling: prevents a single DuckDB query from monopolising RAM on
#             high-memory servers where 50% could be many tens of GB.
# THREADS   – DuckDB worker threads; lower = less peak RAM, slower sort/hash.
#             2 is a safe default for laptops; raise on dedicated servers.
# PRESERVE_INSERTION_ORDER – DuckDB default is True (sort output to match
#             insertion order), which costs extra RAM.  Profile aggregation
#             output order is non-deterministic anyway, so False is safe here.
# RAM_MAX_FRACTION – When set (e.g. 0.45), effective ceiling = max(MAX_GB,
#             available_ram * RAM_MAX_FRACTION).  High-RAM machines get a
#             higher DuckDB limit, reducing OOM.  None = use only MAX_GB.
PROFILE_DUCKDB_RAM_FRACTION: float = 0.5
PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB: float = 0.5
PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB: float = 8.0
PROFILE_DUCKDB_RAM_MAX_FRACTION: Optional[float] = 0.45
PROFILE_DUCKDB_THREADS: int = 2
PROFILE_DUCKDB_PRESERVE_INSERTION_ORDER: bool = False

# --- Step 7 DuckDB out-of-core sort (OOM-safe; PLAN Step 7 Out-of-Core) ---
# When True, Step 7 uses DuckDB to sort and split chunk Parquets (spills to disk
# when over memory_limit), avoiding pandas concat+sort peak RAM. When False or
# on DuckDB error, fall back to pandas concat + sort + split.
STEP7_USE_DUCKDB: bool = True
STEP7_DUCKDB_RAM_FRACTION: float = 0.50
STEP7_DUCKDB_RAM_MIN_GB: float = 2.0
STEP7_DUCKDB_RAM_MAX_GB: float = 24.0
STEP7_DUCKDB_THREADS: int = 4
STEP7_DUCKDB_PRESERVE_INSERTION_ORDER: bool = False
# Temp directory for DuckDB spill; None = caller uses DATA_DIR / "duckdb_tmp"
STEP7_DUCKDB_TEMP_DIR: Optional[str] = None

# --- Plan B+: Step 7 keep train on disk (PLAN 方案 B+ 階段 1–2) ---
# When True and DuckDB succeeds, Step 7 does not load train into memory; only valid/test
# are loaded. Step 8 then samples from train Parquet (first N rows) for screening; after
# screening train is loaded once for export/Step 9. Reduces peak RAM between Step 7 and Step 8.
# Requires STEP7_USE_DUCKDB=True; on DuckDB failure we raise (no pandas fallback) per PLAN.
STEP7_KEEP_TRAIN_ON_DISK: bool = False

# --- Plan B+: LibSVM export (PLAN 方案 B+ 階段 3) ---
# When True and step7_train_path is set (B+ path), stream export from Parquet to
# train_for_lgb.libsvm + train_for_lgb.libsvm.weight and valid_for_lgb.libsvm
# before loading train into memory. Requires STEP7_KEEP_TRAIN_ON_DISK and split paths.
STEP9_EXPORT_LIBSVM: bool = False

# --- Plan B: train from file (PLAN 方案 B) ---
# When True, Step 9 will train from on-disk CSV/TSV (or equivalent) instead of
# loading full train_df into memory. Requires export + Booster path (not yet implemented).
# Default False until the full path is implemented and validated.
STEP9_TRAIN_FROM_FILE: bool = False

# --- Plan B+ stage 5 (PLAN 方案 B+ 階段 5)：optional .bin for LibSVM path ---
# When True and training from LibSVM (train_libsvm_paths), save dtrain to
# train_for_lgb.bin after first build; on next run use .bin if present (faster I/O).
STEP9_SAVE_LGB_BINARY: bool = False
# When set (e.g. 2_000_000), Step 8 feature screening uses only this many rows from
# train (strategy A: sample-based screening to avoid loading full train). None = use
# full train for screening (current behaviour). If set to an integer, must be > 0;
# 0 or negative is invalid (treat as None when implementing Step 8).
STEP8_SCREEN_SAMPLE_ROWS: Optional[int] = None

# --- Canonical mapping: DuckDB path (PLAN Canonical mapping 全歷史 Step 1) ---
# When building canonical mapping from local Parquet, DuckDB is used to scan full
# session history (COALESCE(session_end_dtm, lud_dtm) <= train_end) with limited RAM.
# memory_limit = available_ram × RAM_FRACTION, clamped to [MIN_GB, MAX_GB] (PLAN Canonical mapping DuckDB 對齊 Step 7).
# THREADS: worker threads; lower = less peak RAM, slower.
CANONICAL_MAP_DUCKDB_RAM_FRACTION: float = 0.45
CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MIN_GB: float = 1.0
CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB: float = 24.0
CANONICAL_MAP_DUCKDB_THREADS: int = 1
# When True, skip DuckDB path and build mapping from full sessions in pandas (debug only;
# may OOM on large history). PLAN: CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS.
CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS: bool = False

# --- SQL fragment shared across all modules (FND-03) ---
CASINO_PLAYER_ID_CLEAN_SQL = (
    "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
    "THEN NULL ELSE trim(casino_player_id) END"
)