import os
from typing import Optional

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
OPTUNA_TIMEOUT_SECONDS: Optional[int] = 10 * 60
# Threshold selection objective: F-beta with beta < 1 favours precision over recall.
THRESHOLD_FBETA: float = 0.5

# --- Feature screening (DEC-020) ---
# Maximum number of features to keep after screen_features().
# None = no cap (all Stage-1 survivors kept); integer N = hard upper limit applied
# after Stage-1 (MI ranking) and, if use_lgbm=True, after Stage-2 (LGBM ranking).
SCREEN_FEATURES_TOP_K: Optional[int] = None

# --- Threshold selection guardrails ---
# Minimum number of validation alerts required for a candidate threshold to be
# considered during F-beta maximisation.  Small validation sets (e.g. --sample-rated)
# may require a lower value; large sets may warrant a higher one.
MIN_THRESHOLD_ALERT_COUNT = 5
# Optional threshold constraints (None disables each constraint).
# MIN_RECALL applies to both trainer threshold scan and backtester Optuna search.
# Safeguard: default 0.01 enforces minimum 1% recall when choosing threshold.
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

# --- SQL fragment shared across all modules (FND-03) ---
CASINO_PLAYER_ID_CLEAN_SQL = (
    "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') "
    "THEN NULL ELSE trim(casino_player_id) END"
)