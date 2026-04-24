import logging
import math
import os
from pathlib import Path
from typing import Literal, Optional, Tuple, cast

from dotenv import load_dotenv
from trainer.core._config_duckdb_memory import (
    CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB,
    CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MIN_GB,
    CANONICAL_MAP_DUCKDB_RAM_FRACTION,
    CANONICAL_MAP_DUCKDB_THREADS,
    DUCKDB_MEMORY_LIMIT_MAX_GB,
    DUCKDB_MEMORY_LIMIT_MIN_GB,
    DUCKDB_PRESERVE_INSERTION_ORDER,
    DUCKDB_RAM_FRACTION,
    DUCKDB_RAM_MAX_FRACTION,
    DUCKDB_THREADS,
    PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB,
    PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB,
    PROFILE_DUCKDB_PRESERVE_INSERTION_ORDER,
    PROFILE_DUCKDB_RAM_FRACTION,
    PROFILE_DUCKDB_RAM_MAX_FRACTION,
    PROFILE_DUCKDB_THREADS,
    STEP7_DUCKDB_PRESERVE_INSERTION_ORDER,
    STEP7_DUCKDB_RAM_FRACTION,
    STEP7_DUCKDB_RAM_MAX_GB,
    STEP7_DUCKDB_RAM_MIN_GB,
    STEP7_DUCKDB_TEMP_DIR,
    STEP7_DUCKDB_THREADS,
)
from trainer.core._config_training_memory import (
    CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS,
    CHUNK_CONCAT_MEMORY_WARN_BYTES,
    CHUNK_CONCAT_RAM_FACTOR,
    MIN_VALID_TEST_ROWS,
    NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT,
    NEG_SAMPLE_FRAC,
    NEG_SAMPLE_FRAC_ASSUMED_POS_RATE,
    NEG_SAMPLE_FRAC_AUTO,
    NEG_SAMPLE_FRAC_MIN,
    NEG_SAMPLE_RAM_SAFETY,
    PROFILE_PRELOAD_MAX_BYTES,
    PROFILE_USE_DUCKDB,
    STEP7_KEEP_TRAIN_ON_DISK,
    STEP7_USE_DUCKDB,
    STEP8_SCREEN_SAMPLE_ROWS,
    STEP9_COMPARE_ALL_GBMS,
    STEP9_EXPORT_LIBSVM,
    STEP9_SAVE_LGB_BINARY,
    STEP9_TRAIN_FROM_FILE,
    TRAIN_METRICS_PREDICT_BATCH_ROWS,
    TRAIN_SPLIT_FRAC,
    VALID_SPLIT_FRAC,
)

_log = logging.getLogger(__name__)

# Stable facade note:
# - ``trainer.core.config`` remains the public SSOT import surface.
# - ``trainer.config`` continues to re-export this module for backward compatibility.
# - Internal shards are imported here so external callers do not need to change.
# - Phase 1 of the refactor only extracts OOM / training-memory boundaries; other
#   config sections stay in this facade module until a later, lower-risk split.

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

# P1.6 (investigation plan): HKD wager edges for prediction_log.bet_size_bucket.
# Buckets are half-open [edge[i], edge[i+1]); final open-ended bucket is f"{int(edges[-1])}_plus".
PREDICTION_LOG_BET_SIZE_EDGES_HKD: Tuple[float, ...] = (0.0, 100.0, 500.0, 2000.0, 10000.0)

# ------------------ Runtime Retention -----------------------------
# Trim local CSV buffers to avoid unbounded growth
SCORER_ALERT_RETENTION_DAYS = 30      # Keep scorer alerts for last N days
SCORER_STATE_RETENTION_HOURS = 24     # Keep scorer SQLite state for this many hours
VALIDATOR_ALERT_RETENTION_DAYS = 30   # Keep alerts visible to validator for last N days
VALIDATION_RESULTS_RETENTION_DAYS = 180  # Keep validation_results history for last N days
# In-memory validation_results cache prune: full-map scan at most once per interval (seconds).
# 0 or negative = every validator cycle (legacy behavior; higher CPU on large caches).
VALIDATOR_CACHE_PRUNE_INTERVAL_SECONDS = 300

# ------------------ Scorer poll defaults (SSOT for scorer CLI) -----------------
# Used by scorer.py --lookback-hours / --interval.
# Single source for Track Human lookback: trainer, backtester, and serving all use this
# (train–serve parity). TRAINER_USE_LOOKBACK has been removed (PLAN step 5).
# Env SCORER_LOOKBACK_HOURS: non-finite or <=0 → fallback 8 (Phase 2 hardening).
# Cap avoids datetime/timedelta OverflowError in scorer when env is absurdly large (Code Review).
try:
    _max_lb = int(float(os.getenv("SCORER_LOOKBACK_HOURS_MAX", "8760")))
    SCORER_LOOKBACK_HOURS_MAX: int = _max_lb if _max_lb > 0 else 8760
except (TypeError, ValueError, OverflowError):
    SCORER_LOOKBACK_HOURS_MAX = 8760
_raw_scorer_lb = os.getenv("SCORER_LOOKBACK_HOURS", "8")
try:
    _scorer_lb_parsed = int(float(_raw_scorer_lb))
    SCORER_LOOKBACK_HOURS = _scorer_lb_parsed if _scorer_lb_parsed > 0 else 8
except (TypeError, ValueError, OverflowError):
    _log.warning("SCORER_LOOKBACK_HOURS invalid (%r); using 8", _raw_scorer_lb)
    SCORER_LOOKBACK_HOURS = 8
if SCORER_LOOKBACK_HOURS > SCORER_LOOKBACK_HOURS_MAX:
    _log.warning(
        "SCORER_LOOKBACK_HOURS=%d exceeds SCORER_LOOKBACK_HOURS_MAX=%d; capping",
        SCORER_LOOKBACK_HOURS,
        SCORER_LOOKBACK_HOURS_MAX,
    )
    SCORER_LOOKBACK_HOURS = SCORER_LOOKBACK_HOURS_MAX
SCORER_POLL_INTERVAL_SECONDS = 45  # Polling interval in seconds (includes run time)

# ------------------ Step 6 chunk cache (Task 7 R6 prefeatures) ------------------
# PLAN: ``.cursor/plans/PLAN_chunk_cache_portable_hit.md`` — default ON for Track-Human skip
# on prefeatures hits; OOM/disk notes in ``doc/training_oom_and_runtime_audit.md``.
CHUNK_TWO_STAGE_CACHE_DEFAULT: bool = True


def chunk_two_stage_cache_enabled() -> bool:
    """Return whether R6 two-stage prefeatures Parquet cache is enabled.

    When env ``CHUNK_TWO_STAGE_CACHE`` is unset or empty, returns
    :data:`CHUNK_TWO_STAGE_CACHE_DEFAULT`. Otherwise:

    - Enable: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    - Disable: ``0``, ``false``, ``no``, ``off``.

    Other values log a warning and fall back to :data:`CHUNK_TWO_STAGE_CACHE_DEFAULT`.
    """
    raw = (os.getenv("CHUNK_TWO_STAGE_CACHE") or "").strip().lower()
    if not raw:
        return CHUNK_TWO_STAGE_CACHE_DEFAULT
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    _log.warning(
        "CHUNK_TWO_STAGE_CACHE=%r invalid; using default %s",
        os.getenv("CHUNK_TWO_STAGE_CACHE"),
        CHUNK_TWO_STAGE_CACHE_DEFAULT,
    )
    return CHUNK_TWO_STAGE_CACHE_DEFAULT


# Max age (hours) of payout_complete_dtm for rows that enter the model each score_once.
# Despite the env name, this applies on *every* cycle when set: older new bets skip
# scoring / prediction_log / alerts (feature build still uses full SCORER_LOOKBACK_HOURS).
# Unset or <=0 disables. Capped at SCORER_LOOKBACK_HOURS_MAX.
_csw_raw = os.getenv("SCORER_COLD_START_WINDOW_HOURS", "").strip()
SCORER_COLD_START_WINDOW_HOURS: Optional[float]
if not _csw_raw:
    SCORER_COLD_START_WINDOW_HOURS = 2.0
else:
    try:
        _csw_p = float(_csw_raw)
        if not math.isfinite(_csw_p) or _csw_p <= 0:
            _log.warning(
                "SCORER_COLD_START_WINDOW_HOURS invalid (%r); scoring age filter disabled",
                _csw_raw,
            )
            SCORER_COLD_START_WINDOW_HOURS = None
        elif _csw_p > float(SCORER_LOOKBACK_HOURS_MAX):
            _log.warning(
                "SCORER_COLD_START_WINDOW_HOURS=%s exceeds SCORER_LOOKBACK_HOURS_MAX=%d; capping",
                _csw_raw,
                SCORER_LOOKBACK_HOURS_MAX,
            )
            SCORER_COLD_START_WINDOW_HOURS = float(SCORER_LOOKBACK_HOURS_MAX)
        else:
            SCORER_COLD_START_WINDOW_HOURS = _csw_p
    except (TypeError, ValueError, OverflowError):
        _log.warning(
            "SCORER_COLD_START_WINDOW_HOURS invalid (%r); scoring age filter disabled",
            _csw_raw,
        )
        SCORER_COLD_START_WINDOW_HOURS = None

# T-OnlineCalibration: if set (>0), scorer ignores state DB runtime_rated_threshold older than this (hours).
_rtt_age = os.getenv("RUNTIME_THRESHOLD_MAX_AGE_HOURS", "").strip()
RUNTIME_THRESHOLD_MAX_AGE_HOURS: Optional[float]
if not _rtt_age:
    RUNTIME_THRESHOLD_MAX_AGE_HOURS = None
else:
    try:
        _rtt_f = float(_rtt_age)
        RUNTIME_THRESHOLD_MAX_AGE_HOURS = _rtt_f if math.isfinite(_rtt_f) and _rtt_f > 0 else None
    except (TypeError, ValueError):
        RUNTIME_THRESHOLD_MAX_AGE_HOURS = None

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
# Task 9 / DEC-037: ClickHouse bet fetch window for validator.
# Pre-context helps "last bet before bet_ts" lookup; max lookback caps CH pressure.
VALIDATOR_FETCH_PRE_CONTEXT_MINUTES: int = 60
VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES: int = 180
VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES_CAP: int = 24 * 60  # hard cap to avoid runaway CH scans
VALIDATOR_NO_BET_RETRY_MAX_ALERTS: int = 50  # Task 9B: targeted retry budget per validator cycle
# Max span (minutes) for a single no-bet retry window (pre_context + lookahead+extras); avoids runaway CH scans.
VALIDATOR_NO_BET_RETRY_MAX_WINDOW_MINUTES: int = 240
# Task 9C: when player_id+time retry returns no rows, query TBET by bet_id (stable key) and merge payout times.
VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED: bool = True
VALIDATOR_NO_BET_BET_ID_CHUNK_SIZE: int = 500  # max bet_ids per IN batch

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
OPTUNA_TIMEOUT_SECONDS: Optional[int] = 10 * 60 * 1 # -1 = no timeout, 10 * 60 = 10 minutes
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

# --- LightGBM device (GPU enable plan Phase A) ---
# Training only: `cpu` (default) or `gpu` (OpenCL on Windows via NVIDIA driver; not `cuda` on Windows).
# Override with env LIGHTGBM_DEVICE_TYPE=gpu or optional root `config.py` LIGHTGBM_DEVICE_TYPE.
# Thread count when device_type=gpu (avoid n_jobs=-1 oversubscribing CPU alongside GPU work).
_LIGHTGBM_DEVICE_RAW = os.getenv("LIGHTGBM_DEVICE_TYPE", "cpu").strip().lower()
if _LIGHTGBM_DEVICE_RAW not in ("cpu", "gpu"):
    _log.warning(
        "LIGHTGBM_DEVICE_TYPE=%r invalid (use cpu or gpu); defaulting to cpu",
        _LIGHTGBM_DEVICE_RAW,
    )
    LIGHTGBM_DEVICE_TYPE: Literal["cpu", "gpu"] = "cpu"
else:
    LIGHTGBM_DEVICE_TYPE = cast(Literal["cpu", "gpu"], _LIGHTGBM_DEVICE_RAW)
try:
    _lgb_gpu_nj = int(os.getenv("LIGHTGBM_GPU_N_JOBS", "4"))
    LIGHTGBM_GPU_N_JOBS: int = _lgb_gpu_nj if _lgb_gpu_nj > 0 else 1
except (TypeError, ValueError):
    LIGHTGBM_GPU_N_JOBS = 4

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
# Scorer SHAP reason-code generation (CPU heavy on large alert batches).
# Default OFF for production latency; set env SCORER_ENABLE_SHAP_REASON_CODES=1 to enable.
SCORER_ENABLE_SHAP_REASON_CODES = os.getenv(
    "SCORER_ENABLE_SHAP_REASON_CODES", "0"
).strip().lower() in ("1", "true", "t", "yes", "y")
# --- A4 two-stage (Stage-1 + Stage-2 FP detector) ---
# Training and inference are opt-in for safe rollout / rollback.
A4_TWO_STAGE_ENABLE_TRAINING = os.getenv(
    "A4_TWO_STAGE_ENABLE_TRAINING", "0"
).strip().lower() in ("1", "true", "t", "yes", "y")
A4_TWO_STAGE_ENABLE_INFERENCE = os.getenv(
    "A4_TWO_STAGE_ENABLE_INFERENCE", "0"
).strip().lower() in ("1", "true", "t", "yes", "y")
# MVP fusion mode is fixed to product. Invalid values should fallback to product.
_A4_TWO_STAGE_FUSION_MODE_RAW = (
    (os.getenv("A4_TWO_STAGE_FUSION_MODE") or "product").strip().lower() or "product"
)
A4_TWO_STAGE_FUSION_MODE = (
    _A4_TWO_STAGE_FUSION_MODE_RAW
    if _A4_TWO_STAGE_FUSION_MODE_RAW in {"product"}
    else "product"
)
# Stage-2 only scores rows where stage1_score >= stage1_threshold * multiplier.
A4_TWO_STAGE_CANDIDATE_MULTIPLIER = float(
    os.getenv("A4_TWO_STAGE_CANDIDATE_MULTIPLIER", "0.9")
)
A4_TWO_STAGE_MIN_TRAIN_ROWS = int(os.getenv("A4_TWO_STAGE_MIN_TRAIN_ROWS", "500"))
A4_TWO_STAGE_MIN_TRAIN_POSITIVES = int(os.getenv("A4_TWO_STAGE_MIN_TRAIN_POSITIVES", "50"))
A4_TWO_STAGE_MIN_VALID_ROWS = int(os.getenv("A4_TWO_STAGE_MIN_VALID_ROWS", "100"))
A4_TWO_STAGE_PREDICT_BATCH_ROWS = int(os.getenv("A4_TWO_STAGE_PREDICT_BATCH_ROWS", "250000"))
LOSS_STREAK_PUSH_RESETS = False  # Whether PUSH resets the loss-streak counter (F4)
HIST_AVG_BET_CAP = 500_000       # Winsorization cap for avg_bet (F2; validate with EDA)

# --- Production class-ratio assumption (for adjusted test precision reporting) ---
# Expected negative-to-positive ratio in production (serving), used to adjust
# test set precision to a realistic estimate of serving precision when negatives
# have been downsampled during training.
# Set to None to disable adjusted-precision reporting.
# Example: 15.0 means production has ~15 negative observations per 1 positive.
PRODUCTION_NEG_POS_RATIO: Optional[float] = 87.0/13.0

# --- Threshold / reporting operating contract (W2 field-test vs legacy) ---
# ``legacy``: historical behaviour (F-beta / AP-centric paths unchanged).
# ``field_test``: DEC-026 field-test objective alignment (Optuna + validation pick
# when precondition + data allow). Written to ``training_metrics.json`` and echoed
# in ``backtest_metrics.json`` for calibration / scorer parity audits.
SELECTION_MODE: str = "field_test"

# --- OOM / training-memory settings re-exported from internal shards ---
# Exposure classes:
# - user policy knobs: e.g. NEG_SAMPLE_FRAC, STEP8_SCREEN_SAMPLE_ROWS
# - pipeline mode defaults: e.g. STEP7_USE_DUCKDB, STEP7_KEEP_TRAIN_ON_DISK
# - internal guards: e.g. CHUNK_CONCAT_RAM_FACTOR, DUCKDB_* memory budget constants
# The public import surface remains this module so tests and callers can continue
# to monkeypatch ``trainer.core.config`` directly.

# --- DuckDB helper: single SSOT for all stages (DEC-027) ---
# These wrappers intentionally stay on the public facade module so that tests and
# callers which monkeypatch ``trainer.core.config.DUCKDB_*`` affect runtime logic.
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