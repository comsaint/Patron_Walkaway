import logging
from typing import Literal, Optional, Tuple

from dotenv import load_dotenv
from trainer.core._config_clickhouse_sources import (
    CASINO_PLAYER_ID_CLEAN_SQL,
    CH_HOST,
    CH_PASS,
    CH_PASSWORD,
    CH_PORT,
    CH_SECURE,
    CH_TEAMDB_HOST,
    CH_USER,
    SOURCE_DB,
    TBET,
    TGAME,
    TPROFILE,
    TSESSION,
)
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
from trainer.core._config_env_paths import (
    DEFAULT_BACKTEST_OUT,
    DEFAULT_MODEL_DIR,
    NUMEXPR_MAX_THREADS,
    PREDICTION_EXPORT_BATCH_ROWS,
    PREDICTION_EXPORT_SAFETY_LAG_MINUTES,
    PREDICTION_LOG_BET_SIZE_EDGES_HKD,
    PREDICTION_LOG_DB_PATH,
    PREDICTION_LOG_RETENTION_DAYS,
    PREDICTION_LOG_RETENTION_DELETE_BATCH,
    PREDICTION_LOG_SUMMARY_WINDOW_MINUTES,
    _REPO_ROOT,
    bootstrap_dotenv,
)
from trainer.core._config_serving_runtime import (
    CHUNK_TWO_STAGE_CACHE_DEFAULT,
    DISABLE_PROGRESS_BAR,
    RUNTIME_THRESHOLD_MAX_AGE_HOURS,
    SCORER_ALERT_RETENTION_DAYS,
    SCORER_COLD_START_WINDOW_HOURS,
    SCORER_ENABLE_SHAP_REASON_CODES,
    SCORER_LOOKBACK_HOURS,
    SCORER_LOOKBACK_HOURS_MAX,
    SCORER_POLL_INTERVAL_SECONDS,
    SCORER_STATE_RETENTION_HOURS,
    TABLE_STATUS_HC_RETENTION_DAYS,
    TABLE_STATUS_LOOKBACK_HOURS,
    TABLE_STATUS_REFRESH_SECONDS,
    TABLE_STATUS_RETENTION_HOURS,
    VALIDATION_RESULTS_RETENTION_DAYS,
    VALIDATOR_CACHE_PRUNE_INTERVAL_SECONDS,
    chunk_two_stage_cache_enabled as _chunk_two_stage_cache_enabled_impl,
)
from trainer.core._config_training_domain import (
    A4_TWO_STAGE_CANDIDATE_MULTIPLIER,
    A4_TWO_STAGE_ENABLE_INFERENCE,
    A4_TWO_STAGE_ENABLE_TRAINING,
    A4_TWO_STAGE_FUSION_MODE,
    A4_TWO_STAGE_MIN_TRAIN_POSITIVES,
    A4_TWO_STAGE_MIN_TRAIN_ROWS,
    A4_TWO_STAGE_MIN_VALID_ROWS,
    A4_TWO_STAGE_PREDICT_BATCH_ROWS,
    ALERT_HORIZON_MIN,
    BACKTEST_HOURS,
    BACKTEST_OFFSET_HOURS,
    BET_AVAIL_DELAY_MIN,
    GBM_BACKENDS_DEVICE_MODE,
    GBM_BAKEOFF_MAX_PARALLEL_BACKENDS,
    G1_ALERT_VOLUME_MIN_PER_HOUR,
    G1_FBETA,
    G1_PRECISION_MIN,
    GAMING_DAY_START_HOUR,
    HIST_AVG_BET_CAP,
    HISTORY_BUFFER_DAYS,
    HK_TZ,
    LABEL_LOOKAHEAD_MIN,
    LIGHTGBM_DEVICE_TYPE,
    LIGHTGBM_GPU_N_JOBS,
    LOSS_STREAK_PUSH_RESETS,
    OPTUNA_ACTIVE_MODEL_COUNT_FOR_TOTAL_TIMEOUT_SPLIT,
    OPTUNA_EARLY_STOP_PATIENCE,
    OPTUNA_HPO_SAMPLE_ROWS,
    OPTUNA_N_TRIALS,
    OPTUNA_TIMEOUT_SECONDS,
    PLACEHOLDER_PLAYER_ID,
    PRODUCTION_NEG_POS_RATIO,
    RUN_BREAK_MIN,
    SCREEN_FEATURES_METHOD,
    SCREEN_FEATURES_TOP_K,
    SELECTION_MODE,
    SESSION_AVAIL_DELAY_MIN,
    TABLE_HC_WINDOW_MIN,
    THRESHOLD_FBETA,
    THRESHOLD_MIN_ALERTS_PER_HOUR,
    THRESHOLD_MIN_ALERT_COUNT,
    THRESHOLD_MIN_RECALL,
    THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL,
    TRAINER_DAYS,
    TRAINER_GPU_IDS,
    UNRATED_VOLUME_LOG,
    WALKAWAY_GAP_MIN,
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
from trainer.core._config_validator import (
    VALIDATOR_ALERT_RETENTION_DAYS,
    VALIDATOR_EXTENDED_WAIT_MINUTES,
    VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES,
    VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES_CAP,
    VALIDATOR_FETCH_PRE_CONTEXT_MINUTES,
    VALIDATOR_FINALITY_HOURS,
    VALIDATOR_FINALIZE_MINUTES,
    VALIDATOR_FINALIZE_ON_HORIZON,
    VALIDATOR_FRESHNESS_BUFFER_MINUTES,
    VALIDATOR_NO_BET_BET_ID_CHUNK_SIZE,
    VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED,
    VALIDATOR_NO_BET_RETRY_MAX_ALERTS,
    VALIDATOR_NO_BET_RETRY_MAX_WINDOW_MINUTES,
)

_log = logging.getLogger(__name__)

# Stable facade note:
# - ``trainer.core.config`` remains the public SSOT import surface.
# - ``trainer.config`` continues to re-export this module for backward compatibility.
# - Internal shards are imported here so external callers do not need to change.
# - Tests and callers may continue to monkeypatch names on this facade module.

bootstrap_dotenv(load_dotenv, _log)


def chunk_two_stage_cache_enabled() -> bool:
    """Facade wrapper so monkeypatches on this module keep affecting the helper."""
    return _chunk_two_stage_cache_enabled_impl(CHUNK_TWO_STAGE_CACHE_DEFAULT)

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
