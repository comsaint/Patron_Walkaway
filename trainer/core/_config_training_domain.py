from __future__ import annotations

import logging
import os
from typing import Literal, Optional, cast

"""Internal training-domain / threshold / model-mode config shard."""

_log = logging.getLogger(__name__)

HK_TZ = "Asia/Hong_Kong"
TRAINER_DAYS = 7
BACKTEST_HOURS = 6
BACKTEST_OFFSET_HOURS = 1
HISTORY_BUFFER_DAYS: int = 2

WALKAWAY_GAP_MIN = 30
ALERT_HORIZON_MIN = 15
LABEL_LOOKAHEAD_MIN = WALKAWAY_GAP_MIN + ALERT_HORIZON_MIN
BET_AVAIL_DELAY_MIN = 1
SESSION_AVAIL_DELAY_MIN = 7
RUN_BREAK_MIN = WALKAWAY_GAP_MIN
GAMING_DAY_START_HOUR = 3

G1_PRECISION_MIN = 0.70
G1_ALERT_VOLUME_MIN_PER_HOUR = 5
G1_FBETA = 0.5
OPTUNA_N_TRIALS = 150
# Total HPO wall-clock budget for Step 9. When multiple GBM backends run HPO in
# the same bakeoff, trainer splits this timeout evenly across this many model
# candidates so each backend gets the same wall-clock allowance.
OPTUNA_TIMEOUT_SECONDS: Optional[int] = 60 * 60 * 3
OPTUNA_ACTIVE_MODEL_COUNT_FOR_TOTAL_TIMEOUT_SPLIT: int = 3
OPTUNA_EARLY_STOP_PATIENCE: Optional[int] = 40
OPTUNA_HPO_SAMPLE_ROWS: Optional[int] = 1500000
THRESHOLD_FBETA: float = 0.5
THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL: float = 0.01

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
_GBM_BACKENDS_DEVICE_MODE_RAW = os.getenv("GBM_BACKENDS_DEVICE_MODE", "auto").strip().lower()
if _GBM_BACKENDS_DEVICE_MODE_RAW not in ("auto", "cpu", "gpu"):
    _log.warning(
        "GBM_BACKENDS_DEVICE_MODE=%r invalid (use auto, cpu, or gpu); defaulting to auto",
        _GBM_BACKENDS_DEVICE_MODE_RAW,
    )
    GBM_BACKENDS_DEVICE_MODE: Literal["auto", "cpu", "gpu"] = "auto"
else:
    GBM_BACKENDS_DEVICE_MODE = cast(
        Literal["auto", "cpu", "gpu"],
        _GBM_BACKENDS_DEVICE_MODE_RAW,
    )


def _resolve_trainer_device_mode() -> Literal["auto", "cpu", "gpu"]:
    """Unified trainer device intent: auto | cpu | gpu.

    Precedence:
    - ``TRAINER_DEVICE_MODE`` when set in the process environment (explicit user intent).
    - Otherwise infer from legacy ``LIGHTGBM_DEVICE_TYPE`` / ``GBM_BACKENDS_DEVICE_MODE``
      when those env vars are present (shim); default ``auto``.
    """
    explicit_raw = (os.getenv("TRAINER_DEVICE_MODE") or "").strip()
    if explicit_raw:
        ex = explicit_raw.lower()
        if ex not in ("auto", "cpu", "gpu"):
            _log.warning(
                "TRAINER_DEVICE_MODE=%r invalid (use auto, cpu, or gpu); defaulting to auto",
                explicit_raw,
            )
            resolved: Literal["auto", "cpu", "gpu"] = "auto"
        else:
            resolved = cast(Literal["auto", "cpu", "gpu"], ex)
        if "LIGHTGBM_DEVICE_TYPE" in os.environ or "GBM_BACKENDS_DEVICE_MODE" in os.environ:
            _log.warning(
                "TRAINER_DEVICE_MODE is set; LIGHTGBM_DEVICE_TYPE and GBM_BACKENDS_DEVICE_MODE "
                "are ignored for unified scheduling. Prefer TRAINER_DEVICE_MODE only."
            )
        return resolved

    if "LIGHTGBM_DEVICE_TYPE" in os.environ:
        raw_l = (os.getenv("LIGHTGBM_DEVICE_TYPE") or "").strip().lower()
        if raw_l == "gpu":
            _log.warning(
                "TRAINER_DEVICE_MODE not set; inferred gpu from LIGHTGBM_DEVICE_TYPE=%r. "
                "Prefer TRAINER_DEVICE_MODE=gpu.",
                os.getenv("LIGHTGBM_DEVICE_TYPE"),
            )
            return "gpu"
        if raw_l == "cpu":
            _log.warning(
                "TRAINER_DEVICE_MODE not set; inferred cpu from LIGHTGBM_DEVICE_TYPE=%r. "
                "Prefer TRAINER_DEVICE_MODE=cpu.",
                os.getenv("LIGHTGBM_DEVICE_TYPE"),
            )
            return "cpu"
        if raw_l:
            _log.warning(
                "TRAINER_DEVICE_MODE not set; LIGHTGBM_DEVICE_TYPE=%r is not cpu/gpu; using auto",
                os.getenv("LIGHTGBM_DEVICE_TYPE"),
            )
        return "auto"

    if "GBM_BACKENDS_DEVICE_MODE" in os.environ:
        _log.warning(
            "TRAINER_DEVICE_MODE not set; inferred %r from GBM_BACKENDS_DEVICE_MODE. "
            "Prefer TRAINER_DEVICE_MODE.",
            GBM_BACKENDS_DEVICE_MODE,
        )
        return GBM_BACKENDS_DEVICE_MODE

    return "auto"


TRAINER_DEVICE_MODE: Literal["auto", "cpu", "gpu"] = _resolve_trainer_device_mode()

_TRAINER_GPU_IDS_RAW = (os.getenv("TRAINER_GPU_IDS") or "").strip()
TRAINER_GPU_IDS: Optional[str] = _TRAINER_GPU_IDS_RAW or None
try:
    _bakeoff_workers_raw = int(os.getenv("GBM_BAKEOFF_MAX_PARALLEL_BACKENDS", "0"))
    GBM_BAKEOFF_MAX_PARALLEL_BACKENDS: Optional[int] = (
        _bakeoff_workers_raw if _bakeoff_workers_raw > 0 else None
    )
except (TypeError, ValueError):
    GBM_BAKEOFF_MAX_PARALLEL_BACKENDS = None

SCREEN_FEATURES_TOP_K: Optional[int] = 50
SCREEN_FEATURES_METHOD: Literal["lgbm", "mi", "mi_then_lgbm"] = "lgbm"
THRESHOLD_MIN_ALERT_COUNT: int = 5
THRESHOLD_MIN_RECALL: Optional[float] = 0.01
THRESHOLD_MIN_ALERTS_PER_HOUR: Optional[float] = 1.0

TABLE_HC_WINDOW_MIN = 30
PLACEHOLDER_PLAYER_ID = -1
UNRATED_VOLUME_LOG = True

# A4 two-stage (R4): default on; set A4_TWO_STAGE_ENABLE_TRAINING=0 / A4_TWO_STAGE_ENABLE_INFERENCE=0 to disable.
A4_TWO_STAGE_ENABLE_TRAINING = os.getenv(
    "A4_TWO_STAGE_ENABLE_TRAINING", "1"
).strip().lower() in ("1", "true", "t", "yes", "y")
A4_TWO_STAGE_ENABLE_INFERENCE = os.getenv(
    "A4_TWO_STAGE_ENABLE_INFERENCE", "1"
).strip().lower() in ("1", "true", "t", "yes", "y")
_A4_TWO_STAGE_FUSION_MODE_RAW = (
    (os.getenv("A4_TWO_STAGE_FUSION_MODE") or "product").strip().lower() or "product"
)
A4_TWO_STAGE_FUSION_MODE = (
    _A4_TWO_STAGE_FUSION_MODE_RAW
    if _A4_TWO_STAGE_FUSION_MODE_RAW in {"product"}
    else "product"
)
A4_TWO_STAGE_CANDIDATE_MULTIPLIER = float(
    os.getenv("A4_TWO_STAGE_CANDIDATE_MULTIPLIER", "0.9")
)
A4_TWO_STAGE_MIN_TRAIN_ROWS = int(os.getenv("A4_TWO_STAGE_MIN_TRAIN_ROWS", "500"))
A4_TWO_STAGE_MIN_TRAIN_POSITIVES = int(os.getenv("A4_TWO_STAGE_MIN_TRAIN_POSITIVES", "50"))
A4_TWO_STAGE_MIN_VALID_ROWS = int(os.getenv("A4_TWO_STAGE_MIN_VALID_ROWS", "100"))
A4_TWO_STAGE_PREDICT_BATCH_ROWS = int(os.getenv("A4_TWO_STAGE_PREDICT_BATCH_ROWS", "250000"))
LOSS_STREAK_PUSH_RESETS = False
HIST_AVG_BET_CAP = 500_000

PRODUCTION_NEG_POS_RATIO: Optional[float] = 87.0 / 13.0
SELECTION_MODE: str = "field_test"

# OOF stacking (A3 extension): expanding-monthly OOF over rated training rows.
OOF_STACKING_ENABLED: bool = os.getenv(
    "OOF_STACKING_ENABLED", "1"
).strip().lower() in ("1", "true", "t", "yes", "y")
OOF_STACKING_MIN_FOLDS: int = int(os.getenv("OOF_STACKING_MIN_FOLDS", "2"))
OOF_STACKING_HOLDOUT_MONTHS: int = int(os.getenv("OOF_STACKING_HOLDOUT_MONTHS", "1"))
OOF_STACKING_MIN_VALID_POSITIVES: int = int(
    os.getenv("OOF_STACKING_MIN_VALID_POSITIVES", "1")
)
try:
    _oof_max_months_raw = int(os.getenv("OOF_STACKING_MAX_MONTHS", "0"))
    OOF_STACKING_MAX_MONTHS: Optional[int] = (
        _oof_max_months_raw if _oof_max_months_raw > 0 else None
    )
except (TypeError, ValueError):
    OOF_STACKING_MAX_MONTHS = None

# D3 Phase 2: pit_asof = merge_asof session→bet by link_usable_time; cutoff_window = legacy train_end map.
_IDM_RAW = (os.getenv("IDENTITY_MAPPING_MODE") or "cutoff_window").strip().lower()
if _IDM_RAW not in ("pit_asof", "cutoff_window"):
    _log.warning(
        "IDENTITY_MAPPING_MODE=%r invalid (use pit_asof or cutoff_window); using cutoff_window",
        os.getenv("IDENTITY_MAPPING_MODE"),
    )
    IDENTITY_MAPPING_MODE: Literal["pit_asof", "cutoff_window"] = "cutoff_window"
else:
    IDENTITY_MAPPING_MODE = cast(Literal["pit_asof", "cutoff_window"], _IDM_RAW)

# B2: join t_game Parquet features (DuckDB). Default off (known issues); set
# T_GAME_FEATURES_ENABLED=1 to re-enable.
T_GAME_FEATURES_ENABLED: bool = os.getenv(
    "T_GAME_FEATURES_ENABLED", "0"
).strip().lower() in ("1", "true", "t", "yes", "y")

# A3: include CatBoost in GBM bakeoff. Default off (known issues); set
# GBM_BAKEOFF_ENABLE_CATBOOST=1 or pass --gbm-bakeoff-catboost to enable.
GBM_BAKEOFF_ENABLE_CATBOOST: bool = os.getenv(
    "GBM_BAKEOFF_ENABLE_CATBOOST", "0"
).strip().lower() in ("1", "true", "t", "yes", "y")

