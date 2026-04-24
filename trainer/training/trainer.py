"""trainer/trainer.py — Phase 1 Refactor
=========================================
Patron Walkaway Prediction — Training Pipeline

Pipeline (SSOT §4.3 / §9)
--------------------------
1. time_fold.get_monthly_chunks(start, end)  -> month boundaries
2. Per chunk: load bets + sessions -> DQ -> identity -> labels -> Track Human features
   - Data source: ClickHouse (production) OR local Parquet (dev iteration)
   - Labels use C1 extended pull; bets in (window_end, extended_end] are
     used only for label computation, NOT added to training rows.
3. Write each processed chunk to .data/chunks/ as Parquet.
4. Concatenate all chunks; split train / valid / test at ROW level (time-ordered
   70/15/15 — SSOT §9.2).  Chunks control ETL/cache volume only, not split semantics.
5. sample_weight = 1 / N_run  (canonical_id × run_id from compute_run_boundary), train set only.
6. Optuna TPE hyperparameter search on validation set (per model type).
7. Train Rated GBDT family under the A3 contract; the final rated artifact may be a
   single model or an ensemble wrapper, but the bundle still exposes one ``model.pkl``.
8. Atomic artifact bundle -> trainer/models/.

Artifact format (version-tagged, v10 single-entry)
--------------------------------------------------
models/
  model.pkl                 Rated artifact model object (single model or ensemble wrapper)
  feature_list.json         [{name, track}]  track ∈ {"track_llm", "track_human", "track_profile"} (PLAN Step 7)
  model_version             YYYYMMDD-HHMMSS-<git7>  (plain text)
  training_metrics.json     legacy v1: validation + test metrics, feature importance (gain), Optuna best params
  training_metrics.v2.json  v2: nested datasets + selection summary (no long importance / no gbm_bakeoff blob)
  feature_importance.json   winner gain importance list (split from v1 payload)
  comparison_metrics.json   comparison families registry (e.g. A3 gbm_bakeoff)

Model bundle contract (DEC-040)
-------------------------------
Serving and backtesting load **model.pkl** only. The trainer writes one rated
artifact entry into model.pkl (and does not emit legacy walkaway_model.pkl).
Stale dual-model and legacy pickles are removed after each successful training run.

Data source switching
---------------------
  --use-local-parquet   Read from data/ Parquet files instead of
                        ClickHouse.  Same DQ filters + time semantics apply.
  Default: ClickHouse for production.
"""

from __future__ import annotations

import gc
import importlib
import math
import os
from importlib import import_module as _import_module_threshold_selection
import shutil
import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple, Union, cast

import joblib
import lightgbm as lgb
import numpy as np
import optuna
from optuna.trial import FrozenTrial
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.model_selection import train_test_split
from zoneinfo import ZoneInfo

from trainer.profile_schedule import latest_month_end_on_or_before, month_end_dates
from trainer.training.field_test_objective_precondition import (
    FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV,
    log_optuna_precondition_context,
    log_precondition_block_warning,
    precondition_constrained_optuna_allowed,
    training_metrics_overlay_from_precondition,
    try_load_precondition_json,
)
from trainer.training.ranking_recipe_weights import (
    RANKING_RECIPE_BASELINE,
    RANKING_RECIPE_COMBINED,
    RANKING_RECIPE_HNM,
    build_final_ranking_weights_from_libsvm_proxy,
    build_final_ranking_weights_in_memory,
    invalidate_lgb_binary_cache_for_libsvm,
    read_libsvm_weight_file,
    refine_weights_hnm_shallow_lgbm,
    write_libsvm_weight_file,
    resolve_ranking_recipe,
)
from trainer.training.two_stage import (
    A4_FUSION_MODE_PRODUCT,
    candidate_cutoff_from_threshold,
    candidate_mask_from_scores,
    fuse_product_scores,
    validate_fusion_mode,
)
from trainer.core.model_bundle_paths import (
    safe_version_subdirectory,
    write_latest_model_manifest,
)
from trainer.core.training_metrics_v2_bundle_write import write_training_metrics_v2_sidecars
from trainer.core.mlflow_utils import (
    has_active_run,
    log_artifact_safe,
    log_artifacts_safe,
    log_metrics_safe,
    log_params_safe,
    log_tags_safe,
    safe_start_run,
    warm_up_mlflow_run_safe,
)

try:
    from tqdm import tqdm as _tqdm_bar
except ImportError:
    def _tqdm_bar(**kwargs: Any) -> Any:
        """No-op progress bar when tqdm is not installed (PLAN § Step 6 進度條)."""
        class _NoopBar:
            def update(self, n: int = 1) -> None: pass  # noqa: E701
            def close(self) -> None: pass  # noqa: E701
        return _NoopBar()


class _ProgressNoop:
    """No-op bar when DISABLE_PROGRESS_BAR (PLAN § progress-bars-long-steps)."""
    def update(self, n: int = 1) -> None: ...
    def close(self) -> None: ...

optuna.logging.set_verbosity(optuna.logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("trainer")

# MLflow namespace: keep this project isolated on shared tracking server.
# Override via credential/mlflow.env (MLFLOW_EXPERIMENT_TRAIN).
MLFLOW_EXPERIMENT_TRAIN = (
    (os.environ.get("MLFLOW_EXPERIMENT_TRAIN") or "").strip()
    or "patron/patron_walkaway/prod/train"
)


def _agent_debug_log(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict,
    run_id: str = "pre-fix",
) -> None:
    """Append one NDJSON debug line for runtime investigation (debug mode session bc1669)."""
    try:
        payload = {
            "sessionId": "bc1669",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        candidates = [
            Path.cwd() / "debug-bc1669.log",
            Path(__file__).resolve().parents[2] / "debug-bc1669.log",
        ]
        _last_error: Optional[str] = None
        for log_path in candidates:
            try:
                with open(log_path, "a", encoding="utf-8") as _f:
                    _f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                return
            except Exception as _e:  # pragma: no cover - debug-only fallback
                _last_error = f"{type(_e).__name__}: {_e}"
        logger.warning("agent debug log write failed for all candidates: %s", _last_error)
    except Exception as _e:
        # Debug logging must never interrupt training.
        logger.warning("agent debug log payload/build failed: %s", _e)

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
    OPTUNA_TIMEOUT_SECONDS: Optional[int] = getattr(_cfg, "OPTUNA_TIMEOUT_SECONDS", 10 * 60)
    OPTUNA_EARLY_STOP_PATIENCE: Optional[int] = getattr(_cfg, "OPTUNA_EARLY_STOP_PATIENCE", None)
    OPTUNA_HPO_SAMPLE_ROWS: Optional[int] = getattr(_cfg, "OPTUNA_HPO_SAMPLE_ROWS", None)
    # G1_PRECISION_MIN / G1_ALERT_VOLUME_MIN_PER_HOUR / G1_FBETA intentionally
    # not imported — deprecated per DEC-009/010; rollback path only.
    PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
    SOURCE_DB = _cfg.SOURCE_DB
    TBET = _cfg.TBET
    TSESSION = _cfg.TSESSION
    TPROFILE: str = getattr(_cfg, "TPROFILE", "player_profile")
    HK_TZ_STR: str = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    TRAINER_DAYS: int = getattr(_cfg, "TRAINER_DAYS", 30)
    HISTORY_BUFFER_DAYS: int = getattr(_cfg, "HISTORY_BUFFER_DAYS", 2)
    CHUNK_CONCAT_MEMORY_WARN_BYTES: int = getattr(_cfg, "CHUNK_CONCAT_MEMORY_WARN_BYTES", 1 * (1024**3))
    CHUNK_CONCAT_RAM_FACTOR: float = getattr(_cfg, "CHUNK_CONCAT_RAM_FACTOR", 3)
    STEP7_PANDAS_FALLBACK_MAX_BYTES: int = getattr(_cfg, "STEP7_PANDAS_FALLBACK_MAX_BYTES", 256 * 1024 * 1024)
    TRAIN_SPLIT_FRAC: float = getattr(_cfg, "TRAIN_SPLIT_FRAC", 0.70)
    VALID_SPLIT_FRAC: float = getattr(_cfg, "VALID_SPLIT_FRAC", 0.15)
    MIN_VALID_TEST_ROWS: int = getattr(_cfg, "MIN_VALID_TEST_ROWS", 50)
    THRESHOLD_MIN_ALERT_COUNT: int = getattr(_cfg, "THRESHOLD_MIN_ALERT_COUNT", 5)
    THRESHOLD_MIN_RECALL: Optional[float] = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)
    THRESHOLD_FBETA: float = getattr(_cfg, "THRESHOLD_FBETA", 0.5)
    NEG_SAMPLE_FRAC: float = getattr(_cfg, "NEG_SAMPLE_FRAC", 1.0)
    NEG_SAMPLE_FRAC_AUTO: bool = getattr(_cfg, "NEG_SAMPLE_FRAC_AUTO", True)
    NEG_SAMPLE_FRAC_MIN: float = getattr(_cfg, "NEG_SAMPLE_FRAC_MIN", 0.05)
    NEG_SAMPLE_FRAC_ASSUMED_POS_RATE: float = getattr(_cfg, "NEG_SAMPLE_FRAC_ASSUMED_POS_RATE", 0.15)
    NEG_SAMPLE_RAM_SAFETY: float = getattr(_cfg, "NEG_SAMPLE_RAM_SAFETY", 0.75)
    NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT: int = getattr(_cfg, "NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT", 200 * 1024 * 1024)
    PRODUCTION_NEG_POS_RATIO: Optional[float] = getattr(_cfg, "PRODUCTION_NEG_POS_RATIO", None)
    SELECTION_MODE: str = str(getattr(_cfg, "SELECTION_MODE", "field_test") or "field_test").strip() or "field_test"
    STEP7_USE_DUCKDB: bool = getattr(_cfg, "STEP7_USE_DUCKDB", True)
    # STEP7 DuckDB: runtime uses get_duckdb_memory_config("step7"); exposed for tests (DEC-027).
    STEP7_DUCKDB_RAM_FRACTION: float = getattr(_cfg, "STEP7_DUCKDB_RAM_FRACTION", 0.50)
    STEP7_DUCKDB_RAM_MIN_GB: float = getattr(_cfg, "STEP7_DUCKDB_RAM_MIN_GB", 2.0)
    STEP7_DUCKDB_RAM_MAX_GB: float = getattr(_cfg, "STEP7_DUCKDB_RAM_MAX_GB", 24.0)
    STEP7_DUCKDB_THREADS: int = getattr(_cfg, "STEP7_DUCKDB_THREADS", 4)
    STEP7_DUCKDB_PRESERVE_INSERTION_ORDER: bool = getattr(_cfg, "STEP7_DUCKDB_PRESERVE_INSERTION_ORDER", False)
    STEP7_DUCKDB_TEMP_DIR: Optional[str] = getattr(_cfg, "STEP7_DUCKDB_TEMP_DIR", None)
    STEP7_KEEP_TRAIN_ON_DISK: bool = getattr(_cfg, "STEP7_KEEP_TRAIN_ON_DISK", False)
    STEP9_EXPORT_LIBSVM: bool = getattr(_cfg, "STEP9_EXPORT_LIBSVM", False)
    STEP9_TRAIN_FROM_FILE: bool = getattr(_cfg, "STEP9_TRAIN_FROM_FILE", False)
    STEP9_SAVE_LGB_BINARY: bool = getattr(_cfg, "STEP9_SAVE_LGB_BINARY", False)
    TRAIN_METRICS_PREDICT_BATCH_ROWS: int = getattr(_cfg, "TRAIN_METRICS_PREDICT_BATCH_ROWS", 500_000)
    A4_TWO_STAGE_ENABLE_TRAINING: bool = bool(getattr(_cfg, "A4_TWO_STAGE_ENABLE_TRAINING", False))
    A4_TWO_STAGE_FUSION_MODE: str = str(getattr(_cfg, "A4_TWO_STAGE_FUSION_MODE", A4_FUSION_MODE_PRODUCT) or A4_FUSION_MODE_PRODUCT)
    A4_TWO_STAGE_CANDIDATE_MULTIPLIER: float = float(getattr(_cfg, "A4_TWO_STAGE_CANDIDATE_MULTIPLIER", 0.9))
    A4_TWO_STAGE_MIN_TRAIN_ROWS: int = int(getattr(_cfg, "A4_TWO_STAGE_MIN_TRAIN_ROWS", 500))
    A4_TWO_STAGE_MIN_TRAIN_POSITIVES: int = int(getattr(_cfg, "A4_TWO_STAGE_MIN_TRAIN_POSITIVES", 50))
    A4_TWO_STAGE_MIN_VALID_ROWS: int = int(getattr(_cfg, "A4_TWO_STAGE_MIN_VALID_ROWS", 100))
    A4_TWO_STAGE_PREDICT_BATCH_ROWS: int = int(getattr(_cfg, "A4_TWO_STAGE_PREDICT_BATCH_ROWS", 250_000))
    STEP8_SCREEN_SAMPLE_ROWS: Optional[int] = getattr(_cfg, "STEP8_SCREEN_SAMPLE_ROWS", None)
    # Canonical mapping DuckDB from get_duckdb_memory_config("canonical_map") (DEC-027).
    CASINO_PLAYER_ID_CLEAN_SQL: str = getattr(_cfg, "CASINO_PLAYER_ID_CLEAN_SQL", "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END")
except ModuleNotFoundError:
    import trainer.config as _cfg  # type: ignore[import]

    WALKAWAY_GAP_MIN = _cfg.WALKAWAY_GAP_MIN
    ALERT_HORIZON_MIN = _cfg.ALERT_HORIZON_MIN
    LABEL_LOOKAHEAD_MIN = _cfg.LABEL_LOOKAHEAD_MIN
    BET_AVAIL_DELAY_MIN = _cfg.BET_AVAIL_DELAY_MIN
    SESSION_AVAIL_DELAY_MIN = _cfg.SESSION_AVAIL_DELAY_MIN
    OPTUNA_N_TRIALS = _cfg.OPTUNA_N_TRIALS
    OPTUNA_HPO_SAMPLE_ROWS: Optional[int] = getattr(_cfg, "OPTUNA_HPO_SAMPLE_ROWS", None)  # type: ignore[no-redef]
    OPTUNA_TIMEOUT_SECONDS: Optional[int] = getattr(_cfg, "OPTUNA_TIMEOUT_SECONDS", 10 * 60)  # type: ignore[no-redef]
    OPTUNA_EARLY_STOP_PATIENCE: Optional[int] = getattr(_cfg, "OPTUNA_EARLY_STOP_PATIENCE", None)  # type: ignore[no-redef]
    # G1_PRECISION_MIN / G1_ALERT_VOLUME_MIN_PER_HOUR / G1_FBETA intentionally
    # not imported — deprecated per DEC-009/010; rollback path only.
    PLACEHOLDER_PLAYER_ID = _cfg.PLACEHOLDER_PLAYER_ID
    SOURCE_DB = _cfg.SOURCE_DB
    TBET = _cfg.TBET
    TSESSION = _cfg.TSESSION
    TPROFILE = getattr(_cfg, "TPROFILE", "player_profile")
    HK_TZ_STR = getattr(_cfg, "HK_TZ", "Asia/Hong_Kong")
    TRAINER_DAYS = getattr(_cfg, "TRAINER_DAYS", 30)
    HISTORY_BUFFER_DAYS = getattr(_cfg, "HISTORY_BUFFER_DAYS", 2)
    CHUNK_CONCAT_MEMORY_WARN_BYTES = getattr(_cfg, "CHUNK_CONCAT_MEMORY_WARN_BYTES", 1 * (1024**3))
    CHUNK_CONCAT_RAM_FACTOR = getattr(_cfg, "CHUNK_CONCAT_RAM_FACTOR", 3)
    STEP7_PANDAS_FALLBACK_MAX_BYTES = getattr(_cfg, "STEP7_PANDAS_FALLBACK_MAX_BYTES", 256 * 1024 * 1024)
    TRAIN_SPLIT_FRAC = getattr(_cfg, "TRAIN_SPLIT_FRAC", 0.70)
    VALID_SPLIT_FRAC = getattr(_cfg, "VALID_SPLIT_FRAC", 0.15)
    MIN_VALID_TEST_ROWS = getattr(_cfg, "MIN_VALID_TEST_ROWS", 50)
    THRESHOLD_MIN_ALERT_COUNT = getattr(_cfg, "THRESHOLD_MIN_ALERT_COUNT", 5)
    THRESHOLD_MIN_RECALL = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)
    THRESHOLD_FBETA = getattr(_cfg, "THRESHOLD_FBETA", 0.5)
    NEG_SAMPLE_FRAC = getattr(_cfg, "NEG_SAMPLE_FRAC", 1.0)
    NEG_SAMPLE_FRAC_AUTO = getattr(_cfg, "NEG_SAMPLE_FRAC_AUTO", True)
    NEG_SAMPLE_FRAC_MIN = getattr(_cfg, "NEG_SAMPLE_FRAC_MIN", 0.05)
    NEG_SAMPLE_FRAC_ASSUMED_POS_RATE = getattr(_cfg, "NEG_SAMPLE_FRAC_ASSUMED_POS_RATE", 0.15)
    NEG_SAMPLE_RAM_SAFETY = getattr(_cfg, "NEG_SAMPLE_RAM_SAFETY", 0.75)
    NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT = getattr(_cfg, "NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT", 200 * 1024 * 1024)
    PRODUCTION_NEG_POS_RATIO = getattr(_cfg, "PRODUCTION_NEG_POS_RATIO", None)  # type: ignore[no-redef]
    SELECTION_MODE = str(getattr(_cfg, "SELECTION_MODE", "field_test") or "field_test").strip() or "field_test"  # type: ignore[no-redef]
    STEP7_USE_DUCKDB = getattr(_cfg, "STEP7_USE_DUCKDB", True)
    STEP7_DUCKDB_RAM_FRACTION = getattr(_cfg, "STEP7_DUCKDB_RAM_FRACTION", 0.50)
    STEP7_DUCKDB_RAM_MIN_GB = getattr(_cfg, "STEP7_DUCKDB_RAM_MIN_GB", 2.0)
    STEP7_DUCKDB_RAM_MAX_GB = getattr(_cfg, "STEP7_DUCKDB_RAM_MAX_GB", 24.0)
    STEP7_DUCKDB_THREADS = getattr(_cfg, "STEP7_DUCKDB_THREADS", 4)
    STEP7_DUCKDB_PRESERVE_INSERTION_ORDER = getattr(_cfg, "STEP7_DUCKDB_PRESERVE_INSERTION_ORDER", False)
    STEP7_DUCKDB_TEMP_DIR = getattr(_cfg, "STEP7_DUCKDB_TEMP_DIR", None)
    STEP7_KEEP_TRAIN_ON_DISK = getattr(_cfg, "STEP7_KEEP_TRAIN_ON_DISK", False)
    STEP9_EXPORT_LIBSVM = getattr(_cfg, "STEP9_EXPORT_LIBSVM", False)
    STEP9_TRAIN_FROM_FILE = getattr(_cfg, "STEP9_TRAIN_FROM_FILE", False)
    STEP9_SAVE_LGB_BINARY = getattr(_cfg, "STEP9_SAVE_LGB_BINARY", False)
    TRAIN_METRICS_PREDICT_BATCH_ROWS = getattr(_cfg, "TRAIN_METRICS_PREDICT_BATCH_ROWS", 500_000)
    A4_TWO_STAGE_ENABLE_TRAINING = bool(getattr(_cfg, "A4_TWO_STAGE_ENABLE_TRAINING", False))
    A4_TWO_STAGE_FUSION_MODE = str(getattr(_cfg, "A4_TWO_STAGE_FUSION_MODE", A4_FUSION_MODE_PRODUCT) or A4_FUSION_MODE_PRODUCT)
    A4_TWO_STAGE_CANDIDATE_MULTIPLIER = float(getattr(_cfg, "A4_TWO_STAGE_CANDIDATE_MULTIPLIER", 0.9))
    A4_TWO_STAGE_MIN_TRAIN_ROWS = int(getattr(_cfg, "A4_TWO_STAGE_MIN_TRAIN_ROWS", 500))
    A4_TWO_STAGE_MIN_TRAIN_POSITIVES = int(getattr(_cfg, "A4_TWO_STAGE_MIN_TRAIN_POSITIVES", 50))
    A4_TWO_STAGE_MIN_VALID_ROWS = int(getattr(_cfg, "A4_TWO_STAGE_MIN_VALID_ROWS", 100))
    A4_TWO_STAGE_PREDICT_BATCH_ROWS = int(getattr(_cfg, "A4_TWO_STAGE_PREDICT_BATCH_ROWS", 250_000))
    STEP8_SCREEN_SAMPLE_ROWS = getattr(_cfg, "STEP8_SCREEN_SAMPLE_ROWS", None)
    CASINO_PLAYER_ID_CLEAN_SQL = getattr(_cfg, "CASINO_PLAYER_ID_CLEAN_SQL", "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END")

# LightGBM device: env + trainer.core.config default, optional override via root config.py (GPU plan Phase A).
# importlib: avoid ruff E402 (imports must follow the try/except _cfg block above).
_core_trainer_config = importlib.import_module("trainer.core.config")

_LIGHTGBM_DEV = str(
    getattr(_cfg, "LIGHTGBM_DEVICE_TYPE", _core_trainer_config.LIGHTGBM_DEVICE_TYPE)
).strip().lower()
if _LIGHTGBM_DEV not in ("cpu", "gpu"):
    logger.warning("LIGHTGBM_DEVICE_TYPE=%r invalid (use cpu or gpu); using cpu", _LIGHTGBM_DEV)
    LIGHTGBM_DEVICE_TYPE: str = "cpu"
else:
    LIGHTGBM_DEVICE_TYPE = _LIGHTGBM_DEV
try:
    LIGHTGBM_GPU_N_JOBS = int(
        getattr(_cfg, "LIGHTGBM_GPU_N_JOBS", _core_trainer_config.LIGHTGBM_GPU_N_JOBS)
    )
except (TypeError, ValueError):
    LIGHTGBM_GPU_N_JOBS = _core_trainer_config.LIGHTGBM_GPU_N_JOBS
if LIGHTGBM_GPU_N_JOBS < 1:
    LIGHTGBM_GPU_N_JOBS = 1
_GBM_BACKENDS_DEVICE_MODE_RAW = str(
    getattr(
        _cfg,
        "GBM_BACKENDS_DEVICE_MODE",
        getattr(_core_trainer_config, "GBM_BACKENDS_DEVICE_MODE", "auto"),
    )
    or "auto"
).strip().lower()
if _GBM_BACKENDS_DEVICE_MODE_RAW not in ("auto", "cpu", "gpu"):
    logger.warning(
        "GBM_BACKENDS_DEVICE_MODE=%r invalid (use auto, cpu, or gpu); using auto",
        _GBM_BACKENDS_DEVICE_MODE_RAW,
    )
    GBM_BACKENDS_DEVICE_MODE: str = "auto"
else:
    GBM_BACKENDS_DEVICE_MODE = _GBM_BACKENDS_DEVICE_MODE_RAW
TRAINER_GPU_IDS: Optional[str] = (
    str(
        getattr(
            _cfg,
            "TRAINER_GPU_IDS",
            getattr(_core_trainer_config, "TRAINER_GPU_IDS", None),
        )
        or ""
    ).strip()
    or None
)
try:
    GBM_BAKEOFF_MAX_PARALLEL_BACKENDS = int(
        getattr(
            _cfg,
            "GBM_BAKEOFF_MAX_PARALLEL_BACKENDS",
            getattr(_core_trainer_config, "GBM_BAKEOFF_MAX_PARALLEL_BACKENDS", 0),
        )
    )
except (TypeError, ValueError):
    GBM_BAKEOFF_MAX_PARALLEL_BACKENDS = 0
if GBM_BAKEOFF_MAX_PARALLEL_BACKENDS < 0:
    GBM_BAKEOFF_MAX_PARALLEL_BACKENDS = 0

# Effective device for this process: updated by configure_lightgbm_device_for_run() in run_pipeline.
_EFFECTIVE_LIGHTGBM_DEVICE: str = LIGHTGBM_DEVICE_TYPE
_LIGHTGBM_GPU_FALLBACK_USED: bool = False
_REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS: str = LIGHTGBM_DEVICE_TYPE
_CLI_LIGHTGBM_DEVICE_OVERRIDE: Optional[str] = None

try:
    _threshold_selection_mod = _import_module_threshold_selection(
        "trainer.training.threshold_selection"
    )
except ModuleNotFoundError:
    _threshold_selection_mod = _import_module_threshold_selection("training.threshold_selection")
pick_threshold_dec026 = _threshold_selection_mod.pick_threshold_dec026

# Module-level pipeline imports: try = run from trainer dir with modules on path (e.g. dev);
# except = run as package (python -m trainer.trainer). Only the except path uses relative db_conn.
try:
    from time_fold import get_monthly_chunks, get_train_valid_test_split  # type: ignore[import]
    from identity import (  # type: ignore[import]
        build_canonical_mapping_from_df,
        build_canonical_mapping,
        build_canonical_mapping_from_links,
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
        coerce_feature_dtypes,
        PROFILE_FEATURE_COLS,
        get_all_candidate_feature_ids,
        get_candidate_feature_ids,
    )
    # except path uses relative .db_conn (python -m trainer.trainer)
    from db_conn import get_clickhouse_client  # type: ignore[import]
    from etl_player_profile import (  # type: ignore[import]
        compute_profile_schema_hash,
        LOCAL_PROFILE_SCHEMA_HASH,
        backfill as _etl_backfill,
    )
    from config import SCREEN_FEATURES_METHOD  # type: ignore[import]
    from schema_io import normalize_bets_sessions  # type: ignore[import]
except ModuleNotFoundError:
    from trainer.time_fold import get_monthly_chunks, get_train_valid_test_split  # type: ignore[import]
    from trainer.identity import (  # type: ignore[import]
        build_canonical_mapping_from_df,
        build_canonical_mapping,
        build_canonical_mapping_from_links,
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
        coerce_feature_dtypes,
        PROFILE_FEATURE_COLS,
        get_all_candidate_feature_ids,
        get_candidate_feature_ids,
    )
    from trainer.db_conn import get_clickhouse_client  # type: ignore[import]
    from trainer.etl_player_profile import (  # type: ignore[import]
        compute_profile_schema_hash,
        LOCAL_PROFILE_SCHEMA_HASH,
        backfill as _etl_backfill,
    )
    from trainer.config import SCREEN_FEATURES_METHOD  # type: ignore[import]
    from trainer.schema_io import normalize_bets_sessions  # type: ignore[import]

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

# Minimal bet columns needed by the full process_chunk pipeline.
# Column pushdown: load_local_parquet reads only these from the ~60-column t_bet Parquet,
# cutting RAM by ~2/3 and avoiding the 17-object-column .copy() OOM.
#
# Includes:
#   - DQ / identity:    bet_id, session_id, player_id, table_id, payout_complete_dtm,
#                       gaming_day, wager, lud_dtm, __etl_insert_Dtm
#   - Track Human:      status  (loss_streak needs it; run_boundary uses payout_complete_dtm)
#                       casino_win (run P&L features)
#   - Track LLM YAML:   payout_odds, is_back_bet, position_idx  (allowed_columns whitelist)
#   - Legacy / Track LLM: base_ha, etc. (see feature_spec YAML)
#   - Output chunk:     run_id, canonical_id, is_rated, label added downstream
#
# If a future feature spec references additional source columns, add them here.
_REQUIRED_BET_PARQUET_COLS: list = [
    # Keys & timestamps
    "bet_id",
    "session_id",
    "player_id",
    "table_id",
    "payout_complete_dtm",
    "gaming_day",
    # DQ guard / Track Human state machines
    "wager",
    "status",
    "casino_win",
    # Legacy / Track LLM features
    "payout_odds",
    "base_ha",
    "is_back_bet",
    "position_idx",
]


# Resolve to trainer/ so feature_spec, .data, models remain under trainer/ (PLAN 2.2 move).
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / ".data"
CHUNK_DIR = DATA_DIR / "chunks"
LOCAL_PARQUET_DIR = PROJECT_ROOT / "data"
CANONICAL_MAPPING_PARQUET = LOCAL_PARQUET_DIR / "canonical_mapping.parquet"
CANONICAL_MAPPING_CUTOFF_JSON = LOCAL_PARQUET_DIR / "canonical_mapping.cutoff.json"
FEATURE_SPEC_PATH = BASE_DIR / "feature_spec" / "features_candidates.yaml"
MODEL_DIR: Path = cast(Path, getattr(_cfg, "DEFAULT_MODEL_DIR", BASE_DIR / "models"))
OUT_DIR = BASE_DIR / "out_trainer"

for _d in (DATA_DIR, CHUNK_DIR, LOCAL_PARQUET_DIR, MODEL_DIR, OUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Feature column lists are now from Feature Spec YAML (get_all_candidate_feature_ids /
# get_candidate_feature_ids). See feat-consolidation Step 3; no TRACK_B_FEATURE_COLS,
# LEGACY_FEATURE_COLS, or ALL_FEATURE_COLS here.
# HISTORY_BUFFER_DAYS is read from config (DEC-027) in the config block above.

# ---------------------------------------------------------------------------
# Canonical mapping: DuckDB path (PLAN Step 2)
# ---------------------------------------------------------------------------

def _compute_canonical_map_duckdb_budget(available_bytes: Optional[int]) -> int:
    """Compute DuckDB memory_limit (bytes) for canonical mapping. DEC-027: uses config.get_duckdb_memory_limit_bytes."""
    get_limit = getattr(_cfg, "get_duckdb_memory_limit_bytes", None)
    if get_limit is not None:
        return get_limit("canonical_map", available_bytes)
    # Fallback if config not yet updated.
    _min_gb = getattr(_cfg, "DUCKDB_MEMORY_LIMIT_MIN_GB", 1.0)
    lo = int(_min_gb * 1024**3)
    if available_bytes is None:
        return lo
    frac = getattr(_cfg, "DUCKDB_RAM_FRACTION", 0.45)
    _max_gb = getattr(_cfg, "DUCKDB_MEMORY_LIMIT_MAX_GB", 24.0)
    hi = int(_max_gb * 1024**3)
    return max(lo, min(hi, int(available_bytes * frac)))


def build_canonical_links_and_dummy_from_duckdb(
    session_parquet_path: Path,
    train_end: datetime,
) -> Tuple[pd.DataFrame, Set[int]]:
    """Build links (player_id, casino_player_id, lud_dtm) and FND-12 dummy set from session Parquet via DuckDB.

    PLAN canonical-mapping-full-history Step 2. Uses FND-01 dedup, FND-02/FND-04 DQ,
    FND-03 (CASINO_PLAYER_ID_CLEAN_SQL), FND-12 dummy detection. train_end should be
    timezone-consistent with the Parquet session timestamps (naive with naive data).

    Parameters
    ----------
    session_parquet_path : Path
        Path to gmwds_t_session.parquet (or equivalent).
    train_end : datetime
        Cutoff: only sessions with COALESCE(session_end_dtm, lud_dtm) <= train_end are used.

    Returns
    -------
    links_df : DataFrame with columns [player_id, casino_player_id, lud_dtm]
    dummy_pids : set of player_id (FND-12 dummy/fake-account IDs to exclude)
    """
    try:
        import duckdb
    except ImportError as e:
        raise RuntimeError(
            "build_canonical_links_and_dummy_from_duckdb requires duckdb; install with: pip install duckdb"
        ) from e

    path = Path(session_parquet_path).resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Session Parquet not found: {path}")

    # Required columns for view and filters (Round 253 Review #1).
    # PLAN canonical-step3-schema-check-oom: read only Parquet metadata to get column names,
    # do not load any rows (pd.read_parquet(columns=...) still loads all rows and can OOM on huge session files).
    required = set(_CANONICAL_MAP_SESSION_COLS)
    try:
        import pyarrow.parquet as _pq_sess
        schema_names = set(_pq_sess.read_schema(path).names)
    except Exception as e:
        raise ValueError(f"Session Parquet schema read failed: {e}") from e
    missing = required - schema_names
    if missing:
        raise ValueError(f"Session Parquet missing required columns: {sorted(missing)}")

    # Config validation (Round 253 Review #4). DEC-027: threads from get_duckdb_memory_config("canonical_map"); accept numeric like Step 7.
    get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
    if get_cfg is not None:
        threads = get_cfg("canonical_map")[4]
    else:
        threads = getattr(_cfg, "CANONICAL_MAP_DUCKDB_THREADS", 1)
    try:
        threads = max(1, int(threads))
    except (TypeError, ValueError):
        raise ValueError("CANONICAL_MAP_DUCKDB_THREADS must be a positive integer")

    clean_sql = getattr(_cfg, "CASINO_PLAYER_ID_CLEAN_SQL", None) or CASINO_PLAYER_ID_CLEAN_SQL
    if ";" in (clean_sql or ""):
        raise ValueError("CASINO_PLAYER_ID_CLEAN_SQL must not contain semicolon")

    path_escaped = str(path).replace("'", "''")
    # train_end: use naive for SQL literal; caller must pass timezone-consistent value
    _te = train_end
    if hasattr(_te, "tzinfo") and _te.tzinfo is not None:
        _te = pd.Timestamp(_te).tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    cutoff_str = pd.Timestamp(_te).strftime("%Y-%m-%d %H:%M:%S")
    placeholder = PLACEHOLDER_PLAYER_ID

    # FND-01 dedup: ORDER BY lud_dtm (__etl_insert_Dtm optional; not in _CANONICAL_MAP_SESSION_COLS)
    cte = f"""WITH deduped AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY session_id
            ORDER BY lud_dtm DESC NULLS LAST
        ) AS rn
    FROM read_parquet('{path_escaped}')
)"""
    links_sql = f"""
{cte}
SELECT player_id,
       ({clean_sql}) AS casino_player_id,
       lud_dtm
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {placeholder}
  AND COALESCE(session_end_dtm, lud_dtm) <= '{cutoff_str}'
  AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
  AND ({clean_sql}) IS NOT NULL"""
    dummy_sql = f"""
{cte}
SELECT player_id
FROM deduped
WHERE rn = 1
  AND is_manual = 0
  AND is_deleted = 0 AND is_canceled = 0
  AND player_id IS NOT NULL AND player_id != {placeholder}
  AND COALESCE(session_end_dtm, lud_dtm) <= '{cutoff_str}'
  AND (COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0)
GROUP BY player_id
HAVING COUNT(session_id) = 1
   AND SUM(COALESCE(num_games_with_wager, 0)) <= 1"""

    # Align with Step 7: temp_directory (spill to disk over memory_limit), preserve_insertion_order=false (PLAN Canonical mapping DuckDB 對齊 Step 7)
    temp_dir_raw = str(DATA_DIR / "duckdb_tmp")
    if "'" in temp_dir_raw:
        temp_dir = str(DATA_DIR / "duckdb_tmp")
    else:
        temp_dir = temp_dir_raw
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    temp_dir_sql = temp_dir.replace("'", "''")

    # Dynamic RAM budget (PLAN Canonical mapping DuckDB 對齊 Step 7)
    try:
        import psutil as _psutil
        _avail = _psutil.virtual_memory().available
    except Exception:
        _avail = None
    budget_bytes = _compute_canonical_map_duckdb_budget(_avail)
    mem_gb = budget_bytes / 1024**3

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"SET memory_limit = '{mem_gb}GB'")
        con.execute(f"SET threads = {int(threads)}")
        try:
            con.execute(f"SET temp_directory = '{temp_dir_sql}'")
        except Exception as exc:
            logger.warning("Canonical mapping DuckDB SET temp_directory failed (non-fatal): %s", exc)
        try:
            con.execute("SET preserve_insertion_order = false")
        except Exception as exc:
            logger.warning("Canonical mapping DuckDB SET preserve_insertion_order failed (non-fatal): %s", exc)
        logger.info(
            "Canonical mapping DuckDB runtime: memory_limit=%.2fGB  threads=%d  temp_directory=%s",
            mem_gb, int(threads), temp_dir,
        )
        try:
            links_df = con.execute(links_sql).df()
            dummy_df = con.execute(dummy_sql).df()
        except Exception as exc:
            _hint = (
                " If OOM: ensure temp_directory is writable, or reduce CANONICAL_MAP_DUCKDB_THREADS / "
                "memory limit; see PLAN Canonical mapping DuckDB 對齊 Step 7."
            )
            raise RuntimeError(
                f"Canonical mapping DuckDB query failed: {exc!s}.{_hint}"
            ) from exc
        dummy_pids: Set[int] = set() if dummy_df.empty else set(dummy_df["player_id"].astype(int).tolist())
        return (links_df, dummy_pids)
    finally:
        con.close()


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
    casino_win,
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

    # Pull extra history so Track Human state machines (loss_streak, run_boundary)
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
            f"Session Parquet missing: {sess_path}. "
            "Export ClickHouse t_session to data/ (gmwds_t_session.parquet) or run without --use-local-parquet."
        )
    if not sessions_only and not bets_path.exists():
        raise FileNotFoundError(
            f"Bet Parquet missing: {bets_path}. "
            "Export ClickHouse t_bet to data/ (gmwds_t_bet.parquet) or run without --use-local-parquet."
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
        # Column pushdown: only load _REQUIRED_BET_PARQUET_COLS to cut RAM by ~2/3 vs
        # loading all ~60 t_bet columns (OOM fix — 17 object columns were the final straw).
        bets_lo = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
        import pyarrow.parquet as _pq_bets
        _bet_schema_cols = set(_pq_bets.read_schema(bets_path).names)
        _bet_cols = [c for c in _REQUIRED_BET_PARQUET_COLS if c in _bet_schema_cols]
        bets = pd.read_parquet(
            bets_path,
            columns=_bet_cols,
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
    use_local_parquet: bool = False,  # kept for backward-compat; prefers local Parquet when available
    canonical_ids: Optional[List[str]] = None,
) -> Optional[pd.DataFrame]:
    """Load player_profile snapshots covering the training window.

    Primary path: local Parquet (data/player_profile.parquet), built by
    etl_player_profile.py.  Falls back to ClickHouse with a chunked-IN
    strategy when the local artifact is absent and use_local_parquet=False.

    The ClickHouse path splits large canonical_id lists into batches of
    _IN_BATCH IDs per SQL IN (...) clause and merges results with pd.concat.
    No DDL permissions (temp-table creation) are required.

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
        Prefer local Parquet artifact; skip ClickHouse fallback even when the
        file is missing.
    canonical_ids:
        R82: optional list of canonical_id values to filter the profile table.
        Pass the full set of rated player IDs from canonical_map to cap memory
        usage; None loads all players in the time window.
    """
    _IN_BATCH = 4_000  # keep each IN(...) list well under ClickHouse 256 KB max_query_size

    # R222 Review #2: empty canonical_ids → no profile load (avoid full-table read when no rated players).
    if canonical_ids is not None and len(canonical_ids) == 0:
        return None

    # --- Primary path: local Parquet (ETL artifact from etl_player_profile.py) ---
    profile_path = LOCAL_PARQUET_DIR / "player_profile.parquet"
    if use_local_parquet or profile_path.exists():
        if not profile_path.exists():
            logger.info(
                "player_profile: %s not found -- run etl_player_profile.py first. "
                "Profile features will be NaN for this run.",
                profile_path,
            )
            return None
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
            if df.empty:
                logger.info(
                    "player_profile: no snapshot rows found in window %s - %s; "
                    "profile features will be NaN.",
                    window_start.date(), window_end.date(),
                )
                return None
            logger.info("player_profile: %d rows loaded from local Parquet", len(df))
            return df
        except Exception as exc:
            logger.warning("player_profile local Parquet load failed: %s", exc)
            return None

    # --- Fallback path: ClickHouse with chunked-IN strategy ---
    # Used when local Parquet artifact is absent and use_local_parquet=False.
    # Three branches based on canonical_ids size:
    #   Branch 1 (_query_no_filter): canonical_ids is None -> load all IDs in window
    #   Branch 2: small list          -> single IN clause
    #   Branch 3: large list          -> chunked IN batches with pd.concat
    from datetime import timedelta as _td_ch
    _snap_lo_s = (window_start - _td_ch(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    _snap_hi_s = window_end.strftime("%Y-%m-%d %H:%M:%S")
    _BASE_SQL = (
        "SELECT * "
        "FROM " + SOURCE_DB + "." + TPROFILE + " "
        "WHERE snapshot_dtm >= '" + _snap_lo_s + "' "
        "AND snapshot_dtm <= '" + _snap_hi_s + "'"
    )
    client = get_clickhouse_client()

    if canonical_ids is None:
        _query_no_filter = _BASE_SQL
        try:
            df = client.query_df(_query_no_filter)
        except Exception as exc:
            logger.warning("player_profile ClickHouse query failed: %s", exc)
            return None
        if df.empty:
            return None
        return df.sort_values(["canonical_id", "snapshot_dtm"]).reset_index(drop=True)

    _cid_list = [str(c) for c in canonical_ids]
    if len(_cid_list) <= _IN_BATCH:
        # Small list: single IN clause avoids chunked overhead
        _cids_str = ", ".join("'" + c + "'" for c in _cid_list)
        _small_query = _BASE_SQL + " AND canonical_id IN (" + _cids_str + ")"
        try:
            df = client.query_df(_small_query)
        except Exception as exc:
            logger.warning("player_profile ClickHouse query failed: %s", exc)
            return None
        if df.empty:
            return None
        return df.sort_values(["canonical_id", "snapshot_dtm"]).reset_index(drop=True)

    # Large list: chunked IN with pd.concat
    logger.info(
        "player_profile: %d canonical_ids -> chunked IN strategy (%d IDs per batch)",
        len(_cid_list), _IN_BATCH,
    )
    _parts = []
    _n_batches = (len(_cid_list) + _IN_BATCH - 1) // _IN_BATCH
    for _i in range(0, len(_cid_list), _IN_BATCH):
        _batch = _cid_list[_i: _i + _IN_BATCH]
        _batch_num = _i // _IN_BATCH + 1
        logger.info(
            "player_profile: batch %d/%d (%d IDs)",
            _batch_num, _n_batches, len(_batch),
        )
        _cids_str = ", ".join("'" + c + "'" for c in _batch)
        _batch_query = _BASE_SQL + " AND canonical_id IN (" + _cids_str + ")"
        try:
            _parts.append(client.query_df(_batch_query))
        except Exception as _exc:
            logger.error(
                "player_profile batch %d/%d failed: %s",
                _batch_num, _n_batches, _exc,
            )
    df = pd.concat(_parts, ignore_index=True) if _parts else pd.DataFrame()
    if df.empty:
        return None
    df = df.sort_values(["canonical_id", "snapshot_dtm"]).reset_index(drop=True)
    return df


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


# Month-end schedule: shared with etl_player_profile CLI (--month-end). See PLAN § CLI for month-end-only player_profile.


def ensure_player_profile_ready(
    window_start: datetime,
    window_end: datetime,
    use_local_parquet: bool = False,
    canonical_id_whitelist: Optional[set] = None,
    snapshot_interval_days: int = 1,
    preload_sessions: bool = True,
    canonical_map: Optional[pd.DataFrame] = None,
    max_lookback_days: int = 365,
) -> None:
    """Auto-check profile table freshness and rebuild missing local ranges if needed.

    Local-parquet training mode only:
      1) determine required snapshot window for PIT join,
      2) compare against existing player_profile coverage,
      3) auto-run helper script to backfill missing range(s).

    Parameters
    ----------
    canonical_id_whitelist:
        When provided, passed to ``backfill`` to restrict profiling to the
        sampled rated player set.  Also triggers in-process backfill (avoids
        subprocess overhead and allows the whitelist to be passed directly).
    snapshot_interval_days:
        Deprecated for scheduling.  Month-end scheduling is now enforced in all
        modes.  This value is still forwarded for backward compatibility, but
        it does not control snapshot date selection.
    preload_sessions:
        Forwarded to ``backfill``.  Set False (--no-preload) to disable
        full-table session preload, using per-day PyArrow pushdown reads
        instead.  Reduces peak RAM at the cost of more disk I/O.
    canonical_map:
        Pre-built player_id -> canonical_id mapping DataFrame from
        trainer.py.  Forwarded to ``backfill`` so the ETL does not
        redundantly search for ``canonical_mapping.parquet`` on disk.
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
        # R106/R200: add population-mode and horizon indicators so caches with
        # different lookback settings do not mix.
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

    # OPT-001: Use the nearest month-end on or before window_start as required_start.
    # This ensures the PIT join has a valid anchor snapshot for bets in the first
    # (possibly partial) month of the training window, while avoiding building a
    # full year of stale snapshots that are never actually used.
    #
    # Rationale: join_player_profile uses merge_asof(direction="backward"), so a bet
    # on Feb 15 needs the Jan 31 snapshot.
    required_start = latest_month_end_on_or_before(window_start.date())
    required_end = window_end.date()

    session_rng = _parquet_date_range(
        session_path,
        ["gaming_day", "session_end_dtm", "lud_dtm", "session_start_dtm"],
    )
    if session_rng:
        _pre_clamp_start = required_start
        required_start = max(required_start, session_rng[0])
        if required_start > _pre_clamp_start:
            logger.warning(
                "OPT-001 anchor clamp: session parquet starts at %s, which is after the "
                "ideal anchor snapshot date %s.  Bets between %s and the first available "
                "month-end snapshot may have NaN profile features.",
                session_rng[0],
                _pre_clamp_start,
                window_start.date(),
            )
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
        _snap_dates = month_end_dates(miss_start, miss_end) if effective_month_end else None
        # If the missing range is intra-month (no month-end within range), anchor
        # PIT with the most recent month-end on/before miss_end.
        if _snap_dates is not None and len(_snap_dates) == 0:
            _anchor = latest_month_end_on_or_before(miss_end)
            _snap_dates = [_anchor]
            _backfill_start = min(_backfill_start, _anchor)
            logger.info(
                "Month-end-only schedule: intra-month missing range %s -> %s; "
                "building anchor snapshot at %s.",
                miss_start, miss_end, _anchor,
            )

        # Use in-process backfill when any of:
        # (a) canonical_map already in memory — a subprocess cannot receive a
        #     Python DataFrame object, so in-process is the only path that can
        #     forward the pre-built map (eliminates "No local
        #     canonical_mapping.parquet" warning).
        # (b) canonical_id_whitelist provided — avoids subprocess overhead and
        #     allows the whitelist to be forwarded directly without CLI
        #     serialisation.
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
            # R105: auto_script check only for subprocess path; in-process
            # backfill does not need the script.
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
    # R111: when snapshot_interval_days > 1 or month-end scheduling, date gaps
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
        Track Human state machines cross-chunk context.  Defaults to window_start.

    Notes
    -----
    When ``bets`` is empty (e.g. sessions-only DQ path used when building the
    canonical mapping), the bets processing block is skipped entirely and only
    session DQ filters are applied.  This avoids a ``KeyError`` on
    ``payout_complete_dtm`` when a caller passes a stub DataFrame.
    """
    # --- sessions (FND-01 / FND-02 / FND-04) — applied first so that the
    # bets.empty early-return path still yields clean session data.
    session_dt_cols: Dict[str, pd.Series] = {}
    for dt_col in ("session_start_dtm", "session_end_dtm", "lud_dtm"):
        if dt_col in sessions.columns:
            session_dt_cols[dt_col] = pd.to_datetime(
                sessions[dt_col], utc=False, errors="coerce"
            )

    session_id_num = pd.to_numeric(
        sessions["session_id"] if "session_id" in sessions.columns else pd.Series(np.nan, index=sessions.index),
        errors="coerce",
    )
    player_id_num = pd.to_numeric(
        sessions["player_id"] if "player_id" in sessions.columns else pd.Series(np.nan, index=sessions.index),
        errors="coerce",
    )
    _valid_session_id_mask = session_id_num.notna()
    sessions = sessions.loc[_valid_session_id_mask].copy()
    for dt_col, normalized in session_dt_cols.items():
        sessions[dt_col] = normalized.loc[_valid_session_id_mask].to_numpy()
    sessions["session_id"] = session_id_num.loc[_valid_session_id_mask].to_numpy()
    sessions["player_id"] = player_id_num.loc[_valid_session_id_mask].to_numpy()

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

    # FND-02 + FND-04 (A10): single combined mask then one .copy().
    # FND-02: exclude manual adjustment sessions and soft-deleted rows.
    dq_mask = (
        (sessions["is_manual"] == 0)
        & (sessions["is_deleted"] == 0)
        & (sessions["is_canceled"] == 0)
    )
    # FND-04: exclude ghost sessions with no real wager activity (SSOT §5).
    if "turnover" in sessions.columns or "num_games_with_wager" in sessions.columns:
        _turnover = sessions.get(
            "turnover", pd.Series(0.0, index=sessions.index)
        ).fillna(0)
        _games = sessions["num_games_with_wager"].fillna(0)
        dq_mask = dq_mask & ((_turnover > 0) | (_games > 0))
    sessions = sessions.loc[dq_mask].copy()

    if bets.empty:
        # Sessions-only path — return clean sessions, skip bets processing entirely.
        # This avoids a KeyError on payout_complete_dtm when called with a stub DataFrame.
        return bets, sessions

    # --- bets ---
    payout_complete_dtm = pd.to_datetime(bets["payout_complete_dtm"], utc=False)

    # R23: Timezone normalisation — tz_localize naive, tz_convert aware to HK,
    # then strip tz so downstream callers (labels, features) receive tz-naive
    # HK local time and no naive/aware TypeError can occur at the boundary.
    if payout_complete_dtm.dt.tz is None:
        payout_complete_dtm = payout_complete_dtm.dt.tz_localize(
            HK_TZ, nonexistent="shift_forward", ambiguous="NaT"
        )
    else:
        payout_complete_dtm = payout_complete_dtm.dt.tz_convert(HK_TZ)
    # Strip tz after normalization — downstream (compute_labels, features) is tz-naive.
    payout_complete_dtm = payout_complete_dtm.dt.tz_localize(None)
    # DEC-018: unify datetime resolution to ns so merge_asof / comparisons always see
    # the same dtype regardless of Parquet file's stored precision ([ms] vs [us]).
    payout_complete_dtm = payout_complete_dtm.astype("datetime64[ns]")

    # Boundary comparison — both sides are tz-naive after DEC-018 process_chunk strip.
    # The explicit .replace(tzinfo=None) guards here are kept as a defensive fallback
    # for callers that bypass process_chunk (e.g. backtester, tests).
    _lo = bets_history_start if bets_history_start is not None else window_start
    _lo = _lo.replace(tzinfo=None) if getattr(_lo, "tzinfo", None) else _lo
    _hi = extended_end.replace(tzinfo=None) if getattr(extended_end, "tzinfo", None) else extended_end

    # Key numeric only; table_id is categorical after normalizer (PLAN § apply_dq 配合修改).
    numeric_key_cols: Dict[str, pd.Series] = {}
    for col in ("bet_id", "session_id", "player_id"):
        if col in bets.columns:
            numeric_key_cols[col] = pd.to_numeric(bets.get(col), errors="coerce")

    # Build the keep-mask from normalized Series first, then copy only surviving rows.
    # This avoids an eager full-frame copy of the Step 6 bets chunk before we know
    # which rows survive DQ.
    _dq_mask = (
        payout_complete_dtm.between(_lo, _hi, inclusive="left")
        & payout_complete_dtm.notna()
        & numeric_key_cols["bet_id"].notna()
        & numeric_key_cols["session_id"].notna()
    )
    if "wager" in bets.columns:
        # Defense-in-depth wager guard (R1602): applied inside the combined mask.
        _dq_mask &= bets["wager"].fillna(0).gt(0)
    bets = bets.loc[_dq_mask].copy().reset_index(drop=True)
    bets["payout_complete_dtm"] = payout_complete_dtm.loc[_dq_mask].to_numpy()
    for col, coerced in numeric_key_cols.items():
        bets[col] = coerced.loc[_dq_mask].to_numpy()

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
        ].reset_index(drop=True)

    # Ensure gaming_day exists (fallback: date of payout)
    if "gaming_day" not in bets.columns:
        bets["gaming_day"] = pd.to_datetime(bets["payout_complete_dtm"]).dt.date

    # Ensure status column exists (for loss_streak)
    if "status" not in bets.columns:
        bets["status"] = None

    # Numeric guard for legacy features; skip columns already categorical (PLAN § apply_dq 配合修改).
    for col in ("wager", "payout_odds", "base_ha", "is_back_bet", "position_idx", "casino_win"):
        if col not in bets.columns:
            continue
        if isinstance(bets[col].dtype, pd.CategoricalDtype):
            continue
        bets[col] = pd.to_numeric(bets[col], errors="coerce").fillna(0)

    # DEC-018 / R23 contract assertion: payout_complete_dtm must leave apply_dq tz-naive.
    if not bets.empty and "payout_complete_dtm" in bets.columns:
        assert bets["payout_complete_dtm"].dt.tz is None, \
            "R23 violation: payout_complete_dtm must be tz-naive after DQ"

    return bets, sessions


# ---------------------------------------------------------------------------
# Track Human feature computation
# ---------------------------------------------------------------------------

def add_track_human_features(
    bets: pd.DataFrame,
    canonical_map: pd.DataFrame,
    window_end: datetime,
    lookback_hours: Optional[float] = None,
) -> pd.DataFrame:
    """Return a copy of *bets* with Track Human feature columns attached.

    A copy is taken so the caller's DataFrame is not mutated.  After column
    pushdown, ``bets`` is already narrow (~20 cols), so the copy cost is low.
    When ``lookback_hours`` is set (e.g. SCORER_LOOKBACK_HOURS), Track Human
    features use only bets in (row_time - lookback_hours, row_time] for
    train–serve parity with scorer.
    """
    df = bets.copy()

    if "canonical_id" not in df.columns:
        logger.warning("canonical_id missing; Track Human features will be zeros")
        df["loss_streak"] = 0
        df["run_id"] = 0
        df["minutes_since_run_start"] = 0.0
        df["bets_in_run_so_far"] = 0
        df["wager_sum_in_run_so_far"] = 0.0
        df["net_win_in_run_so_far"] = 0.0
        df["net_win_per_bet_in_run"] = 0.0
        return df

    # loss_streak (cutoff = window_end so future bets don't influence streak)
    streak = compute_loss_streak(df, cutoff_time=window_end, lookback_hours=lookback_hours)
    df["loss_streak"] = streak.reindex(df.index, fill_value=0)

    # run_boundary (cutoff = window_end); reindex so rows beyond cutoff get 0 not NaN (Review #2)
    run_df = compute_run_boundary(df, cutoff_time=window_end, lookback_hours=lookback_hours)
    df["run_id"] = run_df["run_id"].reindex(df.index, fill_value=0).values
    df["minutes_since_run_start"] = run_df["minutes_since_run_start"].reindex(df.index, fill_value=0.0).values
    df["bets_in_run_so_far"] = run_df["bets_in_run_so_far"].reindex(df.index, fill_value=0).values
    df["wager_sum_in_run_so_far"] = run_df["wager_sum_in_run_so_far"].reindex(df.index, fill_value=0.0).values
    df["net_win_in_run_so_far"] = run_df["net_win_in_run_so_far"].reindex(df.index, fill_value=0.0).values
    df["net_win_per_bet_in_run"] = run_df["net_win_per_bet_in_run"].reindex(df.index, fill_value=0.0).values

    return df


# ---------------------------------------------------------------------------
# Chunk processing
# ---------------------------------------------------------------------------

def _chunk_parquet_path(chunk: dict) -> Path:
    ws = chunk["window_start"].strftime("%Y%m%d")
    we = chunk["window_end"].strftime("%Y%m%d")
    return CHUNK_DIR / f"chunk_{ws}_{we}.parquet"


def _chunk_prefeatures_parquet_path(chunk: dict) -> Path:
    """Task 7 R6: Parquet of bets after Track Human, before Track LLM."""
    ws = chunk["window_start"].strftime("%Y%m%d")
    we = chunk["window_end"].strftime("%Y%m%d")
    return CHUNK_DIR / f"chunk_{ws}_{we}.prefeatures.parquet"


def _chunk_prefeatures_sidecar_path(chunk: dict) -> Path:
    """Sidecar for :func:`_chunk_prefeatures_parquet_path` (same fingerprint pipe format)."""
    ws = chunk["window_start"].strftime("%Y%m%d")
    we = chunk["window_end"].strftime("%Y%m%d")
    return CHUNK_DIR / f"chunk_{ws}_{we}.prefeatures.cache_key"


# Sentinel in ``feature_spec_hash`` slot for R6 pre-LLM stage keys (not a real spec hash).
_CHUNK_PREFEATURES_SPEC_PLACEHOLDER = "__pre_llm__"


def _prefeatures_cache_components(components: dict) -> dict:
    """Task 7 R6: key material for post-Track-Human bets (LLM spec + neg-sample excluded)."""
    return {
        **components,
        "feature_spec_hash": _CHUNK_PREFEATURES_SPEC_PLACEHOLDER,
        "neg_sample_frac": 1.0,
    }


def _chunk_two_stage_cache_enabled() -> bool:
    """R6 prefeatures cache: default from ``trainer.core.config`` (on); env overrides."""
    return bool(_core_trainer_config.chunk_two_stage_cache_enabled())


def _bump_chunk_cache_stat(stats: Optional[Dict[str, int]], key: str) -> None:
    """Increment optional Step 6 cache counters for pipeline_diagnostics (Task 7 DoD)."""
    if stats is None:
        return
    stats[key] = stats.get(key, 0) + 1


def _oom_check_and_adjust_neg_sample_frac(
    chunks: list,
    current_frac: float,
) -> float:
    """Estimate Step 7 peak RAM after Step 1; auto-reduce NEG_SAMPLE_FRAC if OOM is likely.

    Called immediately after the chunk list is finalised so the user sees a
    warning — and any auto-adjustment — before the slow Step 6 data loading
    begins.

    Logic:
    1. Skip if NEG_SAMPLE_FRAC_AUTO is False.
    2. Try psutil for available RAM; skip gracefully if not installed.
    3. Estimate per-chunk on-disk size from cached chunk Parquets (if any),
       otherwise fall back to NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT.
    4. estimated_peak_ram = N_chunks × per_chunk_bytes × CHUNK_CONCAT_RAM_FACTOR
       × (1 + TRAIN_SPLIT_FRAC)  (full_df and train split coexist at Step 7 peak).
    5. Print a one-line summary so user can see the estimate.
    6. If peak ≤ budget: no change.
    7. If current_frac < 1.0 (user-configured): warn only, do not override.
    8. Otherwise compute the auto frac from the algebra:
         rows_factor = pos_rate + frac × (1 - pos_rate)
         need rows_factor ≤ budget / estimated_peak_ram
         → frac = (budget/peak - pos_rate) / (1 - pos_rate)
       Clamp to [NEG_SAMPLE_FRAC_MIN, 1.0].

    Returns the effective frac to use for the pipeline run.
    """
    if not NEG_SAMPLE_FRAC_AUTO:
        logger.info("OOM-check: NEG_SAMPLE_FRAC_AUTO=False — skipping")
        return current_frac

    try:
        import psutil as _psutil
        _vmem = _psutil.virtual_memory()
        available_ram = _vmem.available
        total_ram = _vmem.total
    except Exception as _e:
        logger.warning("OOM-check: psutil unavailable (%s); skipping RAM pre-check.", _e)
        print("[OOM-check] psutil unavailable; skipping RAM pre-check.", flush=True)
        return current_frac

    # R-NEG-4: validate ASSUMED_POS_RATE is in (0, 1) before using in formula.
    # p ≥ 1.0 causes division by zero; p ≤ 0.0 degenerates the formula.
    if not (0.0 < NEG_SAMPLE_FRAC_ASSUMED_POS_RATE < 1.0):
        logger.warning(
            "OOM-check: NEG_SAMPLE_FRAC_ASSUMED_POS_RATE=%.2f out of valid range (0, 1); "
            "falling back to 0.15",
            NEG_SAMPLE_FRAC_ASSUMED_POS_RATE,
        )
        p = 0.15
    else:
        p = NEG_SAMPLE_FRAC_ASSUMED_POS_RATE

    # --- Estimate per-chunk on-disk size ---
    # R-371-3: only include chunks whose .cache_key sidecar exists so that chunks
    # whose cache key will mismatch (and therefore be recomputed at full size) do
    # not silently deflate the estimate with their old downsampled Parquet size.
    existing_sizes = [
        _chunk_parquet_path(c).stat().st_size
        for c in chunks
        if _chunk_parquet_path(c).exists()
        and _chunk_parquet_path(c).with_suffix(".cache_key").exists()
    ]
    if existing_sizes:
        per_chunk_bytes = sum(existing_sizes) / len(existing_sizes)
        size_source = f"avg of {len(existing_sizes)}/{len(chunks)} cached chunk Parquets (with .cache_key sidecar)"
    else:
        per_chunk_bytes = NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT
        size_source = f"default estimate ({NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT // (1024**2)} MB/chunk; no cached chunks with valid .cache_key)"

    n_chunks = len(chunks)
    estimated_on_disk = per_chunk_bytes * n_chunks
    # Step 7 peak: when STEP7_USE_DUCKDB we only read back the largest split (train);
    # when pandas path, full_df and train split coexist (PLAN Step 6).
    if STEP7_USE_DUCKDB:
        estimated_peak_ram = estimated_on_disk * CHUNK_CONCAT_RAM_FACTOR * TRAIN_SPLIT_FRAC
    else:
        # R-371-7: full_df AND train split coexist in memory.
        estimated_peak_ram = estimated_on_disk * CHUNK_CONCAT_RAM_FACTOR * (1.0 + TRAIN_SPLIT_FRAC)
    ram_budget = available_ram * NEG_SAMPLE_RAM_SAFETY

    # R-NEG-3/5: include total RAM alongside available so operators can judge
    # whether "available" is temporarily low (e.g. OS cache) vs genuinely tight.
    print(
        f"[OOM-check] {n_chunks} chunk(s) × {per_chunk_bytes / (1024**2):.0f} MB"
        f" × {CHUNK_CONCAT_RAM_FACTOR}x factor"
        f" → est. Step 7 peak RAM {estimated_peak_ram / (1024**3):.1f} GB"
        f" | total {total_ram / (1024**3):.1f} GB | available {available_ram / (1024**3):.1f} GB"
        f" | budget ({NEG_SAMPLE_RAM_SAFETY:.0%}) {ram_budget / (1024**3):.1f} GB"
        f"  [{size_source}]",
        flush=True,
    )
    logger.info(
        "OOM-check: %d chunks est. peak %.1f GB  total %.1f GB  available %.1f GB  budget %.1f GB  (%s)",
        n_chunks,
        estimated_peak_ram / (1024**3),
        total_ram / (1024**3),
        available_ram / (1024**3),
        ram_budget / (1024**3),
        size_source,
    )

    if estimated_peak_ram <= ram_budget:
        print("[OOM-check] RAM looks OK - no adjustment to NEG_SAMPLE_FRAC.", flush=True)
        logger.info("OOM-check: peak %.1f GB <= budget %.1f GB -- no adjustment", estimated_peak_ram / (1024**3), ram_budget / (1024**3))
        return current_frac

    # OOM is likely.
    if current_frac < 1.0:
        print(
            f"[OOM-check] WARNING: estimated peak {estimated_peak_ram / (1024**3):.1f} GB"
            f" > budget {ram_budget / (1024**3):.1f} GB, but NEG_SAMPLE_FRAC={current_frac:.2f}"
            f" is already user-configured — not overriding. Consider lowering it further.",
            flush=True,
        )
        logger.warning(
            "OOM-check: est. peak %.1f GB > budget %.1f GB but NEG_SAMPLE_FRAC=%.2f is user-set — not overriding",
            estimated_peak_ram / (1024**3), ram_budget / (1024**3), current_frac,
        )
        return current_frac

    # Auto-compute frac:  rows_factor = p + frac*(1-p)  where p = assumed positive rate.
    # Need: estimated_peak_ram * rows_factor ≤ ram_budget
    # → frac ≤ (ram_budget/estimated_peak_ram - p) / (1-p)
    # p is already validated/defaulted above.
    needed_factor = ram_budget / estimated_peak_ram   # fraction of total rows needed
    raw_frac = (needed_factor - p) / (1.0 - p)
    auto_frac = max(NEG_SAMPLE_FRAC_MIN, min(1.0, raw_frac))

    _warn_floor = raw_frac < NEG_SAMPLE_FRAC_MIN
    print(
        f"[OOM-check] *** OOM RISK: est. peak {estimated_peak_ram / (1024**3):.1f} GB"
        f" > budget {ram_budget / (1024**3):.1f} GB ***\n"
        f"  Auto-adjusting NEG_SAMPLE_FRAC: 1.0 → {auto_frac:.2f}"
        f"  (assumed pos_rate={p:.0%}, floor={NEG_SAMPLE_FRAC_MIN})"
        + (f"\n  *** Floor hit: even frac={NEG_SAMPLE_FRAC_MIN} may not be enough —"
           f" consider reducing --days / --recent-chunks ***" if _warn_floor else "")
        + "\n  To disable: set NEG_SAMPLE_FRAC_AUTO=False in config.py",
        flush=True,
    )
    logger.warning(
        "OOM-check: auto-adjusting NEG_SAMPLE_FRAC 1.0 -> %.2f  (peak %.1f GB > budget %.1f GB)",
        auto_frac, estimated_peak_ram / (1024**3), ram_budget / (1024**3),
    )
    return auto_frac


def _oom_check_after_chunk1(
    per_chunk_bytes: int,
    n_chunks: int,
    current_frac: float,
) -> float:
    """Re-estimate Step 7 peak RAM using chunk 1 actual on-disk size; optionally lower frac.

    Called after processing chunk 1 with neg_sample_frac=1.0 (OOM probe). Uses the same
    formula and constants as _oom_check_and_adjust_neg_sample_frac. Logs include
    \"(chunk 1 size)\" to distinguish from the Step 1 pre-check.

    Returns the effective NEG_SAMPLE_FRAC to use for the rest of the run.
    """
    if not NEG_SAMPLE_FRAC_AUTO:
        return current_frac
    try:
        import psutil as _psutil
        _vmem = _psutil.virtual_memory()
        available_ram = _vmem.available
    except Exception as _e:
        logger.warning("OOM-check (chunk 1 size): psutil unavailable (%s); skipping", _e)
        return current_frac

    if not (0.0 < NEG_SAMPLE_FRAC_ASSUMED_POS_RATE < 1.0):
        p = 0.15
    else:
        p = NEG_SAMPLE_FRAC_ASSUMED_POS_RATE

    estimated_on_disk = per_chunk_bytes * n_chunks
    if STEP7_USE_DUCKDB:
        estimated_peak_ram = estimated_on_disk * CHUNK_CONCAT_RAM_FACTOR * TRAIN_SPLIT_FRAC
    else:
        estimated_peak_ram = estimated_on_disk * CHUNK_CONCAT_RAM_FACTOR * (1.0 + TRAIN_SPLIT_FRAC)
    ram_budget = available_ram * NEG_SAMPLE_RAM_SAFETY

    print(
        f"[OOM-check (chunk 1 size)] {n_chunks} chunk(s) x {per_chunk_bytes / (1024**2):.0f} MB"
        f" -> est. Step 7 peak RAM {estimated_peak_ram / (1024**3):.1f} GB"
        f" | budget {ram_budget / (1024**3):.1f} GB",
        flush=True,
    )
    logger.info(
        "OOM-check (chunk 1 size): %d chunks x %.0f MB -> est. peak %.1f GB  budget %.1f GB",
        n_chunks, per_chunk_bytes / (1024**2), estimated_peak_ram / (1024**3), ram_budget / (1024**3),
    )

    if estimated_peak_ram <= ram_budget:
        print("[OOM-check (chunk 1 size)] RAM looks OK - no adjustment.", flush=True)
        logger.info("OOM-check (chunk 1 size): peak <= budget — no adjustment")
        return current_frac

    if current_frac < 1.0:
        print(
            f"[OOM-check (chunk 1 size)] WARNING: est. peak {estimated_peak_ram / (1024**3):.1f} GB"
            f" > budget {ram_budget / (1024**3):.1f} GB, but NEG_SAMPLE_FRAC={current_frac:.2f}"
            " is user-configured — not overriding.",
            flush=True,
        )
        logger.warning(
            "OOM-check (chunk 1 size): est. peak > budget but NEG_SAMPLE_FRAC=%.2f is user-set — not overriding",
            current_frac,
        )
        return current_frac

    needed_factor = ram_budget / estimated_peak_ram
    raw_frac = (needed_factor - p) / (1.0 - p)
    auto_frac = max(NEG_SAMPLE_FRAC_MIN, min(1.0, raw_frac))
    _warn_floor = raw_frac < NEG_SAMPLE_FRAC_MIN
    print(
        f"[OOM-check (chunk 1 size)] *** OOM RISK *** Auto-adjusting NEG_SAMPLE_FRAC: 1.0 -> {auto_frac:.2f}"
        f"  (assumed pos_rate={p:.0%}, floor={NEG_SAMPLE_FRAC_MIN})"
        + (f"\n  *** Floor hit: frac={NEG_SAMPLE_FRAC_MIN} may not be enough ***" if _warn_floor else ""),
        flush=True,
    )
    logger.warning(
        "OOM-check (chunk 1 size): auto-adjusting NEG_SAMPLE_FRAC 1.0 -> %.2f  (peak %.1f GB > budget %.1f GB)",
        auto_frac, estimated_peak_ram / (1024**3), ram_budget / (1024**3),
    )
    return auto_frac


# Task 7 / R3: structured `.cache_key` sidecar version (fingerprint string unchanged).
_CHUNK_CACHE_SIDECAR_VERSION = 1


def _chunk_cache_components(
    chunk: dict,
    bets: Optional[pd.DataFrame] = None,
    profile_hash: str = "none",
    feature_spec_hash: str = "none",
    neg_sample_frac: float = 1.0,
    *,
    data_hash: Optional[str] = None,
) -> dict:
    """Pipeline components that determine chunk cache validity (TRN-07 / Task 7 R3).

    When ``data_hash`` is set (Task 7 R5 local parquet metadata path), ``bets`` may be
    omitted. Otherwise ``bets`` is required and row content is hashed (ClickHouse path).
    """
    ws = chunk["window_start"].isoformat()
    we = chunk["window_end"].isoformat()
    if data_hash is not None:
        _dh = str(data_hash).strip()
        if not _dh:
            raise ValueError("data_hash must be non-empty when provided")
        data_hash = _dh
    elif bets is not None:
        # R1 (Task 7): order-insensitive data fingerprint.
        data_hash = _order_insensitive_bets_hash(bets)
    else:
        raise ValueError("_chunk_cache_components: need bets or data_hash")
    _effective_lookback = getattr(_cfg, "SCORER_LOOKBACK_HOURS", 8)
    cfg_str = json.dumps({
        "WALKAWAY_GAP_MIN": WALKAWAY_GAP_MIN,
        "SESSION_AVAIL_DELAY_MIN": SESSION_AVAIL_DELAY_MIN,
        "HISTORY_BUFFER_DAYS": HISTORY_BUFFER_DAYS,
        "TRACK_HUMAN_LOOKBACK_HOURS": _effective_lookback,
    }, sort_keys=True)
    cfg_hash = hashlib.md5(cfg_str.encode()).hexdigest()[:6]
    return {
        "window_start": ws,
        "window_end": we,
        "data_hash": data_hash,
        "cfg_hash": cfg_hash,
        "profile_hash": profile_hash,
        "feature_spec_hash": feature_spec_hash,
        "neg_sample_frac": float(neg_sample_frac),
    }


def _parquet_stable_rowgroups_schema_digest(meta: Any) -> str:
    """Stable digest from Parquet metadata only (no mtime, no file ``created_by``).

    Incorporates footer ``num_rows``, per-row-group ``num_rows`` / ``total_byte_size``,
    and column path + physical/logical types so copies across machines with different
    mtime still match; schema changes bust the digest without scanning row data.
    """
    rgs: List[List[int]] = []
    for i in range(meta.num_row_groups):
        rg = meta.row_group(i)
        rgs.append([int(rg.num_rows), int(rg.total_byte_size)])
    schema = meta.schema
    # PyArrow: never use FileMetaData.num_columns here — some builds implement it
    # via ParquetSchema.num_columns, which was removed (AttributeError). len(schema)
    # is the portable column count across tested pyarrow versions.
    _n_cols = len(schema)
    cols: List[Tuple[str, str, str]] = []
    for i in range(int(_n_cols)):
        col = schema.column(i)
        path = col.path
        if hasattr(path, "as_tuple"):
            path_key = ".".join(str(x) for x in path.as_tuple())
        elif isinstance(path, str):
            path_key = path
        else:
            path_key = str(path)
        cols.append((path_key, str(col.physical_type), str(col.logical_type)))
    cols.sort(key=lambda t: t[0])
    blob = {
        "columns": [[a, b, c] for a, b, c in cols],
        "fp_v": 2,
        "nrows": int(meta.num_rows),
        "row_groups": rgs,
    }
    raw = json.dumps(blob, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _local_parquet_source_data_hash(
    window_start: datetime,
    extended_end: datetime,
) -> str:
    """Task 7 R5 (+ portable fp_v2): fingerprint local bet/session Parquet without row scan.

    Uses file size, footer ``num_rows``, a stable metadata digest (row groups + schema;
    excludes ``st_mtime`` and file ``created_by``), and the same logical filter bounds as
    ``load_local_parquet`` so chunk keys update when exports or window bounds change.

    **Trade-off**: extreme in-place edits keeping identical Parquet metadata could
    theoretically false-hit; prefer false miss for content changes that alter metadata.
    """
    bets_path = LOCAL_PARQUET_DIR / "gmwds_t_bet.parquet"
    sess_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
    bets_lo = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
    sess_lo = window_start - timedelta(days=1)
    sess_hi = extended_end + timedelta(days=1)
    import pyarrow.parquet as pq

    def _file_token(label: str, p: Path) -> str:
        if not p.exists():
            return f"{label}:missing:{p.name}"
        st = p.stat()
        try:
            meta = pq.read_metadata(p)
            nrows = int(meta.num_rows)
            digest = _parquet_stable_rowgroups_schema_digest(meta)
        except Exception as _meta_exc:
            nrows = -1
            digest = "0" * 16
            logger.warning(
                "Task 7 R5: read_metadata failed for %s (%s): %s",
                p, label, _meta_exc,
            )
        return f"{label}|{p.name}|{st.st_size}|{nrows}|{digest}"

    payload = json.dumps({
        "bet_filter_lo": bets_lo.isoformat(),
        "bet_filter_hi": extended_end.isoformat(),
        "sess_filter_lo": sess_lo.isoformat(),
        "sess_filter_hi": sess_hi.isoformat(),
        "bet_file": _file_token("bet", bets_path),
        "sess_file": _file_token("sess", sess_path),
    }, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()[:8]


def _fingerprint_from_chunk_cache_components(components: dict) -> str:
    """Legacy-compatible single-line fingerprint (same format as pre-R3)."""
    ns = float(components["neg_sample_frac"])
    return (
        f"{components['window_start']}|{components['window_end']}|{components['data_hash']}"
        f"|{components['cfg_hash']}|{components['profile_hash']}"
        f"|spec{components['feature_spec_hash']}|ns{ns:.4f}"
    )


def _parse_chunk_cache_fingerprint_pipe(fingerprint: str) -> Optional[dict]:
    """Parse a legacy pipe fingerprint into component dict for miss_reason diffing."""
    parts = fingerprint.strip().split("|")
    if len(parts) != 7:
        return None
    ws, we, dh, ch, ph, spec_part, ns_part = parts
    if not spec_part.startswith("spec") or not ns_part.startswith("ns"):
        return None
    try:
        ns = float(ns_part[2:])
    except ValueError:
        return None
    return {
        "window_start": ws,
        "window_end": we,
        "data_hash": dh,
        "cfg_hash": ch,
        "profile_hash": ph,
        "feature_spec_hash": spec_part[4:],
        "neg_sample_frac": ns,
    }


def _read_chunk_cache_sidecar(raw: str) -> Tuple[str, Optional[dict]]:
    """Return (fingerprint, optional pipeline components) from sidecar file body."""
    text = raw.strip()
    if not text:
        return "", None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return text, None
        fp = obj.get("fingerprint") or obj.get("fp")
        if not isinstance(fp, str) or not fp:
            return text, None
        pipe = obj.get("pipeline")
        if isinstance(pipe, dict):
            return fp, pipe
        return fp, _parse_chunk_cache_fingerprint_pipe(fp)
    return text, _parse_chunk_cache_fingerprint_pipe(text)


def _write_chunk_cache_sidecar(
    fingerprint: str,
    components: dict,
    *,
    source_mode: str,
) -> str:
    """Serialize R3 JSON sidecar; `fingerprint` must match components."""
    payload = {
        "v": _CHUNK_CACHE_SIDECAR_VERSION,
        "fingerprint": fingerprint,
        "pipeline": {k: components[k] for k in (
            "window_start", "window_end", "data_hash", "cfg_hash",
            "profile_hash", "feature_spec_hash", "neg_sample_frac",
        )},
        "source": {"mode": source_mode},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _chunk_cache_miss_reasons(
    stored_fingerprint: str,
    stored_components: Optional[dict],
    current_components: dict,
) -> List[str]:
    """Return coarse miss_reason tags: data/config/profile/spec/neg_sample/window."""
    current_fp = _fingerprint_from_chunk_cache_components(current_components)
    if stored_fingerprint == current_fp:
        return []
    prev = stored_components
    if prev is None:
        prev = _parse_chunk_cache_fingerprint_pipe(stored_fingerprint)
    if prev is None:
        return ["unparsed_stored_key"]
    reasons: List[str] = []
    if prev.get("window_start") != current_components.get("window_start") \
            or prev.get("window_end") != current_components.get("window_end"):
        reasons.append("window")
    if prev.get("data_hash") != current_components.get("data_hash"):
        reasons.append("data")
    if prev.get("cfg_hash") != current_components.get("cfg_hash"):
        reasons.append("config")
    if prev.get("profile_hash") != current_components.get("profile_hash"):
        reasons.append("profile")
    if prev.get("feature_spec_hash") != current_components.get("feature_spec_hash"):
        reasons.append("spec")
    if "neg_sample_frac" in prev and "neg_sample_frac" in current_components:
        if float(prev["neg_sample_frac"]) != float(current_components["neg_sample_frac"]):
            reasons.append("neg_sample")
    return reasons or ["fingerprint_mismatch"]


def _chunk_cache_key(
    chunk: dict,
    bets: pd.DataFrame,
    profile_hash: str = "none",
    feature_spec_hash: str = "none",
    neg_sample_frac: float = 1.0,
) -> str:
    """Hash to detect stale parquet cache (TRN-07).

    Includes a config-constants hash (R71) so that changes to
    WALKAWAY_GAP_MIN, SESSION_AVAIL_DELAY_MIN, or HISTORY_BUFFER_DAYS
    automatically invalidate all cached chunk Parquets.

    R77: profile_hash encodes the shape/content of player_profile so that
    changes to the snapshot table also invalidate the chunk cache.

    R-NEG-1: neg_sample_frac is included so that changing the downsampling ratio
    forces a cache miss and prevents stale full/partial chunks being served.
    """
    components = _chunk_cache_components(
        chunk, bets,
        profile_hash=profile_hash,
        feature_spec_hash=feature_spec_hash,
        neg_sample_frac=neg_sample_frac,
    )
    return _fingerprint_from_chunk_cache_components(components)


def _commutative_frame_row_digest(df: pd.DataFrame) -> str:
    """Short order-insensitive fingerprint for DataFrame rows (Task 7 R1 / R4)."""
    row_hash = pd.util.hash_pandas_object(df, index=False).to_numpy(dtype=np.uint64, copy=False)
    count = np.uint64(row_hash.size)
    sum64 = np.uint64(row_hash.sum(dtype=np.uint64))
    xor64 = np.uint64(np.bitwise_xor.reduce(row_hash, dtype=np.uint64)) if row_hash.size else np.uint64(0)
    sq_sum64 = np.uint64((row_hash * row_hash).sum(dtype=np.uint64)) if row_hash.size else np.uint64(0)
    digest = hashlib.md5(
        f"{int(count)}|{int(sum64)}|{int(xor64)}|{int(sq_sum64)}".encode()
    ).hexdigest()
    return digest[:8]


def _order_insensitive_bets_hash(bets: pd.DataFrame) -> str:
    """Return a short order-insensitive fingerprint for chunk raw bets."""
    return _commutative_frame_row_digest(bets)


def _profile_hash_chunk_scoped(
    profile_df: Optional[pd.DataFrame],
    window_end: datetime,
) -> str:
    """Task 7 R4: profile cache component scoped to rows usable for this chunk's PIT join.

    ``join_player_profile`` picks the latest snapshot with ``snapshot_dtm <= payout_complete_dtm``.
    Training rows in the chunk have ``payout_complete_dtm < window_end`` (DEC-018 naive bounds),
    so snapshots with ``snapshot_dtm > window_end`` cannot affect this chunk — excluding them
    avoids invalidating older chunk caches when new month-end snapshots are appended for later chunks.

    Falls back to the legacy run-level fingerprint when ``snapshot_dtm`` is missing.
    """
    if profile_df is None or profile_df.empty:
        return "none"
    _profile_cols_key = "|".join(sorted(profile_df.columns.tolist()))
    if "snapshot_dtm" not in profile_df.columns:
        return hashlib.md5(
            f"{len(profile_df)}:{_profile_cols_key}".encode()
        ).hexdigest()[:6]
    we = window_end.replace(tzinfo=None) if getattr(window_end, "tzinfo", None) else window_end
    snap = pd.to_datetime(profile_df["snapshot_dtm"])
    if snap.dt.tz is not None:
        snap = snap.dt.tz_convert(HK_TZ_STR).dt.tz_localize(None)
    mask = snap <= we
    sub = profile_df.loc[mask]
    if sub.empty:
        return hashlib.md5(
            f"p0|{_profile_cols_key}|{we.isoformat()}".encode()
        ).hexdigest()[:6]
    body = _commutative_frame_row_digest(sub.reset_index(drop=True))
    return hashlib.md5(
        f"{len(sub)}|{_profile_cols_key}|{body}".encode()
    ).hexdigest()[:6]


def process_chunk(
    chunk: dict,
    canonical_map: pd.DataFrame,
    dummy_player_ids: Optional[set] = None,
    use_local_parquet: bool = False,
    force_recompute: bool = False,
    profile_df: Optional[pd.DataFrame] = None,
    feature_spec: Optional[dict] = None,
    feature_spec_hash: str = "none",
    neg_sample_frac: float = NEG_SAMPLE_FRAC,
    chunk_cache_stats: Optional[Dict[str, int]] = None,
) -> Optional[Path]:
    """Process one monthly chunk; return path to written Parquet or None if empty.

    The canonical_map is built once at the global level (cutoff = training end)
    and passed in here.  Phase 2 should use per-chunk PIT mapping.
    dummy_player_ids: FND-12 dummy/fake-account player_ids to drop from training (TRN-04).
    profile_df: player_profile snapshot table for PIT join (PLAN Step 4/DEC-011).
        Pass None to skip; profile feature columns will be 0 for all rows.
    feature_spec: parsed Track LLM feature spec loaded by run_pipeline.
    feature_spec_hash: short hash of the feature spec used to compute Track LLM
        columns; included in the chunk cache key so spec changes bust cache.
    neg_sample_frac: fraction of label=0 rows to keep (1.0 = keep all).  Overrides
        the module-level NEG_SAMPLE_FRAC; normally supplied by run_pipeline after the
        OOM pre-check (_oom_check_and_adjust_neg_sample_frac).

    Task 7 R6: pre-LLM Parquet cache (``*.prefeatures.parquet``) is **on by default**
    (``trainer.core.config.CHUNK_TWO_STAGE_CACHE_DEFAULT``) so Track Human can be skipped
    when only spec/neg_sample (downstream) changes. Disable with env
    ``CHUNK_TWO_STAGE_CACHE=0`` / ``false`` / ``no`` / ``off`` if RAM or disk is tight
    (see ``doc/training_oom_and_runtime_audit.md``).

    chunk_cache_stats:
        Optional mutable dict; keys ``step6_chunk_cache_*`` are incremented for
        :func:`_write_pipeline_diagnostics_json` (Task 7 DoD).
    """
    window_start = chunk["window_start"]
    window_end = chunk["window_end"]
    extended_end = chunk["extended_end"]

    # DEC-018: pipeline interior is uniformly tz-naive HK local time.
    # time_fold produces tz-aware bounds; strip here so all downstream callers
    # (apply_dq, compute_labels, add_track_human_features, label filter) receive
    # tz-naive datetimes matching the tz-naive data columns from apply_dq R23.
    window_start = window_start.replace(tzinfo=None) if window_start.tzinfo else window_start
    window_end   = window_end.replace(tzinfo=None)   if window_end.tzinfo   else window_end
    extended_end = extended_end.replace(tzinfo=None)  if extended_end.tzinfo  else extended_end
    # Guard: all three boundaries must be tz-naive inside process_chunk.
    for _bname, _bval in (("window_start", window_start), ("window_end", window_end), ("extended_end", extended_end)):
        assert getattr(_bval, "tzinfo", None) is None, \
            f"DEC-018: {_bname} must be tz-naive inside process_chunk (got {_bval!r})"

    chunk_path = _chunk_parquet_path(chunk)
    key_path = chunk_path.with_suffix(".cache_key")
    _source_mode = "local_parquet" if use_local_parquet else "clickhouse"

    # R77 / Task 7 R4: profile snapshot fingerprint (chunk-scoped).
    _profile_hash = _profile_hash_chunk_scoped(profile_df, window_end)

    # --- Load data (local: metadata cache key first so cache hits skip Parquet IO) ---
    if use_local_parquet:
        # Task 7 R5: file-level fingerprint + filter bounds aligned with load_local_parquet.
        _dh_local = _local_parquet_source_data_hash(window_start, extended_end)
        _cache_components = _chunk_cache_components(
            chunk,
            None,
            profile_hash=_profile_hash,
            feature_spec_hash=feature_spec_hash,
            neg_sample_frac=neg_sample_frac,
            data_hash=_dh_local,
        )
        current_key = _fingerprint_from_chunk_cache_components(_cache_components)
        if not force_recompute and chunk_path.exists():
            stored_raw = key_path.read_text(encoding="utf-8") if key_path.exists() else ""
            stored_key, stored_comp = _read_chunk_cache_sidecar(stored_raw)
            if stored_key == current_key:
                logger.info(
                    "Chunk %s–%s: cache hit (key=%s, local metadata)",
                    window_start.date(), window_end.date(), current_key,
                )
                _bump_chunk_cache_stat(chunk_cache_stats, "step6_chunk_cache_final_hit_total")
                _bump_chunk_cache_stat(
                    chunk_cache_stats, "step6_chunk_cache_final_hit_local_metadata_total",
                )
                return chunk_path
            else:
                miss_reasons = _chunk_cache_miss_reasons(stored_key, stored_comp, _cache_components)
                logger.info(
                    "Chunk %s–%s: cache stale (key mismatch, miss_reason=%s), recomputing",
                    window_start.date(), window_end.date(), miss_reasons,
                )
        bets_raw, sessions_raw = load_local_parquet(window_start, extended_end)
    else:
        bets_raw, sessions_raw = load_clickhouse_data(window_start, extended_end)

    if bets_raw.empty:
        logger.warning("Chunk %s–%s: no bets, skipping", window_start.date(), window_end.date())
        return None

    # --- TRN-07: ClickHouse path — cache key from raw bets content hash ---
    if not use_local_parquet:
        _cache_components = _chunk_cache_components(
            chunk,
            bets_raw,
            profile_hash=_profile_hash,
            feature_spec_hash=feature_spec_hash,
            neg_sample_frac=neg_sample_frac,
        )
        current_key = _fingerprint_from_chunk_cache_components(_cache_components)
        if not force_recompute and chunk_path.exists():
            stored_raw = key_path.read_text(encoding="utf-8") if key_path.exists() else ""
            stored_key, stored_comp = _read_chunk_cache_sidecar(stored_raw)
            if stored_key == current_key:
                logger.info(
                    "Chunk %s–%s: cache hit (key=%s)",
                    window_start.date(), window_end.date(), current_key,
                )
                _bump_chunk_cache_stat(chunk_cache_stats, "step6_chunk_cache_final_hit_total")
                _bump_chunk_cache_stat(
                    chunk_cache_stats, "step6_chunk_cache_final_hit_after_load_total",
                )
                return chunk_path
            else:
                miss_reasons = _chunk_cache_miss_reasons(stored_key, stored_comp, _cache_components)
                logger.info(
                    "Chunk %s–%s: cache stale (key mismatch, miss_reason=%s), recomputing",
                    window_start.date(), window_end.date(), miss_reasons,
                )

    # --- Post-Load Normalizer (PLAN § Post-Load Normalizer Phase 2) ---
    bets_norm, sessions_norm = normalize_bets_sessions(bets_raw, sessions_raw)

    # --- DQ --- (bets_history_start pulls HISTORY_BUFFER_DAYS of extra context for Track Human)
    history_start = window_start - timedelta(days=HISTORY_BUFFER_DAYS)
    bets, sessions = apply_dq(
        bets_norm, sessions_norm, window_start, extended_end,
        bets_history_start=history_start,
    )
    if bets.empty:
        logger.warning("Chunk %s–%s: empty after DQ", window_start.date(), window_end.date())
        return None

    # --- TRN-04: drop FND-12 dummy/fake-account rows before feature engineering ---
    if dummy_player_ids and "player_id" in bets.columns:
        before = len(bets)
        bets = bets[~bets["player_id"].isin(dummy_player_ids)].reset_index(drop=True)
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

    # H3 routing contract: every canonical_id in mapping is rated; unrated rows should
    # not enter heavy FE path (Track Human / Track LLM / labels / profile join).
    rated_ids: set = (
        set(canonical_map["canonical_id"].astype(str).unique())
        if not canonical_map.empty and "canonical_id" in canonical_map.columns
        else set()
    )
    bets["canonical_id"] = bets["canonical_id"].astype(str)
    if rated_ids:
        _n_before_rated_prune = len(bets)
        bets = bets[bets["canonical_id"].isin(rated_ids)].copy()
        _n_pruned = _n_before_rated_prune - len(bets)
        if _n_pruned > 0:
            logger.info(
                "Chunk %s–%s: early-pruned %d unrated rows before heavy FE (rated_rows=%d)",
                window_start.date(),
                window_end.date(),
                _n_pruned,
                len(bets),
            )
    else:
        logger.warning(
            "Chunk %s–%s: canonical_map empty -> no rated rows; skip heavy FE",
            window_start.date(),
            window_end.date(),
        )
        return None
    if bets.empty:
        logger.warning(
            "Chunk %s–%s: empty after rated-only early prune",
            window_start.date(),
            window_end.date(),
        )
        return None

    # --- Track Human features (on rated-only bets incl. history, cutoff=window_end) ---
    # Computing before label filtering ensures cross-chunk state (loss_streak,
    # run_boundary) uses historical context from HISTORY_BUFFER_DAYS before window_start.
    # Always use SCORER_LOOKBACK_HOURS for train–serve parity (same window as scorer, default 8h).
    # Task 7 R6: optional pre-LLM Parquet cache (skip Track Human when key matches).
    _lookback_hours = getattr(_cfg, "SCORER_LOOKBACK_HOURS", 8)
    _two_stage = _chunk_two_stage_cache_enabled()
    _pref_path = _chunk_prefeatures_parquet_path(chunk)
    _pref_key_path = _chunk_prefeatures_sidecar_path(chunk)
    _pref_comps = _prefeatures_cache_components(_cache_components)
    _pref_key = _fingerprint_from_chunk_cache_components(_pref_comps)
    _skip_track_human = False
    if _two_stage and not force_recompute and _pref_path.exists():
        stored_pref = _pref_key_path.read_text(encoding="utf-8") if _pref_key_path.exists() else ""
        sk, sc = _read_chunk_cache_sidecar(stored_pref)
        if sk == _pref_key:
            logger.info(
                "Chunk %s–%s: prefeatures cache hit (key=%s), skipping Track Human",
                window_start.date(), window_end.date(), _pref_key,
            )
            bets = pd.read_parquet(_pref_path)
            _skip_track_human = True
            _bump_chunk_cache_stat(chunk_cache_stats, "step6_chunk_cache_prefeatures_hit_total")
        else:
            miss_reasons = _chunk_cache_miss_reasons(sk, sc, _pref_comps)
            logger.info(
                "Chunk %s–%s: prefeatures cache stale (miss_reason=%s), recomputing Track Human",
                window_start.date(), window_end.date(), miss_reasons,
            )

    if not _skip_track_human:
        bets = add_track_human_features(bets, canonical_map, window_end, lookback_hours=_lookback_hours)
        if _two_stage:
            _bump_chunk_cache_stat(
                chunk_cache_stats, "step6_chunk_cache_prefeatures_track_human_recompute_total",
            )
            bets.to_parquet(_pref_path, index=False)
            _pref_key_path.write_text(
                _write_chunk_cache_sidecar(_pref_key, _pref_comps, source_mode=_source_mode),
                encoding="utf-8",
            )

    # --- Track LLM: DuckDB + Feature Spec YAML (DEC-022/023/024) ---
    # R3500: compute on the FULL bets DataFrame (with HISTORY_BUFFER_DAYS context)
    # BEFORE label filtering so window features see the same history as the scorer
    # (train-serve parity).  The result is merged back onto bets by bet_id so that
    # compute_labels still receives the extended-zone rows it needs for right-censoring.
    # DEC-031 / T-DEC031: Track LLM errors propagate — no silent skip of LLM features.
    _bets_llm_feature_cols: list = []
    if feature_spec is not None:
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

    # --- Labels (C1 extended pull) ---
    labeled = compute_labels(
        bets_df=bets,
        window_end=window_end,
        extended_end=extended_end,
    )
    # H1: drop censored terminal bets + filter to training window in a single pass.
    # Combining the two filters into one mask avoids an intermediate ~32M-row .copy()
    # that was the direct OOM trigger (17 object cols × 32M rows ≈ 4 GiB allocation).
    # Both sides are tz-naive after DEC-018 strip at process_chunk() entry.
    _keep_mask = (
        ~labeled["censored"]
        & (labeled["payout_complete_dtm"] >= window_start)
        & (labeled["payout_complete_dtm"] < window_end)
    )
    labeled = labeled.loc[_keep_mask].reset_index(drop=True)
    if labeled.empty:
        logger.warning("Chunk %s–%s: empty after label filtering", window_start.date(), window_end.date())
        return None

    # --- player_profile PIT join (PLAN Step 4 / DEC-011) ---
    # Attaches Rated-player profile features via as-of merge (snapshot_dtm <= bet_time).
    # Non-rated bets and bets without a prior snapshot receive 0 for all profile columns.
    labeled = join_player_profile(labeled, profile_df)

    # Ensure all non-profile feature columns exist with numeric defaults.
    # R74: profile columns are intentionally left as NaN when a player has no
    # prior snapshot — LightGBM routes them to the trained default-child.
    # Blanket fillna(0) across all candidate cols would erase that signal.
    # R127-2: derive profile set from feature_spec/YAML (SSOT). When feature_spec is None
    # (e.g. YAML path missing), fallback to PROFILE_FEATURE_COLS from features.py; long-term
    # PLAN Step 3 may move this to load default YAML or reject run (Round 141 Review P1).
    _all_candidate_cols = get_all_candidate_feature_ids(feature_spec, screening_only=True) if feature_spec else list(PROFILE_FEATURE_COLS)
    _yaml_profile_set = (
        set(get_candidate_feature_ids(feature_spec, "track_profile", screening_only=False))
        if feature_spec
        else set(PROFILE_FEATURE_COLS)
    )
    _non_profile_cols = [c for c in _all_candidate_cols if c not in _yaml_profile_set]
    for col in _non_profile_cols:
        if col not in labeled.columns:
            labeled[col] = 0
    labeled[_non_profile_cols] = labeled[_non_profile_cols].fillna(0)

    # Mark rated/non-rated for downstream schema parity.
    labeled["is_rated"] = labeled["canonical_id"].isin(rated_ids)

    _n_total_before_sample = len(labeled)
    _n_pos_before_sample = int(labeled["label"].sum())
    _n_rated_before_sample = int(labeled["is_rated"].sum())

    # --- Per-chunk negative downsampling (OOM mitigation, config.NEG_SAMPLE_FRAC) ---
    # Keep ALL positives; downsample negatives to neg_sample_frac fraction.
    # class_weight='balanced' + per-run sample_weight compensate automatically.
    if neg_sample_frac < 1.0 and "label" in labeled.columns:
        _pos_mask = labeled["label"] == 1
        _n_neg_before = int((~_pos_mask).sum())
        # R-NEG-6: chunk-specific seed avoids systematic same-index bias across chunks.
        # R-371-5: use hashlib instead of built-in hash() which is randomised per
        # process by PYTHONHASHSEED (Python 3.3+), making seeds non-reproducible.
        _chunk_seed = int(
            hashlib.md5(
                f"{window_start.isoformat()}|{window_end.isoformat()}".encode()
            ).hexdigest()[:8],
            16,
        ) % (2**31)
        _neg_keep = labeled[~_pos_mask].sample(frac=neg_sample_frac, random_state=_chunk_seed)
        labeled = pd.concat([labeled[_pos_mask], _neg_keep], ignore_index=True)
        logger.info(
            "Chunk %s–%s: neg downsample frac=%.2f  rows %d->%d  "
            "(pos kept: %d, neg: %d->%d)",
            window_start.date(), window_end.date(),
            neg_sample_frac, _n_total_before_sample, len(labeled),
            _n_pos_before_sample, _n_neg_before, len(_neg_keep),
        )
        print(
            "  [neg-sample] chunk %s–%s: %d -> %d rows (neg %.0f%%, pos all kept)"
            % (window_start.date(), window_end.date(),
               _n_total_before_sample, len(labeled), neg_sample_frac * 100),
            flush=True,
        )
        # R-NEG-7: guard against extreme frac values that remove all negatives.
        if int((labeled["label"] == 0).sum()) == 0:
            logger.error(
                "Chunk %s–%s: neg_sample_frac=%.4f removed ALL negatives — "
                "model training will fail. Increase NEG_SAMPLE_FRAC or NEG_SAMPLE_FRAC_MIN.",
                window_start.date(), window_end.date(), neg_sample_frac,
            )

    logger.info(
        "Chunk %s–%s: %d rows (label=1: %d, rated: %d)",
        window_start.date(), window_end.date(),
        len(labeled),
        int(labeled["label"].sum()),
        _n_rated_before_sample,
    )

    labeled.to_parquet(chunk_path, index=False)
    # Persist structured cache sidecar (Task 7 R3); fingerprint matches legacy pipe format.
    key_path.write_text(
        _write_chunk_cache_sidecar(current_key, _cache_components, source_mode=_source_mode),
        encoding="utf-8",
    )
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
# Plan B: export train/valid to CSV for LightGBM from-file training (PLAN §3)
# ---------------------------------------------------------------------------

def _export_train_valid_to_csv(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    feature_cols: List[str],
    export_dir: Path,
    *,
    ranking_recipe: Optional[str] = None,
) -> Tuple[Path, Path]:
    """Export rated rows to CSV for LightGBM lgb.Dataset(path) (PLAN 方案 B 匯出).

    Train: screened_cols + label + weight (weight = 1/N_run per canonical_id, run_id).
    Valid: screened_cols + label (no weight).
    Only rows with is_rated == True are exported.
    Returns (train_csv_path, valid_csv_path).
    """
    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    if "label" not in train_df.columns or "label" not in valid_df.columns:
        raise ValueError("train_df and valid_df must contain 'label' for export")
    # Round 186 Review P3: dedupe feature_cols so CSV header has no duplicate column names.
    feature_cols_unique = list(dict.fromkeys(feature_cols))
    # Round 186 Review P1: use only columns present in BOTH train and valid (Step 9 alignment).
    common_cols = [
        c for c in feature_cols_unique
        if c in train_df.columns and c in valid_df.columns
    ]
    # R199 Review #2: refuse no-common-columns in normal runs.
    # For tiny/synthetic debug splits (empty train/valid), keep pipeline alive with label-only export.
    if len(common_cols) == 0:
        if train_df.empty or valid_df.empty:
            logger.warning(
                "Plan B export: no common feature columns with empty split (train=%d, valid=%d); "
                "exporting label-only CSVs for debug/test flow.",
                len(train_df),
                len(valid_df),
            )
        else:
            raise ValueError(
                "Plan B export: no common feature columns between train_df and valid_df; cannot export valid CSV for LightGBM."
            )
    only_in_train = [c for c in feature_cols_unique if c in train_df.columns and c not in valid_df.columns]
    only_in_valid = [c for c in feature_cols_unique if c in valid_df.columns and c not in train_df.columns]
    if only_in_train or only_in_valid:
        logger.warning(
            "Plan B export: using common features only (skipped: only in train=%s, only in valid=%s)",
            only_in_train or None,
            only_in_valid or None,
        )
    cols_train_plus_label = common_cols + ["label"]
    # Rated only (PLAN: 僅匯出 is_rated == true 的列)
    train_rated = (
        train_df[train_df["is_rated"]]
        if "is_rated" in train_df.columns
        else train_df
    )
    valid_rated = (
        valid_df[valid_df["is_rated"]]
        if "is_rated" in valid_df.columns
        else valid_df
    )
    # Weight for train (same semantics as compute_sample_weights)
    weight_series = compute_sample_weights(train_rated)
    _recipe_csv = resolve_ranking_recipe(ranking_recipe)
    weight_series, _ = build_final_ranking_weights_in_memory(
        train_rated,
        weight_series,
        _recipe_csv,
        common_cols,
        lgb_classifier_params=None,
    )
    train_export = train_rated[cols_train_plus_label].copy()
    train_export.insert(len(cols_train_plus_label), "weight", weight_series.values)
    train_path = export_dir / "train_for_lgb.csv"
    train_export.to_csv(train_path, index=False)
    logger.info(
        "Exported train for Plan B: %s (%d rows, %d features + label + weight)",
        train_path,
        len(train_export),
        len(common_cols),
    )
    valid_cols = common_cols + ["label"]
    valid_export = valid_rated[valid_cols]
    valid_path = export_dir / "valid_for_lgb.csv"
    valid_export.to_csv(valid_path, index=False)
    logger.info(
        "Exported valid for Plan B: %s (%d rows, %d features + label)",
        valid_path,
        len(valid_export),
        len(valid_cols) - 1,
    )
    return (train_path, valid_path)


# ---------------------------------------------------------------------------
# Plan B+: stream export Parquet → LibSVM + .weight (PLAN 方案 B+ 階段 3)
# ---------------------------------------------------------------------------


def _labels_from_libsvm(path: Path) -> np.ndarray:
    """Read labels (first column) from a LibSVM file without loading features (PLAN B+ 階段 6).

    One label per line; returns float array for compatibility with precision_recall_curve.
    """
    labels: List[float] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            first = line.split(None, 1)[0]
            try:
                labels.append(float(first))
            except ValueError:
                continue
    return np.asarray(labels, dtype=np.float64)


def _export_parquet_to_libsvm(
    train_path: Path,
    valid_path: Path,
    feature_cols: List[str],
    export_dir: Path,
    test_path: Optional[Path] = None,
) -> Tuple[Path, Path, Optional[Path]]:
    """Stream export from split Parquets to LibSVM + .weight (PLAN B+ §4.3, 階段 6 第 3 步).

    Train: rated rows only; weight = 1/N_run (same as compute_sample_weights).
    Valid: rated rows only; no weight file.
    Test (optional): rated rows only; no weight file. When test_path is provided, writes test_for_lgb.libsvm.
    Does not load full train/valid/test into memory.
    Returns (train_libsvm_path, valid_libsvm_path, test_libsvm_path or None).
    """
    import duckdb

    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    if not feature_cols:
        raise ValueError("feature_cols must be non-empty for LibSVM export")
    # Exclude "label" so SELECT label, {cols} never doubles the label column (would yield 51 cols for 50 names).
    export_cols = [c for c in feature_cols if c != "label"]
    if len(export_cols) < len(feature_cols):
        logger.warning(
            "LibSVM export: excluded %r from feature_cols (already selected as first column); using %d features.",
            "label",
            len(export_cols),
        )
    if not export_cols:
        raise ValueError("feature_cols must contain at least one column other than 'label' for LibSVM export")
    feature_cols = export_cols
    if not train_path.exists():
        raise FileNotFoundError(f"Train Parquet not found: {train_path}")
    if not valid_path.exists():
        raise FileNotFoundError(f"Valid Parquet not found: {valid_path}")

    def _esc_path(s: str) -> str:
        return s.replace("'", "''")

    def _esc_col(c: str) -> str:
        return '"' + c.replace('"', '""') + '"'

    train_libsvm = export_dir / "train_for_lgb.libsvm"
    train_weight = export_dir / "train_for_lgb.libsvm.weight"
    valid_libsvm = export_dir / "valid_for_lgb.libsvm"
    train_libsvm_tmp = export_dir / "train_for_lgb.libsvm.tmp"
    train_weight_tmp = export_dir / "train_for_lgb.libsvm.weight.tmp"
    valid_libsvm_tmp = export_dir / "valid_for_lgb.libsvm.tmp"
    test_libsvm: Optional[Path] = None

    con = duckdb.connect(":memory:")
    try:
        train_s = _esc_path(str(train_path))
        valid_s = _esc_path(str(valid_path))
        cols = ", ".join(_esc_col(c) for c in feature_cols)
        # Rated only; weight = 1/N_run per (canonical_id, run_id)
        train_sql = (
            f"SELECT label, {cols}, "
            "1.0 / COUNT(*) OVER (PARTITION BY canonical_id, run_id) AS _w "
            f"FROM read_parquet('{train_s}') WHERE COALESCE(is_rated, false) = true"
        )
        valid_sql = (
            f"SELECT label, {cols} "
            f"FROM read_parquet('{valid_s}') WHERE COALESCE(is_rated, false) = true"
        )
        batch_size = 50_000
        n_train = 0
        _train_row_len_logged = False
        with open(train_libsvm_tmp, "w", encoding="utf-8") as f_lib, open(
            train_weight_tmp, "w", encoding="utf-8"
        ) as f_w:
            result = con.execute(train_sql)
            while True:
                rows = result.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    if not _train_row_len_logged:
                        _exp = 1 + len(feature_cols) + 1  # label + features + _w
                        if len(row) != _exp:
                            logger.warning(
                                "LibSVM export (train): first row has %d columns (expected %d); "
                                "writing only first %d feature dims to avoid feature_name/num_feature mismatch.",
                                len(row),
                                _exp,
                                len(feature_cols),
                            )
                        _train_row_len_logged = True
                    raw_label = int(row[0])
                    label = 1 if raw_label else 0
                    if raw_label not in (0, 1):
                        logger.warning(
                            "LibSVM export: non-binary label %s at row, coercing to 0/1",
                            raw_label,
                        )
                    # Exactly len(feature_cols) feature values; use 0-based indices (0..nf-1) for LightGBM (see #1776, #6149).
                    nf = len(feature_cols)
                    vals = [row[1 + i] for i in range(nf)]
                    w = float(row[-1])
                    parts = [str(label)]
                    for i, v in enumerate(vals):
                        if v is None or (isinstance(v, float) and v == 0.0):
                            continue
                        try:
                            x = float(v)
                        except (TypeError, ValueError):
                            x = 0.0
                        if isinstance(x, float) and math.isnan(x):
                            x = 0.0
                        if x != 0.0:
                            parts.append(f"{i}:{x}")
                    f_lib.write(" ".join(parts) + "\n")
                    f_w.write(f"{w}\n")
                    n_train += 1
        if n_train == 0:
            logger.warning(
                "LibSVM export produced 0 train rows (no is_rated rows); cannot train from file.",
            )
        os.replace(train_libsvm_tmp, train_libsvm)
        os.replace(train_weight_tmp, train_weight)

        n_valid = 0
        _valid_row_len_logged = False
        with open(valid_libsvm_tmp, "w", encoding="utf-8") as f_lib:
            result = con.execute(valid_sql)
            while True:
                rows = result.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    if not _valid_row_len_logged:
                        _exp = 1 + len(feature_cols)
                        if len(row) != _exp:
                            logger.warning(
                                "LibSVM export (valid): first row has %d columns (expected %d); "
                                "writing only first %d feature dims.",
                                len(row),
                                _exp,
                                len(feature_cols),
                            )
                        _valid_row_len_logged = True
                    raw_label = int(row[0])
                    label = 1 if raw_label else 0
                    if raw_label not in (0, 1):
                        logger.warning(
                            "LibSVM export: non-binary label %s at row, coercing to 0/1",
                            raw_label,
                        )
                    nf = len(feature_cols)
                    vals = [row[1 + i] for i in range(nf)]
                    parts = [str(label)]
                    for i, v in enumerate(vals):
                        if v is None or (isinstance(v, float) and v == 0.0):
                            continue
                        try:
                            x = float(v)
                        except (TypeError, ValueError):
                            x = 0.0
                        if isinstance(x, float) and math.isnan(x):
                            x = 0.0
                        if x != 0.0:
                            parts.append(f"{i}:{x}")
                    f_lib.write(" ".join(parts) + "\n")
                    n_valid += 1
        os.replace(valid_libsvm_tmp, valid_libsvm)

        n_test = 0
        if test_path is not None and test_path.exists():
            test_libsvm = export_dir / "test_for_lgb.libsvm"
            test_libsvm_tmp = export_dir / "test_for_lgb.libsvm.tmp"
            test_s = _esc_path(str(test_path))
            test_sql = (
                f"SELECT label, {cols} "
                f"FROM read_parquet('{test_s}') WHERE COALESCE(is_rated, false) = true"
            )
            with open(test_libsvm_tmp, "w", encoding="utf-8") as f_lib:
                result = con.execute(test_sql)
                while True:
                    rows = result.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        raw_label = int(row[0])
                        label = 1 if raw_label else 0
                        if raw_label not in (0, 1):
                            logger.warning(
                                "LibSVM export (test): non-binary label %s, coercing to 0/1",
                                raw_label,
                            )
                        nf = len(feature_cols)
                        vals = [row[1 + i] for i in range(nf)]
                        parts = [str(label)]
                        for i, v in enumerate(vals):
                            if v is None or (isinstance(v, float) and v == 0.0):
                                continue
                            try:
                                x = float(v)
                            except (TypeError, ValueError):
                                x = 0.0
                            if isinstance(x, float) and math.isnan(x):
                                x = 0.0
                            if x != 0.0:
                                parts.append(f"{i}:{x}")
                        f_lib.write(" ".join(parts) + "\n")
                        n_test += 1
            os.replace(test_libsvm_tmp, test_libsvm)
    finally:
        con.close()

    _max_idx = -1
    _min_idx = 10**9
    _idx_51_count = 0
    _token_count = 0
    _line_count = 0
    try:
        with open(train_libsvm, encoding="utf-8") as _tf:
            for _line in _tf:
                _line = _line.strip()
                if not _line:
                    continue
                _line_count += 1
                _parts = _line.split()
                for _tok in _parts[1:]:
                    if ":" not in _tok:
                        continue
                    _k = _tok.split(":", 1)[0]
                    try:
                        _idx = int(_k)
                    except ValueError:
                        continue
                    _token_count += 1
                    if _idx > _max_idx:
                        _max_idx = _idx
                    if _idx < _min_idx:
                        _min_idx = _idx
                    if _idx == 51:
                        _idx_51_count += 1
    except Exception as _scan_e:
        # #region agent log
        _agent_debug_log(
            hypothesis_id="H1",
            location="trainer/training/trainer.py:_export_parquet_to_libsvm:post-export-scan",
            message="Failed to scan exported train LibSVM",
            data={"path": str(train_libsvm), "error": str(_scan_e)},
        )
        # #endregion
    else:
        # #region agent log
        _agent_debug_log(
            hypothesis_id="H1",
            location="trainer/training/trainer.py:_export_parquet_to_libsvm:post-export-scan",
            message="Exported train LibSVM index statistics",
            data={
                "path": str(train_libsvm),
                "feature_cols_len": len(feature_cols),
                "line_count": _line_count,
                "token_count": _token_count,
                "min_feature_index": (None if _token_count == 0 else _min_idx),
                "max_feature_index": (None if _token_count == 0 else _max_idx),
                "index_51_count": _idx_51_count,
            },
        )
        # #endregion

    if test_libsvm is not None:
        logger.info(
            "Exported LibSVM for Plan B+: train %s (%d rows + weight), valid %s (%d rows), test %s (%d rows)",
            train_libsvm, n_train, valid_libsvm, n_valid, test_libsvm, n_test,
        )
    else:
        logger.info(
            "Exported LibSVM for Plan B+: train %s (%d rows + weight), valid %s (%d rows)",
            train_libsvm, n_train, valid_libsvm, n_valid,
        )
    # Remove stale .bin so Step 9 always builds Dataset from current LibSVM (avoids feature_name(50) vs num_feature(51)).
    _bin_in_export = export_dir / (train_libsvm.stem + ".bin")
    if _bin_in_export.is_file():
        _bin_in_export.unlink(missing_ok=True)
        logger.info("LibSVM export: removed stale %s so training uses current feature set.", _bin_in_export.name)
    return (train_libsvm, valid_libsvm, test_libsvm)


# ---------------------------------------------------------------------------
# Plan B: Booster wrapper for scorer/artifact compatibility (PLAN §5)
# ---------------------------------------------------------------------------

class _BoosterWrapper:
    """Thin wrapper so lgb.Booster can be used where LGBMClassifier is expected (PLAN 方案 B §5).

    Scorer and _compute_test_metrics use model.predict_proba(X)[:, 1] and model.booster_.
    """

    def __init__(self, booster: lgb.Booster):
        self.booster_ = booster

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        p = self.booster_.predict(X)
        p = np.asarray(p).reshape(-1, 1)
        return np.hstack([1.0 - p, p])


# ---------------------------------------------------------------------------
# Optuna hyperparameter search (per model type)
# ---------------------------------------------------------------------------

def _lightgbm_gpu_probe_ok() -> bool:
    """Tiny fit to verify OpenCL GPU path works on this machine (Windows: device_type=gpu)."""
    try:
        X = np.random.RandomState(0).rand(80, 6).astype(np.float32)
        y = (X[:, 0] > 0.5).astype(np.int32)
        clf = lgb.LGBMClassifier(
            objective="binary",
            device_type="gpu",
            n_estimators=3,
            max_depth=3,
            num_leaves=8,
            verbose=-1,
            n_jobs=1,
        )
        clf.fit(X, y)
        return True
    except Exception as exc:
        logger.warning("LightGBM GPU probe failed: %s", exc)
        return False


def configure_lightgbm_device_for_run(args: Any) -> None:
    """Resolve CLI override, optional GPU probe, set effective device for this pipeline run."""
    global _EFFECTIVE_LIGHTGBM_DEVICE, _LIGHTGBM_GPU_FALLBACK_USED
    global _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS, _CLI_LIGHTGBM_DEVICE_OVERRIDE

    raw = getattr(args, "lgbm_device", None)
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        _CLI_LIGHTGBM_DEVICE_OVERRIDE = None
    else:
        s = str(raw).strip().lower()
        if s not in ("cpu", "gpu"):
            raise SystemExit("Invalid --lgbm-device %r; use cpu or gpu." % (raw,))
        _CLI_LIGHTGBM_DEVICE_OVERRIDE = s

    req = _CLI_LIGHTGBM_DEVICE_OVERRIDE if _CLI_LIGHTGBM_DEVICE_OVERRIDE is not None else LIGHTGBM_DEVICE_TYPE
    _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS = req
    _LIGHTGBM_GPU_FALLBACK_USED = False

    if req == "cpu":
        _EFFECTIVE_LIGHTGBM_DEVICE = "cpu"
        logger.info("LightGBM: effective device=cpu (requested)")
        return

    if _lightgbm_gpu_probe_ok():
        _EFFECTIVE_LIGHTGBM_DEVICE = "gpu"
        logger.info("LightGBM: effective device=gpu (requested, probe ok)")
        return

    _EFFECTIVE_LIGHTGBM_DEVICE = "cpu"
    _LIGHTGBM_GPU_FALLBACK_USED = True
    logger.warning(
        "LightGBM: GPU requested but probe failed; using cpu for this run "
        "(fix OpenCL/driver or pass --lgbm-device cpu / LIGHTGBM_DEVICE_TYPE=cpu)."
    )


def _lgb_params_for_pipeline() -> dict:
    """LightGBM params shared by Optuna, final fit, and lgb.train (device-aware)."""
    dev = _EFFECTIVE_LIGHTGBM_DEVICE
    out: dict[str, Any] = {
        "objective": "binary",
        "class_weight": "balanced",
        "verbose": -1,
        "random_state": 42,
        "device_type": dev,
    }
    if dev == "cpu":
        out["force_col_wise"] = True
        out["n_jobs"] = -1
    else:
        out["n_jobs"] = int(LIGHTGBM_GPU_N_JOBS)
    return out


def _parse_gpu_ids(raw: Optional[str]) -> list[str]:
    """Parse a comma-delimited GPU id list, ignoring empty tokens."""
    if raw is None:
        return []
    return [tok.strip() for tok in str(raw).split(",") if tok.strip()]


def discover_visible_gpu_ids() -> list[str]:
    """Best-effort CUDA-style GPU discovery for CatBoost/XGBoost scheduling."""
    configured = _parse_gpu_ids(TRAINER_GPU_IDS)
    if configured:
        return configured

    cuda_visible = os.getenv("CUDA_VISIBLE_DEVICES")
    if cuda_visible is not None and str(cuda_visible).strip():
        raw_ids = _parse_gpu_ids(cuda_visible)
        if raw_ids:
            # CUDA_VISIBLE_DEVICES remaps visible devices to local ordinals 0..N-1.
            return [str(i) for i in range(len(raw_ids))]

    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []

    discovered = []
    for line in proc.stdout.splitlines():
        tok = line.strip()
        if tok:
            discovered.append(tok)
    return discovered


def backend_runtime_params_for_backend(
    backend: str,
    *,
    device_mode: str,
    gpu_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return backend runtime params for CPU/GPU execution."""
    backend_n = str(backend or "").strip().lower()
    mode = str(device_mode or "cpu").strip().lower()
    if backend_n == "lightgbm":
        return {}
    if backend_n == "catboost":
        if mode == "gpu" and gpu_id is not None:
            return {
                "task_type": "GPU",
                "devices": str(gpu_id),
            }
        return {
            "task_type": "CPU",
        }
    if backend_n == "xgboost":
        if mode == "gpu" and gpu_id is not None:
            return {
                "device": f"cuda:{gpu_id}",
                "tree_method": "hist",
            }
        return {
            "device": "cpu",
            "tree_method": "hist",
        }
    return {}


def _backend_runtime_manifest(
    backend: str,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Return flat runtime metadata for metrics/report payloads."""
    params = dict(backend_runtime_params or {})
    backend_n = str(backend or "").strip().lower()
    mode = "cpu"
    gpu_id: Optional[str] = None
    if backend_n == "catboost":
        mode = "gpu" if str(params.get("task_type", "CPU")).strip().upper() == "GPU" else "cpu"
        devices = params.get("devices")
        if mode == "gpu" and devices is not None:
            gpu_id = str(devices)
    elif backend_n == "xgboost":
        device = str(params.get("device", "cpu")).strip().lower()
        if device.startswith("cuda"):
            mode = "gpu"
            if ":" in device:
                gpu_id = device.split(":", 1)[1].strip() or None
        else:
            mode = "cpu"
    elif backend_n == "lightgbm":
        mode = _EFFECTIVE_LIGHTGBM_DEVICE
    return {
        "backend_device_mode": mode,
        "backend_gpu_id": gpu_id,
    }


def resolve_gbm_backend_runtime_plan() -> dict[str, Any]:
    """Plan bakeoff backend device allocation and safe parallelism."""
    visible_gpu_ids = discover_visible_gpu_ids()
    requested_mode = GBM_BACKENDS_DEVICE_MODE
    effective_mode = requested_mode
    if requested_mode == "auto":
        effective_mode = "gpu" if visible_gpu_ids else "cpu"
    elif requested_mode == "gpu" and not visible_gpu_ids:
        logger.warning(
            "GBM backend GPU mode requested but no CUDA-visible GPUs were found; using cpu."
        )
        effective_mode = "cpu"

    bakeoff_backends = ("catboost", "xgboost")
    backend_runtime_by_name: dict[str, dict[str, Any]] = {}
    gpu_assignments: dict[str, str] = {}
    for idx, backend in enumerate(bakeoff_backends):
        gpu_id = None
        if effective_mode == "gpu" and visible_gpu_ids:
            gpu_id = visible_gpu_ids[idx % len(visible_gpu_ids)]
            gpu_assignments[backend] = str(gpu_id)
        backend_runtime_by_name[backend] = backend_runtime_params_for_backend(
            backend,
            device_mode=effective_mode,
            gpu_id=gpu_id,
        )

    max_workers = min(len(bakeoff_backends), len(visible_gpu_ids))
    if isinstance(GBM_BAKEOFF_MAX_PARALLEL_BACKENDS, int) and GBM_BAKEOFF_MAX_PARALLEL_BACKENDS > 0:
        max_workers = min(max_workers, int(GBM_BAKEOFF_MAX_PARALLEL_BACKENDS))
    parallel_backend_workers = max_workers if effective_mode == "gpu" and max_workers > 1 else 1
    parallel_backend_execution = parallel_backend_workers > 1
    return {
        "requested_backend_device_mode": requested_mode,
        "effective_backend_device_mode": effective_mode,
        "visible_gpu_ids": list(visible_gpu_ids),
        "gpu_assignments": dict(gpu_assignments),
        "backend_runtime_by_name": backend_runtime_by_name,
        "parallel_backend_workers": int(parallel_backend_workers),
        "parallel_backend_execution": bool(parallel_backend_execution),
    }


def _base_lgb_params() -> dict:
    """Backward-compat alias for :func:`_lgb_params_for_pipeline`."""
    return _lgb_params_for_pipeline()


def _val_window_hours_from_payout_df(df: Optional[pd.DataFrame]) -> Optional[float]:
    """Validation span in hours from ``payout_complete_dtm`` (for alerts/hour in HPO).

    Returns ``None`` when the column is missing, rows are insufficient, or span is non-positive.
    """
    if df is None or df.empty or "payout_complete_dtm" not in df.columns:
        return None
    ts = pd.to_datetime(df["payout_complete_dtm"], errors="coerce")
    if int(ts.notna().sum()) < 2:
        return None
    ts_naive = ts.dt.tz_localize(None) if getattr(ts.dt, "tz", None) is not None else ts
    mn = ts_naive.min()
    mx = ts_naive.max()
    if pd.isna(mn) or pd.isna(mx):
        return None
    span_sec = float((mx - mn).total_seconds())
    if not math.isfinite(span_sec) or span_sec <= 0.0:
        return None
    return span_sec / 3600.0


def _neg_pos_ratio_from_binary_labels(y: Any) -> Optional[float]:
    """Return neg/pos count ratio for binary 0/1 labels (``n_neg / n_pos``), same contract as ``test_neg_pos_ratio``."""
    y_a = np.asarray(y, dtype=float)
    n_pos = int(np.sum(y_a == 1.0))
    n_neg = int(np.sum(y_a == 0.0))
    if n_pos <= 0:
        return None
    r = float(n_neg) / float(n_pos)
    if not math.isfinite(r) or r <= 0.0:
        return None
    return r


def _rated_field_test_val_pick_per_hour_kwargs(
    *,
    label: str,
    field_test_constrained_optuna_objective_allowed: Optional[bool],
    val_df: pd.DataFrame,
) -> tuple[Optional[float], Optional[float]]:
    """Return ``(window_hours, min_alerts_per_hour)`` for validation DEC-026 pick matching field-test Optuna trials.

    When W1 allows the constrained path, *label* is ``rated``, and payout span is known,
    use the same floor as :func:`run_optuna_search` so refit winner metrics align with
    trial scores (W2 winner-pick parity).  Otherwise ``(None, None)`` — historical pick
    without per-hour density on validation.
    """
    if str(label or "").strip().lower() != "rated":
        return None, None
    if field_test_constrained_optuna_objective_allowed is not True:
        return None, None
    wh = _val_window_hours_from_payout_df(val_df)
    if wh is None:
        return None, None
    _mah = getattr(_cfg, "FIELD_TEST_HPO_MIN_ALERTS_PER_HOUR", 50.0)
    try:
        mf = float(_mah)
    except (TypeError, ValueError):
        mf = 50.0
    if not math.isfinite(mf) or mf <= 0.0:
        mf = 50.0
    return float(wh), mf


def _write_optuna_hpo_manifest(
    sink: Optional[list[dict[str, Any]]],
    payload: dict[str, Any],
) -> None:
    """Replace *sink* with a single-element list copy of *payload* (optional Optuna HPO provenance for metrics)."""
    if sink is None:
        return
    sink.clear()
    sink.append(dict(payload))


def _backend_hpo_defaults(backend: str) -> dict[str, Any]:
    backend_n = str(backend or "").strip().lower()
    if backend_n == "lightgbm":
        return {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "max_depth": 8,
            "min_child_samples": 20,
        }
    if backend_n == "catboost":
        return {
            "iterations": 400,
            "learning_rate": 0.05,
            "depth": 8,
            "l2_leaf_reg": 3.0,
            "random_strength": 1.0,
            "rsm": 1.0,
            "random_seed": 42,
            "verbose": False,
            "early_stopping_rounds": 50,
            "allow_writing_files": False,
            "loss_function": "Logloss",
            "thread_count": -1,
        }
    if backend_n == "xgboost":
        return {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "max_depth": 8,
            "reg_lambda": 1.0,
            "reg_alpha": 0.0,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 4.0,
            "objective": "binary:logistic",
            "tree_method": "hist",
            "random_state": 42,
            "n_jobs": -1,
            "verbosity": 0,
        }
    raise ValueError(f"Unsupported HPO backend: {backend}")


def _balanced_binary_class_ratio(y: pd.Series) -> Optional[float]:
    """Return neg/pos ratio for strict binary labels; None when unavailable."""
    if y is None or len(y) == 0:
        return None
    ya = np.asarray(y, dtype=float).reshape(-1)
    if ya.size == 0 or not np.isfinite(ya).all():
        return None
    pos = int(np.sum(ya == 1.0))
    neg = int(np.sum(ya == 0.0))
    if pos <= 0 or neg <= 0:
        return None
    return float(neg / pos)


def _apply_backend_imbalance_params(
    backend: str,
    params: Mapping[str, Any],
    y_train: pd.Series,
) -> dict[str, Any]:
    """Align imbalance handling across GBM backends for fair bakeoff."""
    backend_n = str(backend or "").strip().lower()
    out = dict(params)
    if backend_n == "lightgbm":
        return out

    ratio = _balanced_binary_class_ratio(y_train)
    if ratio is None:
        return out

    if backend_n == "catboost":
        out.setdefault("class_weights", [1.0, float(ratio)])
        return out
    if backend_n == "xgboost":
        out.setdefault("scale_pos_weight", float(ratio))
        return out
    return out


def resolve_backend_optuna_budget(
    backend: str,
    *,
    default_n_trials: Optional[int] = None,
    default_timeout_seconds: Optional[int] = None,
    default_early_stop_patience: Optional[int] = None,
    timeout_budget_divisor: Optional[int] = None,
) -> dict[str, Optional[int]]:
    _ = backend  # retained for call-site clarity / future per-backend overrides

    def _as_positive_int_or_none(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        return None

    n_trials = None
    timeout_seconds = None
    early_stop_patience = None
    if default_n_trials is None:
        default_n_trials = (
            _as_positive_int_or_none(getattr(_cfg, "OPTUNA_N_TRIALS", None))
            or _as_positive_int_or_none(OPTUNA_N_TRIALS)
            or 1
        )
    if default_timeout_seconds is None:
        default_timeout_seconds = (
            _as_positive_int_or_none(getattr(_cfg, "OPTUNA_TIMEOUT_SECONDS", None))
            or _as_positive_int_or_none(OPTUNA_TIMEOUT_SECONDS)
        )
    if default_early_stop_patience is None:
        default_early_stop_patience = (
            _as_positive_int_or_none(getattr(_cfg, "OPTUNA_EARLY_STOP_PATIENCE", None))
            or _as_positive_int_or_none(OPTUNA_EARLY_STOP_PATIENCE)
        )
    if n_trials is None:
        n_trials = default_n_trials if isinstance(default_n_trials, int) and default_n_trials > 0 else 1
    if timeout_seconds is None:
        timeout_seconds = (
            default_timeout_seconds
            if isinstance(default_timeout_seconds, int) and default_timeout_seconds > 0
            else None
        )
    if (
        timeout_seconds is not None
        and isinstance(timeout_budget_divisor, int)
        and timeout_budget_divisor > 1
    ):
        timeout_seconds = max(1, int(timeout_seconds // timeout_budget_divisor))
    if early_stop_patience is None:
        early_stop_patience = (
            default_early_stop_patience
            if isinstance(default_early_stop_patience, int) and default_early_stop_patience > 0
            else None
        )
    return {
        "n_trials": int(n_trials),
        "timeout_seconds": timeout_seconds,
        "early_stop_patience": early_stop_patience,
    }


def _suggest_backend_optuna_params(
    backend: str,
    trial: optuna.Trial,
) -> dict[str, Any]:
    backend_n = str(backend or "").strip().lower()
    if backend_n == "lightgbm":
        return {
            **_lgb_params_for_pipeline(),
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
    if backend_n == "catboost":
        return {
            "iterations": trial.suggest_int("iterations", 100, 800, step=50),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "depth": trial.suggest_int("depth", 4, 10),
            "l2_leaf_reg": trial.suggest_float("l2_leaf_reg", 1e-3, 20.0, log=True),
            "random_strength": trial.suggest_float("random_strength", 1e-3, 10.0, log=True),
            "rsm": trial.suggest_float("rsm", 0.5, 1.0),
            "random_seed": 42,
            "verbose": False,
            "early_stopping_rounds": 50,
            "allow_writing_files": False,
            "loss_function": "Logloss",
            "thread_count": -1,
        }
    if backend_n == "xgboost":
        return {
            "n_estimators": trial.suggest_int("n_estimators", 100, 800, step=50),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "max_depth": trial.suggest_int("max_depth", 3, 10),
            "reg_lambda": trial.suggest_float("reg_lambda", 1e-4, 10.0, log=True),
            "reg_alpha": trial.suggest_float("reg_alpha", 1e-4, 10.0, log=True),
            "subsample": trial.suggest_float("subsample", 0.5, 1.0),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
            "min_child_weight": trial.suggest_float("min_child_weight", 1.0, 20.0, log=True),
            "objective": "binary:logistic",
            "tree_method": "hist",
            "random_state": 42,
            "n_jobs": -1,
            "verbosity": 0,
        }
    raise ValueError(f"Unsupported HPO backend: {backend}")


def _fit_backend_hpo_scores(
    backend: str,
    *,
    params: dict[str, Any],
    X_tr: pd.DataFrame,
    y_tr: pd.Series,
    X_vl: pd.DataFrame,
    y_vl: pd.Series,
    sw_tr: pd.Series,
) -> np.ndarray:
    backend_n = str(backend or "").strip().lower()
    if backend_n == "lightgbm":
        model = lgb.LGBMClassifier(**params)
        if y_tr.nunique() < 2:
            model.fit(X_tr, y_tr, sample_weight=sw_tr)
        else:
            model.fit(
                X_tr,
                y_tr,
                sample_weight=sw_tr,
                eval_set=[(X_vl, y_vl)],
                callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
            )
        return np.asarray(model.predict_proba(X_vl)[:, 1], dtype=np.float64)

    X_tr_fit = X_tr.astype(np.float32, copy=False)
    X_vl_fit = X_vl.astype(np.float32, copy=False)
    if backend_n == "catboost":
        from catboost import CatBoostClassifier

        fit_params = _apply_backend_imbalance_params(backend_n, params, y_tr)
        model = CatBoostClassifier(**fit_params)
        if y_tr.nunique() < 2:
            model.fit(X_tr_fit, y_tr.astype(np.int32), sample_weight=sw_tr, verbose=False)
        else:
            model.fit(
                X_tr_fit,
                y_tr.astype(np.int32),
                sample_weight=sw_tr,
                eval_set=(X_vl_fit, y_vl.astype(np.int32)),
                early_stopping_rounds=int(fit_params.get("early_stopping_rounds", 50)),
                verbose=False,
            )
        return np.asarray(model.predict_proba(X_vl_fit)[:, 1], dtype=np.float64)

    if backend_n == "xgboost":
        import xgboost as xgb

        fit_params = _apply_backend_imbalance_params(backend_n, params, y_tr)
        model = xgb.XGBClassifier(**fit_params)
        if y_tr.nunique() < 2:
            model.fit(X_tr_fit, y_tr, sample_weight=sw_tr, verbose=False)
        else:
            model.fit(
                X_tr_fit,
                y_tr,
                sample_weight=sw_tr,
                eval_set=[(X_vl_fit, y_vl)],
                verbose=False,
            )
        return np.asarray(model.predict_proba(X_vl_fit)[:, 1], dtype=np.float64)

    raise ValueError(f"Unsupported HPO backend: {backend}")


def run_backend_optuna_search(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    *,
    backend: str = "lightgbm",
    n_trials: Optional[int] = None,
    label: str = "",
    field_test_constrained_optuna_objective_allowed: Optional[bool] = None,
    val_window_hours: Optional[float] = None,
    timeout_seconds: Optional[int] = None,
    early_stop_patience: Optional[int] = None,
    hpo_sample_rows: Optional[int] = OPTUNA_HPO_SAMPLE_ROWS,
    hpo_objective_manifest: Optional[list[dict[str, Any]]] = None,
    backend_runtime_params: Optional[Mapping[str, Any]] = None,
) -> dict:
    """TPE hyperparameter search on validation.

    Default: maximise average precision (AP).  When W1 allows a field-test path
    (``field_test_constrained_optuna_objective_allowed is True``), ``label`` is
    ``rated``, and ``val_window_hours`` is a finite positive span, the study
    instead maximises DEC-026 validation **precision** at the best threshold
    subject to ``FIELD_TEST_HPO_MIN_ALERTS_PER_HOUR`` (default 50) and the usual
    recall / min-alert-count guards — matching :func:`pick_threshold_dec026` semantics.
    When ``PRODUCTION_NEG_POS_RATIO`` is set and validation has positives with
    strictly positive neg/pos ratio, the trial score uses
    :func:`_precision_prod_adjusted` on that raw precision (Implementation Plan R1);
    otherwise the raw DEC-026 precision is maximised.

    When *hpo_objective_manifest* is a list, it is cleared and receives one dict of
    flat ``optuna_hpo_*`` keys for ``training_metrics.json`` (W2 provenance vs ``val_ap``).
    """
    backend_n = str(backend or "").strip().lower() or "lightgbm"
    runtime_manifest = _backend_runtime_manifest(
        backend_n,
        backend_runtime_params=backend_runtime_params,
    )
    budget = resolve_backend_optuna_budget(
        backend_n,
        default_n_trials=(n_trials if isinstance(n_trials, int) and n_trials > 0 else OPTUNA_N_TRIALS),
        default_timeout_seconds=timeout_seconds,
        default_early_stop_patience=early_stop_patience,
    )
    n_trials_eff = int(budget["n_trials"] or 1)
    timeout_eff = budget["timeout_seconds"]
    early_stop_patience_eff = budget["early_stop_patience"]

    # R705: guard against empty validation input — return empty dict (base params)
    # rather than crashing inside LightGBM or average_precision_score.
    if X_val.empty or len(y_val) == 0:
        logger.warning(
            "%s[%s]: empty validation set - skipping Optuna search, returning base params.",
            label or "model",
            backend_n,
        )
        _write_optuna_hpo_manifest(
            hpo_objective_manifest,
            {
                "optuna_hpo_backend": backend_n,
                "optuna_hpo_enabled": True,
                "optuna_hpo_backend_device_mode": runtime_manifest["backend_device_mode"],
                "optuna_hpo_backend_gpu_id": runtime_manifest["backend_gpu_id"],
                "optuna_hpo_n_trials_requested": n_trials_eff,
                "optuna_hpo_timeout_seconds": timeout_eff,
                "optuna_hpo_early_stop_patience": early_stop_patience_eff,
                "optuna_hpo_objective_mode": "skipped_empty_validation",
                "optuna_hpo_study_best_trial_value": None,
            },
        )
        return {}

    def _raise_field_test_gate_blocked(
        *,
        reason_code: str,
        details: str,
    ) -> None:
        _write_optuna_hpo_manifest(
            hpo_objective_manifest,
            {
                "optuna_hpo_backend": backend_n,
                "optuna_hpo_enabled": True,
                "optuna_hpo_backend_device_mode": runtime_manifest["backend_device_mode"],
                "optuna_hpo_backend_gpu_id": runtime_manifest["backend_gpu_id"],
                "optuna_hpo_n_trials_requested": n_trials_eff,
                "optuna_hpo_timeout_seconds": timeout_eff,
                "optuna_hpo_early_stop_patience": early_stop_patience_eff,
                "optuna_hpo_objective_mode": "gate_blocked",
                "optuna_hpo_study_best_trial_value": None,
                "optuna_hpo_gate_blocked": True,
                "optuna_hpo_gate_blocked_reason_code": reason_code,
                "optuna_hpo_gate_blocked_details": details,
            },
        )
        raise RuntimeError(f"{label or 'model'}[{backend_n}]: {details}")

    if field_test_constrained_optuna_objective_allowed is False and str(label or "").strip().lower() == "rated":
        _raise_field_test_gate_blocked(
            reason_code="infeasible_constraint",
            details=(
                "W1 precondition disallows field-test constrained objective "
                "(field_test_constrained_optuna_objective_allowed=False); "
                "DEC-043 contract requires GATE BLOCKED (no AP fallback)."
            ),
        )

    _mah_ft = getattr(_cfg, "FIELD_TEST_HPO_MIN_ALERTS_PER_HOUR", 50.0)
    try:
        _mah_ft_f = float(_mah_ft)
    except (TypeError, ValueError):
        _mah_ft_f = 50.0
    if not math.isfinite(_mah_ft_f) or _mah_ft_f <= 0.0:
        _mah_ft_f = 50.0

    _vwh: Optional[float] = None
    if val_window_hours is not None:
        try:
            _wf = float(val_window_hours)
        except (TypeError, ValueError):
            _wf = float("nan")
        else:
            if math.isfinite(_wf) and _wf > 0.0:
                _vwh = _wf

    _rated_l = str(label or "").strip().lower() == "rated"
    _use_ft_hpo = (
        field_test_constrained_optuna_objective_allowed is True
        and _rated_l
        and _vwh is not None
    )
    if field_test_constrained_optuna_objective_allowed is True and _rated_l and _vwh is None:
        _raise_field_test_gate_blocked(
            reason_code="infeasible_constraint",
            details=(
                "field-test constrained HPO allowed but val_window_hours missing/invalid; "
                "DEC-026 density guard requires positive payout_complete_dtm span "
                "and DEC-043 requires GATE BLOCKED (no AP fallback)."
            ),
        )

    # HPO subsampling (PLAN "Optuna HPO 階段 train/valid 抽樣"): use X_tr, y_tr, sw_tr, X_vl, y_vl in objective.
    X_tr = X_train
    y_tr = y_train
    sw_tr = sw_train
    X_vl = X_val
    y_vl = y_val
    _hpo_ratio: Optional[float] = None

    _sample_rows = (
        hpo_sample_rows
        if isinstance(hpo_sample_rows, int) and hpo_sample_rows > 0
        else None
    )
    if _sample_rows is not None and len(X_train) > _sample_rows:
        # Stratified sample train to _sample_rows; fallback to random if single class.
        idx = np.arange(len(X_train))
        try:
            idx_tr, _ = train_test_split(
                idx,
                train_size=_sample_rows,
                stratify=y_train,
                random_state=42,
            )
        except ValueError:
            # Single class in y_train; use random sample (PLAN §3).
            idx_tr = np.random.RandomState(42).choice(
                idx, size=min(_sample_rows, len(idx)), replace=False
            )
        X_tr = X_train.iloc[idx_tr]
        y_tr = y_train.iloc[idx_tr]
        sw_tr = sw_train.iloc[idx_tr]
        _hpo_ratio = _sample_rows / len(X_train)
        n_valid = min(len(X_val), max(1, int(len(X_val) * _hpo_ratio)))
        if len(X_val) > n_valid:
            idx_v = np.arange(len(X_val))
            try:
                idx_vl, _ = train_test_split(
                    idx_v,
                    train_size=n_valid,
                    stratify=y_val,
                    random_state=42,
                )
            except ValueError:
                idx_vl = np.random.RandomState(42).choice(
                    idx_v, size=min(n_valid, len(idx_v)), replace=False
                )
            X_vl = X_val.iloc[idx_vl]
            y_vl = y_val.iloc[idx_vl]
        logger.info(
            "Optuna HPO[%s]: subsampled train %d -> %d, valid %d -> %d (ratio=%.4f)",
            backend_n,
            len(X_train),
            len(X_tr),
            len(X_val),
            len(X_vl),
            _hpo_ratio,
        )

    _val_np_ratio: Optional[float] = _neg_pos_ratio_from_binary_labels(y_vl)
    _ft_hpo_uses_prod_adj = (
        _use_ft_hpo
        and _val_np_ratio is not None
        and PRODUCTION_NEG_POS_RATIO is not None
    )
    if _use_ft_hpo:
        if _ft_hpo_uses_prod_adj:
            logger.info(
                "%s[%s]: Optuna study maximises validation precision_prod_adjusted "
                "(DEC-026 pick; val neg/pos=%.4g vs PRODUCTION_NEG_POS_RATIO=%s; "
                "min_alerts_per_hour=%.4g; window_hours=%.4g).",
                label or "model",
                backend_n,
                float(_val_np_ratio),
                PRODUCTION_NEG_POS_RATIO,
                _mah_ft_f,
                float(_vwh),
            )
        else:
            logger.info(
                "%s[%s]: Optuna study maximises validation precision (DEC-026 raw; "
                "prod-adjust inactive — need positives + neg/pos ratio and "
                "PRODUCTION_NEG_POS_RATIO) with min_alerts_per_hour=%.4g over window_hours=%.4g.",
                label or "model",
                backend_n,
                _mah_ft_f,
                float(_vwh),
            )

    _metric_label = (
        "best_val_prec_dec026_prod_adj"
        if _ft_hpo_uses_prod_adj
        else ("best_val_prec_dec026" if _use_ft_hpo else "best_AP")
    )

    def objective(trial: optuna.Trial) -> float:
        params = _suggest_backend_optuna_params(backend_n, trial)
        if backend_runtime_params:
            params.update(dict(backend_runtime_params))
        scores = _fit_backend_hpo_scores(
            backend_n,
            params=params,
            X_tr=X_tr,
            y_tr=y_tr,
            X_vl=X_vl,
            y_vl=y_vl,
            sw_tr=sw_tr,
        )
        if _use_ft_hpo:
            _pick = pick_threshold_dec026(
                np.asarray(y_vl, dtype=float),
                np.asarray(scores, dtype=float),
                recall_floor=THRESHOLD_MIN_RECALL,
                min_alert_count=THRESHOLD_MIN_ALERT_COUNT,
                min_alerts_per_hour=_mah_ft_f,
                window_hours=float(_vwh),
                fbeta_beta=THRESHOLD_FBETA,
            )
            raw_p = float(_pick.precision)
            if _ft_hpo_uses_prod_adj:
                adj = _precision_prod_adjusted(
                    raw_p,
                    production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                    test_neg_pos_ratio=_val_np_ratio,
                )
                if adj is not None:
                    return float(adj)
            return raw_p
        return average_precision_score(y_vl, scores)

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    _timeout = (
        float(timeout_eff)
        if timeout_eff is not None and timeout_eff > 0
        else None
    )
    if _timeout is None:
        logger.info(
            "Optuna search (%s[%s]): n_trials=%d, timeout=disabled (OPTUNA_%s_TIMEOUT_SECONDS=%s)",
            label or "model",
            backend_n,
            n_trials_eff,
            backend_n.upper(),
            timeout_eff,
        )
    else:
        logger.info(
            "Optuna search (%s[%s]): n_trials=%d, timeout=%.0fs (~%.1f min)",
            label or "model",
            backend_n,
            n_trials_eff,
            _timeout,
            _timeout / 60.0,
        )

    _start = time.perf_counter()
    # PLAN § progress-bars-long-steps: Step 9 Optuna tqdm bar (ETA); respect DISABLE_PROGRESS_BAR.
    _disable_bar = getattr(_cfg, "DISABLE_PROGRESS_BAR", False)
    optuna_pbar = (
        _ProgressNoop()
        if _disable_bar
        else _tqdm_bar(total=n_trials_eff, desc=f"Step 9 Optuna {backend_n}", unit="trial")
    )

    def _progress_callback(study: optuna.Study, trial: FrozenTrial) -> None:
        optuna_pbar.update(1)
        n = len(study.trials)
        if n == 1 or n % 20 == 0 or n == n_trials_eff:
            elapsed = time.perf_counter() - _start
            try:
                best_ap = study.best_value
            except ValueError:
                # No trials completed yet (e.g. all failed so far); Optuna raises.
                best_ap = None
            best_str = "%.4f" % (best_ap if best_ap is not None else float("nan"))
            logger.info(
                "[Step 9] Optuna (%s[%s]) trial %d/%d  %s=%s  elapsed %.0fs (%.1f min)",
                label or "rated",
                backend_n,
                n,
                n_trials_eff,
                _metric_label,
                best_str,
                elapsed,
                elapsed / 60.0,
            )

    # Study-level early stop: stop when best AP has not improved for N consecutive trials
    # (PLAN "Optuna 整份 study 的 early stop"). Only active when OPTUNA_EARLY_STOP_PATIENCE is a positive int.
    _early_stop_state: dict = {"best": None, "no_improve_count": 0}

    def _early_stop_callback(study: optuna.Study, trial: FrozenTrial) -> None:
        try:
            current_best = study.best_value
        except ValueError:
            # No trials completed yet; skip state update (Review #2).
            return
        if current_best is None:
            return
        prev = _early_stop_state["best"]
        if prev is None or current_best > prev:
            _early_stop_state["best"] = current_best
            _early_stop_state["no_improve_count"] = 0
        else:
            _early_stop_state["no_improve_count"] += 1
        patience = (
            early_stop_patience_eff
            if isinstance(early_stop_patience_eff, int) and early_stop_patience_eff > 0
            else 0
        )
        if patience > 0 and _early_stop_state["no_improve_count"] >= patience:
            study.stop()
            n = len(study.trials)
            logger.info(
                "[Step 9] Optuna early stop (%s[%s]): no improvement for %d trials (stopped at trial %d/%d)",
                label or "rated",
                backend_n,
                patience,
                n,
                n_trials_eff,
            )

    callbacks: List[Callable[[optuna.Study, FrozenTrial], None]] = [_progress_callback]
    if isinstance(early_stop_patience_eff, int) and early_stop_patience_eff > 0:
        callbacks.append(_early_stop_callback)

    try:
        study.optimize(
            objective,
            n_trials=n_trials_eff,
            timeout=_timeout,
            show_progress_bar=False,
            callbacks=callbacks,
        )
    finally:
        optuna_pbar.close()
    best = study.best_params
    try:
        final_best_ap = study.best_value
    except ValueError:
        final_best_ap = None
    logger.info(
        "Optuna (%s[%s]) %s=%s, params=%s",
        label or "model",
        backend_n,
        _metric_label,
        "%.4f" % final_best_ap if final_best_ap is not None else "N/A",
        best,
    )
    _obj_mode = "validation_ap"
    if _use_ft_hpo:
        _obj_mode = (
            "field_test_dec026_val_precision_prod_adj"
            if _ft_hpo_uses_prod_adj
            else "field_test_dec026_val_precision_raw"
        )
    _manifest_pay: dict[str, Any] = {
        "optuna_hpo_backend": backend_n,
        "optuna_hpo_enabled": True,
        "optuna_hpo_backend_device_mode": runtime_manifest["backend_device_mode"],
        "optuna_hpo_backend_gpu_id": runtime_manifest["backend_gpu_id"],
        "optuna_hpo_n_trials_requested": n_trials_eff,
        "optuna_hpo_timeout_seconds": timeout_eff,
        "optuna_hpo_early_stop_patience": early_stop_patience_eff,
        "optuna_hpo_study_trials_completed": int(len(study.trials)),
        "optuna_hpo_study_stopped_early": bool(len(study.trials) < n_trials_eff),
        "optuna_hpo_objective_mode": _obj_mode,
        "optuna_hpo_study_best_trial_value": (
            float(final_best_ap) if final_best_ap is not None else None
        ),
    }
    if _sample_rows is not None:
        _manifest_pay["optuna_hpo_sample_rows_cap"] = int(_sample_rows)
    if _hpo_ratio is not None:
        _manifest_pay["optuna_hpo_sample_ratio"] = float(_hpo_ratio)
        _manifest_pay["optuna_hpo_sampled_train_rows"] = int(len(X_tr))
        _manifest_pay["optuna_hpo_sampled_valid_rows"] = int(len(X_vl))
    if _vwh is not None:
        _manifest_pay["optuna_hpo_val_window_hours_used"] = float(_vwh)
    if _use_ft_hpo:
        _manifest_pay["optuna_hpo_field_test_min_alerts_per_hour"] = float(_mah_ft_f)
        if _val_np_ratio is not None:
            _manifest_pay["optuna_hpo_val_neg_pos_ratio"] = float(_val_np_ratio)
        _manifest_pay["optuna_hpo_val_precision_prod_adjusted_active"] = bool(
            _ft_hpo_uses_prod_adj
        )
        if PRODUCTION_NEG_POS_RATIO is not None:
            try:
                _manifest_pay["optuna_hpo_production_neg_pos_ratio_assumed"] = float(
                    PRODUCTION_NEG_POS_RATIO
                )
            except (TypeError, ValueError):
                pass
    _write_optuna_hpo_manifest(hpo_objective_manifest, _manifest_pay)
    return best


def run_optuna_search(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    sw_train: pd.Series,
    n_trials: int = OPTUNA_N_TRIALS,
    label: str = "",
    field_test_constrained_optuna_objective_allowed: Optional[bool] = None,
    val_window_hours: Optional[float] = None,
    hpo_objective_manifest: Optional[list[dict[str, Any]]] = None,
) -> dict:
    """Backward-compatible LightGBM Optuna wrapper."""
    return run_backend_optuna_search(
        X_train,
        y_train,
        X_val,
        y_val,
        sw_train,
        backend="lightgbm",
        n_trials=n_trials,
        label=label,
        field_test_constrained_optuna_objective_allowed=field_test_constrained_optuna_objective_allowed,
        val_window_hours=val_window_hours,
        hpo_objective_manifest=hpo_objective_manifest,
    )


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
    log_results: bool = True,
    val_dec026_window_hours: Optional[float] = None,
    val_dec026_min_alerts_per_hour: Optional[float] = None,
) -> Tuple[lgb.LGBMClassifier, dict]:
    """Train a single LightGBM model and compute validation metrics.

    When *val_dec026_window_hours* and *val_dec026_min_alerts_per_hour* are set (rated
    field-test path), validation DEC-026 pick uses the same per-hour floor as Optuna trials.
    """
    # R1509: guard single-class training set (LightGBM would train a constant predictor).
    if y_train.nunique() < 2:
        raise ValueError(
            "%s: training set has only one class (y_train.nunique()=%d); need both 0 and 1."
            % (label or "model", int(y_train.nunique()))
        )
    params = {**_lgb_params_for_pipeline(), **hyperparams}
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

        # Threshold selection: shared DEC-026 helper (threshold_selection.pick_threshold_dec026).
        _pick = pick_threshold_dec026(
            np.asarray(y_val, dtype=float),
            np.asarray(val_scores, dtype=float),
            recall_floor=THRESHOLD_MIN_RECALL,
            min_alert_count=THRESHOLD_MIN_ALERT_COUNT,
            min_alerts_per_hour=val_dec026_min_alerts_per_hour,
            window_hours=val_dec026_window_hours,
            fbeta_beta=THRESHOLD_FBETA,
        )
        if _pick.is_fallback:
            best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
            best_fbeta = 0.0
        else:
            best_t = _pick.threshold
            best_prec = _pick.precision
            best_rec = _pick.recall
            best_fbeta = _pick.fbeta
            best_f1 = _pick.f1
    else:
        prauc = 0.0
        best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
        best_fbeta = 0.0

    n_val = int(len(y_val))
    n_val_pos = int(y_val.sum())
    val_random_ap = (n_val_pos / n_val) if n_val > 0 else 0.0

    metrics = {
        "label": label,
        "val_ap": prauc,
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
    if (
        val_dec026_window_hours is not None
        and val_dec026_min_alerts_per_hour is not None
    ):
        metrics["val_dec026_pick_window_hours"] = float(val_dec026_window_hours)
        metrics["val_dec026_pick_min_alerts_per_hour"] = float(
            val_dec026_min_alerts_per_hour
        )
    if log_results:
        logger.info(
            "%s valid: AP=%.4f  F0.5=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f",
            label, prauc, best_fbeta, best_f1, best_prec, best_rec, best_t,
        )
    return model, metrics


def _precision_prod_adjusted(
    prec: Optional[float],
    *,
    production_neg_pos_ratio: Optional[float],
    test_neg_pos_ratio: Optional[float],
) -> Optional[float]:
    """Rescale raw precision for assumed production neg/pos ratio (test_precision_prod_adjusted formula).

    Returns None when inputs are missing, non-finite, out of range, or when the closed form
    would yield a non-finite or out-of-[0,1] value (JSON-safe contract).
    """
    if prec is None:
        return None
    p = float(prec)
    if not math.isfinite(p) or p <= 0.0:
        return None
    if p > 1.0 + 1e-9:
        return None
    if p > 1.0:
        p = 1.0
    if production_neg_pos_ratio is None or test_neg_pos_ratio is None:
        return None
    pn = float(production_neg_pos_ratio)
    tn = float(test_neg_pos_ratio)
    if not math.isfinite(pn) or not math.isfinite(tn) or pn <= 0.0 or tn <= 0.0:
        return None
    scaling = pn / tn
    if not math.isfinite(scaling):
        return None
    inv_p = 1.0 / p
    if not math.isfinite(inv_p):
        return None
    term = (inv_p - 1.0) * scaling
    if not math.isfinite(term):
        return None
    denom = 1.0 + term
    if not math.isfinite(denom) or denom <= 0.0:
        return None
    adj = 1.0 / denom
    if not math.isfinite(adj):
        return None
    if adj < -1e-9 or adj > 1.0 + 1e-9:
        return None
    if adj < 0.0:
        return 0.0
    if adj > 1.0:
        return 1.0
    return float(adj)


def _warn_if_invalid_production_neg_pos_ratio(ratio: Optional[float]) -> None:
    """Log one warning when production neg/pos ratio cannot be used for prod_adjusted fields."""
    if ratio is None:
        return
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        logger.warning(
            "PRODUCTION_NEG_POS_RATIO=%r is invalid (must be finite and > 0); "
            "all prod_adjusted test precision fields (including precision@recall *_prod_adjusted) will be None.",
            ratio,
        )
        return
    if not math.isfinite(r) or r <= 0.0:
        logger.warning(
            "PRODUCTION_NEG_POS_RATIO=%r is invalid (must be finite and > 0); "
            "all prod_adjusted test precision fields (including precision@recall *_prod_adjusted) will be None.",
            ratio,
        )


def _compute_test_metrics(
    model: Union[lgb.LGBMClassifier, "_BoosterWrapper"],
    threshold: float,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    label: str = "",
    _uncalibrated: bool = False,
    log_results: bool = True,
    production_neg_pos_ratio: Optional[float] = None,
) -> dict:
    """Evaluate a trained model on the held-out test set at the val-derived threshold.

    Uses the same MIN_VALID_TEST_ROWS guard as _train_one_model so an under-sized
    test split returns zeroed metrics rather than crashing.  test_ap is computed
    without any threshold so it is comparable to val_ap.

    R1100: requires at least one negative label so average precision is meaningful.
    R1101: _uncalibrated=True is propagated into test_threshold_uncalibrated key.
    R1105: y_test.values is used for positional comparisons to avoid index misalign.

    Additional reporting:
    - test_precision_at_recall_{r}: highest precision achievable at recall >= r,
      computed from the PR curve (threshold-free). Reported for r in (0.001, 0.01, 0.1, 0.5) (DEC-026).
    - threshold_at_recall_{r}, n_alerts_at_recall_{r}: operating point at that precision; alerts_per_minute_at_recall_{r} is None (trainer has no test window length).
    - test_precision_prod_adjusted: test_precision rescaled to the assumed production
      neg/pos ratio (production_neg_pos_ratio). Only computed when
      production_neg_pos_ratio is not None and > 0.
    - test_precision_at_recall_{r}_prod_adjusted: same closed-form rescaling applied to each
      test_precision_at_recall_{r} (approximation at that PR operating point; None when not JSON-safe).
    """
    _TARGET_RECALLS = (0.001, 0.01, 0.1, 0.5)  # DEC-026
    _zeroed_recall_keys: dict = {
        f"test_precision_at_recall_{r}": None for r in _TARGET_RECALLS
    }
    for r in _TARGET_RECALLS:
        _zeroed_recall_keys[f"threshold_at_recall_{r}"] = None
        _zeroed_recall_keys[f"n_alerts_at_recall_{r}"] = None
        _zeroed_recall_keys[f"alerts_per_minute_at_recall_{r}"] = None
        _zeroed_recall_keys[f"test_precision_at_recall_{r}_prod_adjusted"] = None

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
            "test_ap": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            # R1101: propagate uncalibrated flag
            "test_threshold_uncalibrated": _uncalibrated,
            **_zeroed_recall_keys,
            "test_precision_prod_adjusted": None,
            "test_neg_pos_ratio": None,
            "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
        }

    test_scores = model.predict_proba(X_test)[:, 1]
    if not np.isfinite(test_scores).all():
        logger.warning(
            "%s: test predict_proba scores contain non-finite values — test metrics will be zero.",
            label or "model",
        )
        n_te = int(len(y_test))
        n_te_pos = int(y_test.sum()) if not y_test.empty else 0
        return {
            "test_ap": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            "test_threshold_uncalibrated": _uncalibrated,
            **_zeroed_recall_keys,
            "test_precision_prod_adjusted": None,
            "test_neg_pos_ratio": None,
            "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
        }

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
    n_te_neg = int((y_test == 0).sum())
    test_random_ap = (n_te_pos / n_te) if n_te > 0 else 0.0
    test_neg_pos_ratio: Optional[float] = (n_te_neg / n_te_pos) if n_te_pos > 0 else None

    # --- Precision at fixed recall levels (threshold-free, from PR curve) ---
    # For each target recall R, find the maximum precision among all PR-curve
    # points where recall >= R; also record threshold and n_alerts at that point (DEC-026).
    pr_prec_arr, pr_rec_arr, pr_thresholds = precision_recall_curve(y_test, test_scores)
    pr_prec = pr_prec_arr[:-1]
    pr_rec = pr_rec_arr[:-1]
    precision_at_recall: dict = {}
    for r in _TARGET_RECALLS:
        mask = pr_rec >= r
        if mask.any():
            valid_idx = np.where(mask)[0]
            best_local = int(np.argmax(pr_prec[valid_idx]))
            best_idx = int(valid_idx[best_local])
            thr_r = float(pr_thresholds[best_idx])
            n_alerts_r = int((test_scores >= thr_r).sum())
            precision_at_recall[f"test_precision_at_recall_{r}"] = float(pr_prec[best_idx])
            precision_at_recall[f"threshold_at_recall_{r}"] = thr_r
            precision_at_recall[f"n_alerts_at_recall_{r}"] = n_alerts_r
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None  # trainer has no test window
        else:
            precision_at_recall[f"test_precision_at_recall_{r}"] = None
            precision_at_recall[f"threshold_at_recall_{r}"] = None
            precision_at_recall[f"n_alerts_at_recall_{r}"] = None
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None

    for r in _TARGET_RECALLS:
        _raw_par = precision_at_recall.get(f"test_precision_at_recall_{r}")
        precision_at_recall[f"test_precision_at_recall_{r}_prod_adjusted"] = _precision_prod_adjusted(
            float(_raw_par) if _raw_par is not None else None,
            production_neg_pos_ratio=production_neg_pos_ratio,
            test_neg_pos_ratio=test_neg_pos_ratio,
        )

    # --- Production-prior adjusted precision ---
    # Rescales test precision to the expected production neg/pos ratio using the
    # Bayes-consistent approximation: 1/P - 1 scales linearly with neg/pos ratio.
    # Only meaningful when negatives were downsampled (neg_sample_frac < 1.0) and
    # production_neg_pos_ratio is provided.
    test_precision_prod_adjusted = _precision_prod_adjusted(
        prec,
        production_neg_pos_ratio=production_neg_pos_ratio,
        test_neg_pos_ratio=test_neg_pos_ratio,
    )
    _warn_if_invalid_production_neg_pos_ratio(production_neg_pos_ratio)

    if log_results:
        _adj_str = (
            f"  prec_prod_adj={test_precision_prod_adjusted:.4f}"
            if test_precision_prod_adjusted is not None
            else ""
        )
        _par_str = "  ".join(
            f"prec@rec{r}={precision_at_recall[f'test_precision_at_recall_{r}']:.4f}"
            if precision_at_recall[f"test_precision_at_recall_{r}"] is not None
            else f"prec@rec{r}=N/A"
            for r in _TARGET_RECALLS
        )
        _thr_apm_str = "  ".join(
            f"thr@rec{r}={precision_at_recall[f'threshold_at_recall_{r}']:.4f} n={precision_at_recall[f'n_alerts_at_recall_{r}']}"
            if precision_at_recall[f"threshold_at_recall_{r}"] is not None
            else f"thr@rec{r}=N/A"
            for r in _TARGET_RECALLS
        )
        logger.info(
            "%s test: AP=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f%s",
            label, prauc, f1, prec, rec, threshold, _adj_str,
        )
        logger.info("%s test PR-curve: %s", label, _par_str)
        logger.info("%s test thr/n_alerts@rec: %s", label, _thr_apm_str)
    return {
        "test_ap": prauc,
        "test_precision": prec,
        "test_recall": rec,
        "test_f1": f1,
        "test_samples": n_te,
        "test_positives": n_te_pos,
        "test_random_ap": test_random_ap,
        # R1101: propagate uncalibrated flag so downstream can distrust P/R/F1
        "test_threshold_uncalibrated": _uncalibrated,
        **precision_at_recall,
        "test_precision_prod_adjusted": test_precision_prod_adjusted,
        "test_neg_pos_ratio": test_neg_pos_ratio,
        "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
    }


def _compute_test_metrics_from_scores(
    y_test: np.ndarray,
    test_scores: np.ndarray,
    threshold: float,
    label: str = "",
    _uncalibrated: bool = False,
    log_results: bool = True,
    production_neg_pos_ratio: Optional[float] = None,
) -> dict:
    """Compute test-set metrics from precomputed scores (PLAN B+ 階段 6 第 3 步: test from file).

    Same keys as _compute_test_metrics; used when test labels and predictions come from
    LibSVM file (no X_test in memory). y_test and test_scores must be 1d arrays of same length.
    """
    _TARGET_RECALLS = (0.001, 0.01, 0.1, 0.5)  # DEC-026
    _zeroed_recall_keys = {f"test_precision_at_recall_{r}": None for r in _TARGET_RECALLS}
    for r in _TARGET_RECALLS:
        _zeroed_recall_keys[f"threshold_at_recall_{r}"] = None
        _zeroed_recall_keys[f"n_alerts_at_recall_{r}"] = None
        _zeroed_recall_keys[f"alerts_per_minute_at_recall_{r}"] = None
        _zeroed_recall_keys[f"test_precision_at_recall_{r}_prod_adjusted"] = None
    y_arr = np.asarray(y_test).reshape(-1)
    scores_arr = np.asarray(test_scores).reshape(-1)
    if len(y_arr) != len(scores_arr):
        n = min(len(y_arr), len(scores_arr))
        y_arr = y_arr[:n]
        scores_arr = scores_arr[:n]
    n_te = int(len(y_arr))
    n_te_pos = int(np.nansum(y_arr))
    n_te_neg = int(np.sum(np.asarray(y_arr == 0, dtype=float)))
    test_neg_pos_ratio: Optional[float] = (n_te_neg / n_te_pos) if n_te_pos > 0 else None
    _has_test = (
        n_te >= MIN_VALID_TEST_ROWS
        and int(np.isnan(y_arr).sum()) == 0
        and n_te_pos >= 1
        and n_te_neg >= 1
    )
    if not _has_test:
        logger.warning(
            "%s: test from file too small or unbalanced (%d rows, %d pos, %d neg) — test metrics zero.",
            label or "model", n_te, n_te_pos, n_te_neg,
        )
        return {
            "test_ap": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            "test_threshold_uncalibrated": _uncalibrated,
            **_zeroed_recall_keys,
            "test_precision_prod_adjusted": None,
            "test_neg_pos_ratio": None,
            "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
        }
    if not np.isfinite(scores_arr).all():
        logger.warning(
            "%s: test scores (from file) contain non-finite values — test metrics will be zero.",
            label or "model",
        )
        return {
            "test_ap": 0.0,
            "test_precision": 0.0,
            "test_recall": 0.0,
            "test_f1": 0.0,
            "test_samples": n_te,
            "test_positives": n_te_pos,
            "test_random_ap": (n_te_pos / n_te) if n_te > 0 else 0.0,
            "test_threshold_uncalibrated": _uncalibrated,
            **_zeroed_recall_keys,
            "test_precision_prod_adjusted": None,
            "test_neg_pos_ratio": None,
            "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
        }
    prauc = float(average_precision_score(y_arr, scores_arr))
    preds = (scores_arr >= threshold).astype(int)
    tp = int(((preds == 1) & (y_arr == 1)).sum())
    fp = int(((preds == 1) & (y_arr == 0)).sum())
    fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    test_random_ap = (n_te_pos / n_te) if n_te > 0 else 0.0
    pr_prec_arr, pr_rec_arr, pr_thresholds = precision_recall_curve(y_arr, scores_arr)
    pr_prec = pr_prec_arr[:-1]
    pr_rec = pr_rec_arr[:-1]
    precision_at_recall: dict[str, Optional[float]] = {}
    for r in _TARGET_RECALLS:
        mask = pr_rec >= r
        if mask.any():
            valid_idx = np.where(mask)[0]
            best_local = int(np.argmax(pr_prec[valid_idx]))
            best_idx = int(valid_idx[best_local])
            thr_r = float(pr_thresholds[best_idx])
            n_alerts_r = int((scores_arr >= thr_r).sum())
            precision_at_recall[f"test_precision_at_recall_{r}"] = float(pr_prec[best_idx])
            precision_at_recall[f"threshold_at_recall_{r}"] = thr_r
            precision_at_recall[f"n_alerts_at_recall_{r}"] = n_alerts_r
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None
        else:
            precision_at_recall[f"test_precision_at_recall_{r}"] = None
            precision_at_recall[f"threshold_at_recall_{r}"] = None
            precision_at_recall[f"n_alerts_at_recall_{r}"] = None
            precision_at_recall[f"alerts_per_minute_at_recall_{r}"] = None
    for r in _TARGET_RECALLS:
        _raw_par = precision_at_recall.get(f"test_precision_at_recall_{r}")
        precision_at_recall[f"test_precision_at_recall_{r}_prod_adjusted"] = _precision_prod_adjusted(
            float(_raw_par) if _raw_par is not None else None,
            production_neg_pos_ratio=production_neg_pos_ratio,
            test_neg_pos_ratio=test_neg_pos_ratio,
        )
    test_precision_prod_adjusted = _precision_prod_adjusted(
        prec,
        production_neg_pos_ratio=production_neg_pos_ratio,
        test_neg_pos_ratio=test_neg_pos_ratio,
    )
    _warn_if_invalid_production_neg_pos_ratio(production_neg_pos_ratio)
    if log_results:
        _adj_str = f"  prec_prod_adj={test_precision_prod_adjusted:.4f}" if test_precision_prod_adjusted is not None else ""
        logger.info(
            "%s test (from file): AP=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f%s",
            label, prauc, f1, prec, rec, threshold, _adj_str,
        )
    return {
        "test_ap": prauc,
        "test_precision": prec,
        "test_recall": rec,
        "test_f1": f1,
        "test_samples": n_te,
        "test_positives": n_te_pos,
        "test_random_ap": test_random_ap,
        "test_threshold_uncalibrated": _uncalibrated,
        **precision_at_recall,
        "test_precision_prod_adjusted": test_precision_prod_adjusted,
        "test_neg_pos_ratio": test_neg_pos_ratio,
        "production_neg_pos_ratio_assumed": production_neg_pos_ratio,
    }


def _compute_valid_metrics_from_scores(
    y_valid: Union[np.ndarray, pd.Series],
    valid_scores: np.ndarray,
    threshold: float,
) -> dict[str, Any]:
    """Compute lightweight validation metrics from precomputed scores."""
    y_arr = np.asarray(y_valid, dtype=float).reshape(-1)
    s_arr = np.asarray(valid_scores, dtype=float).reshape(-1)
    if len(y_arr) != len(s_arr):
        n = min(len(y_arr), len(s_arr))
        y_arr = y_arr[:n]
        s_arr = s_arr[:n]
    n_val = int(len(y_arr))
    n_val_pos = int(np.sum(y_arr == 1))
    n_val_neg = int(np.sum(y_arr == 0))
    if n_val == 0:
        return {
            "val_ap": 0.0,
            "val_precision": 0.0,
            "val_recall": 0.0,
            "val_f1": 0.0,
            "val_fbeta_05": 0.0,
            "val_samples": 0,
            "val_positives": 0,
            "val_random_ap": 0.0,
        }
    has_both = n_val_pos >= 1 and n_val_neg >= 1 and np.isfinite(s_arr).all()
    val_ap = float(average_precision_score(y_arr == 1, s_arr)) if has_both else 0.0
    preds = (s_arr >= float(threshold)).astype(int)
    tp = int(((preds == 1) & (y_arr == 1)).sum())
    fp = int(((preds == 1) & (y_arr == 0)).sum())
    fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    beta = float(THRESHOLD_FBETA)
    b2 = beta * beta
    fbeta = ((1.0 + b2) * prec * rec / (b2 * prec + rec)) if (b2 * prec + rec) > 0 else 0.0
    return {
        "val_ap": val_ap,
        "val_precision": prec,
        "val_recall": rec,
        "val_f1": f1,
        "val_fbeta_05": fbeta,
        "val_samples": n_val,
        "val_positives": n_val_pos,
        "val_random_ap": (n_val_pos / n_val) if n_val > 0 else 0.0,
    }


def _dataframe_for_lgb_predict(
    model: Union[lgb.LGBMClassifier, "_BoosterWrapper"],
    df: pd.DataFrame,
    avail_cols: List[str],
) -> pd.DataFrame:
    """Return a DataFrame with columns matching the booster's feature names for predict (e.g. f0..f49 when trained from LibSVM without feature_name)."""
    X = df[avail_cols]
    booster = getattr(model, "booster_", None)
    if booster is None or not avail_cols:
        return X
    fnames = booster.feature_name()
    if not fnames or fnames[0] != "f0" or len(fnames) != len(avail_cols):
        return X
    X = X.copy()
    X.columns = fnames
    return X


def _batched_booster_predict_scores(
    booster: lgb.Booster,
    X_train: pd.DataFrame,
    batch_rows: int,
) -> np.ndarray:
    """Chunked ``booster.predict`` on in-memory features (DEC-031 / T-DEC031).

    Avoids sklearn ``predict_proba`` allocating one huge dense probability matrix for
    large training sets when only the positive-class score is needed.
    """
    n = int(len(X_train))
    if n == 0:
        return np.asarray([], dtype=np.float64)
    br = max(1, int(batch_rows))
    parts: list[np.ndarray] = []
    for start in range(0, n, br):
        chunk = X_train.iloc[start : start + br]
        arr = np.ascontiguousarray(chunk.to_numpy(dtype=np.float32, copy=True))
        raw = booster.predict(arr)
        pa = np.asarray(raw).reshape(-1)
        parts.append(pa.astype(np.float64, copy=False))
    return np.concatenate(parts, axis=0)


def _batched_model_positive_class_scores(
    model: Any,
    X: pd.DataFrame,
    batch_rows: int,
) -> np.ndarray:
    """Chunked positive-class scores for any sklearn-like classifier.

    LightGBM ``booster_`` keeps the dedicated fast path.  Other backends (CatBoost /
    XGBoost sklearn wrappers) fall back to chunked ``predict_proba`` to avoid one giant
    dense probability matrix on laptop-scale runs.
    """
    booster = getattr(model, "booster_", None)
    if booster is not None:
        return _batched_booster_predict_scores(booster, X, batch_rows)
    n = int(len(X))
    if n == 0:
        return np.asarray([], dtype=np.float64)
    br = max(1, int(batch_rows))
    parts: list[np.ndarray] = []
    for start in range(0, n, br):
        chunk = X.iloc[start : start + br]
        raw = model.predict_proba(chunk)[:, 1]
        parts.append(np.asarray(raw, dtype=np.float64).reshape(-1))
    return np.concatenate(parts, axis=0)


def _train_metrics_dict_from_y_scores(
    y_train: Union[np.ndarray, pd.Series],
    train_scores: np.ndarray,
    threshold: float,
    label: str = "",
    log_results: bool = True,
) -> dict:
    """Build train_* metrics from parallel label/score arrays (same rules as legacy train metrics)."""
    y_arr = np.asarray(y_train, dtype=float).reshape(-1)
    scores_arr = np.asarray(train_scores, dtype=float).reshape(-1)
    if len(y_arr) != len(scores_arr):
        n_fix = min(len(y_arr), len(scores_arr))
        y_arr = y_arr[:n_fix]
        scores_arr = scores_arr[:n_fix]
    n_tr = int(len(y_arr))
    if n_tr == 0:
        return {
            "train_ap": 0.0,
            "train_precision": 0.0,
            "train_recall": 0.0,
            "train_f1": 0.0,
            "train_samples": 0,
            "train_positives": 0,
            "train_random_ap": 0.0,
        }
    n_tr_pos = int(np.sum(y_arr == 1))
    train_random_ap = (n_tr_pos / n_tr) if n_tr > 0 else 0.0
    if not np.isfinite(scores_arr).all():
        logger.warning(
            "%s train: scores contain non-finite values — train metrics set to zero.",
            label or "model",
        )
        return {
            "train_ap": 0.0,
            "train_precision": 0.0,
            "train_recall": 0.0,
            "train_f1": 0.0,
            "train_samples": n_tr,
            "train_positives": n_tr_pos,
            "train_random_ap": train_random_ap,
        }
    has_both = n_tr_pos >= 1 and (n_tr - n_tr_pos) >= 1
    # sklearn average_precision_score requires binary {0,1} y; use strict-positive mask only for AP.
    y_ap = np.asarray(y_arr == 1, dtype=np.float64).reshape(-1)
    train_prauc = float(average_precision_score(y_ap, scores_arr)) if has_both else 0.0
    preds = (scores_arr >= threshold).astype(int)
    tp = int(((preds == 1) & (y_arr == 1)).sum())
    fp = int(((preds == 1) & (y_arr == 0)).sum())
    fn = int(((preds == 0) & (y_arr == 1)).sum())
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    if log_results:
        logger.info(
            "%s train: AP=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  random_ap=%.4f",
            label, train_prauc, f1, prec, rec, train_random_ap,
        )
    return {
        "train_ap": train_prauc,
        "train_precision": prec,
        "train_recall": rec,
        "train_f1": f1,
        "train_samples": n_tr,
        "train_positives": n_tr_pos,
        "train_random_ap": train_random_ap,
    }


def _compute_train_metrics(
    model: Union[lgb.LGBMClassifier, "_BoosterWrapper"],
    threshold: float,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    label: str = "",
    log_results: bool = True,
) -> dict:
    """Evaluate a trained model on the training set (for reporting overfit / fit quality).

    Reports train_ap, P/R/F1 at the validation-derived threshold, train_samples,
    train_positives, and train_random_ap (positives/samples = theoretical AP for random guess).
    """
    if X_train.empty or y_train.empty:
        return {
            "train_ap": 0.0,
            "train_precision": 0.0,
            "train_recall": 0.0,
            "train_f1": 0.0,
            "train_samples": 0,
            "train_positives": 0,
            "train_random_ap": 0.0,
        }
    try:
        batched = _batched_model_positive_class_scores(
            model, X_train, TRAIN_METRICS_PREDICT_BATCH_ROWS
        )
        return _train_metrics_dict_from_y_scores(
            y_train,
            batched,
            threshold,
            label=label,
            log_results=log_results,
        )
    except Exception as exc:
        logger.warning(
            "Train metrics: batched positive-class predict failed (%s); falling back to predict_proba.",
            exc,
        )
    train_scores = model.predict_proba(X_train)[:, 1]
    return _train_metrics_dict_from_y_scores(
        y_train,
        np.asarray(train_scores, dtype=np.float64).reshape(-1),
        threshold,
        label=label,
        log_results=log_results,
    )


def _compute_feature_importance(
    model: Union[lgb.LGBMClassifier, "_BoosterWrapper"],
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
        gains = model.feature_importances_.tolist()  # type: ignore[union-attr]
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
    ranking_recipe: Optional[str] = None,
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
    run_optuna         : whether to run Optuna HPO (skipped when --skip-optuna)
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

    sw_rated_base = compute_sample_weights(train_rated)
    sw_nonrated_base = compute_sample_weights(train_nonrated)
    _recipe_dual = resolve_ranking_recipe(ranking_recipe)

    _ft_pre_doc: Optional[Dict[str, Any]] = None
    _ft_pre_path_raw = (os.environ.get(FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV) or "").strip()
    if _ft_pre_path_raw:
        _ft_pre_doc = try_load_precondition_json(Path(_ft_pre_path_raw))
        if _ft_pre_doc is None:
            logger.warning(
                "%s set but file missing or invalid: %s",
                FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV,
                _ft_pre_path_raw,
            )
        else:
            log_precondition_block_warning(_ft_pre_doc)
    _ft_optuna_allowed = precondition_constrained_optuna_allowed(_ft_pre_doc)

    results: dict[str, Any] = {}
    for name, tr_df, vl_df, te_df, sw_base in [
        ("rated",    train_rated,    val_rated,    _test_rated,    sw_rated_base),
        ("nonrated", train_nonrated, val_nonrated, _test_nonrated, sw_nonrated_base),
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

        sw = sw_base.astype(float).copy()
        _r2_meta: Dict[str, Any] = {}
        if name == "rated":
            sw, _r2_meta = apply_ranking_recipe_pre_optuna_weights(
                tr_df, sw, _recipe_dual, avail_cols
            )

        _vw_h = _val_window_hours_from_payout_df(vl_df) if name == "rated" else None
        _ft_hpo_active = name == "rated" and _ft_optuna_allowed and _vw_h is not None
        _optuna_hpo_manifest_loop: list[dict[str, Any]] = []
        if run_optuna and not vl_df.empty and y_vl.sum() > 0:
            log_optuna_precondition_context(
                _ft_pre_doc, uses_field_test_hpo_objective=_ft_hpo_active
            )
            hp = run_optuna_search(
                X_tr,
                y_tr,
                X_vl,
                y_vl,
                sw,
                label=name,
                field_test_constrained_optuna_objective_allowed=_ft_optuna_allowed,
                val_window_hours=_vw_h,
                hpo_objective_manifest=_optuna_hpo_manifest_loop,
            )
        else:
            # Default params when validation is empty or no positives
            hp = {
                "n_estimators": 400,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "max_depth": 8,
                "min_child_samples": 20,
            }

        _dec026_wh, _dec026_mah = _rated_field_test_val_pick_per_hour_kwargs(
            label=name,
            field_test_constrained_optuna_objective_allowed=_ft_optuna_allowed,
            val_df=vl_df,
        )
        model, metrics = _train_one_model(
            X_tr,
            y_tr,
            X_vl,
            y_vl,
            sw,
            hp,
            label=name,
            val_dec026_window_hours=_dec026_wh,
            val_dec026_min_alerts_per_hour=_dec026_mah,
        )

        if _optuna_hpo_manifest_loop:
            metrics.update(_optuna_hpo_manifest_loop[0])

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
                    production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                )
            )

        # Feature importance ranked by LightGBM gain.
        metrics["feature_importance"] = _compute_feature_importance(model, avail_cols)
        metrics["importance_method"] = "gain"
        if _r2_meta:
            metrics.update(_r2_meta)

        if name == "rated" and _ft_pre_doc is not None and _ft_pre_path_raw:
            metrics.update(
                training_metrics_overlay_from_precondition(
                    _ft_pre_doc, source_path=_ft_pre_path_raw
                )
            )

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
    valid_df: Optional[pd.DataFrame],
    feature_cols: List[str],
    run_optuna: bool = True,
    test_df: Optional[pd.DataFrame] = None,
    train_from_file: bool = False,
    train_libsvm_paths: Optional[Tuple[Path, Path]] = None,
    test_libsvm_path: Optional[Path] = None,
    ranking_recipe: Optional[str] = None,
    gbm_bakeoff: bool = False,
    valid_split_parquet_path: Optional[Path] = None,
    test_split_parquet_path: Optional[Path] = None,
) -> Tuple[Optional[dict], Optional[dict], dict]:
    """Train one rated artifact entry and return ``(rated_art, None, metrics)``.

    Only rows where is_rated==True are used for training, validation, and test
    evaluation.  Non-rated observations are intentionally excluded (DEC-009/010).

    When train_from_file is True (PLAN 方案 B §4), training uses on-disk CSV from
    DATA_DIR/export (train_for_lgb.csv, valid_for_lgb.csv). A thin Booster wrapper
    (§5) is returned so scorer and artifact save work unchanged.

    When train_libsvm_paths is (train_path, valid_path) and both files exist (PLAN B+ §4.4),
    training uses lgb.Dataset(path) so train data is not loaded into memory; .weight
    file alongside train path is auto-loaded by LightGBM 4.6.0.

    When valid_df is None and train_libsvm_paths is set (PLAN B+ 階段 6), validation
    labels and predictions are read from the valid LibSVM file; path must be under DATA_DIR.

    When test_df is None and test_libsvm_path is set (PLAN B+ 階段 6 第 3 步), test
    labels and predictions are read from the test LibSVM file; path must be under DATA_DIR.

    When *gbm_bakeoff* is True (A3 / R3), after the primary LightGBM path completes we
    always compare LightGBM / CatBoost / XGBoost on the same rated train/valid/test
    matrices and select the winner by field-test validation objective.  Main-path
    LibSVM / CSV optimizations remain valid for LightGBM, but no longer suppress A3.
    """
    _ft_pre_doc: Optional[Dict[str, Any]] = None
    _ft_pre_path_raw = ""
    _optuna_hpo_manifest: list[dict[str, Any]] = []
    if valid_df is None:
        valid_df = pd.DataFrame()
    use_from_libsvm = False
    if train_libsvm_paths is not None:
        _t, _v = train_libsvm_paths
        if _t.exists() and _v.exists():
            use_from_libsvm = True
        else:
            logger.warning(
                "train_libsvm_paths set but files missing (%s / %s); using in-memory training.",
                _t,
                _v,
            )

    use_from_file = False
    if train_from_file and not use_from_libsvm:
        train_path = DATA_DIR / "export" / "train_for_lgb.csv"
        valid_path = DATA_DIR / "export" / "valid_for_lgb.csv"
        if train_path.exists() and valid_path.exists():
            use_from_file = True
        else:
            logger.warning(
                "STEP9_TRAIN_FROM_FILE is True but export CSVs missing (%s / %s); using in-memory training.",
                train_path,
                valid_path,
            )

    if use_from_libsvm:
        train_libsvm_p, valid_libsvm_p = train_libsvm_paths  # type: ignore[misc]
        with open(train_libsvm_p, encoding="utf-8") as _f:
            _n_lines = sum(1 for _ in _f)
        if _n_lines < 1:
            logger.warning(
                "Plan B+: train LibSVM has 0 lines; falling back to in-memory training."
            )
            use_from_libsvm = False
        if use_from_libsvm:
            # R375 #6: single-class check (align with Plan B R188 #3 / R1509).
            with open(train_libsvm_p, encoding="utf-8") as _f:
                _labels = [line.split(None, 1)[0] for line in _f if line.strip()]
            if len(set(_labels)) < 2:
                logger.warning(
                    "Plan B+: train LibSVM has only one class; falling back to in-memory training."
                )
                use_from_libsvm = False

    train_rated: Optional[pd.DataFrame] = None
    val_rated: Optional[pd.DataFrame] = None
    test_rated: Optional[pd.DataFrame] = None
    _train_views_ready = False
    _train_rated_mutable = False
    _val_rated_mutable = False
    _test_rated_mutable = False
    X_tr = pd.DataFrame()
    y_tr: Union[pd.Series, np.ndarray] = pd.Series(dtype=float)
    X_vl = pd.DataFrame()
    y_vl: Union[pd.Series, np.ndarray] = pd.Series(dtype=float)

    def _get_train_rated(*, mutable: bool = False) -> pd.DataFrame:
        nonlocal train_rated, _train_rated_mutable
        if train_rated is None:
            if train_df.empty:
                train_rated = train_df.copy() if mutable else train_df
                _train_rated_mutable = mutable
            elif bool(train_df["is_rated"].all()):
                train_rated = train_df.copy() if mutable else train_df
                _train_rated_mutable = mutable
            else:
                train_rated = (
                    train_df.loc[train_df["is_rated"]].copy()
                    if mutable
                    else train_df.loc[train_df["is_rated"]]
                )
                _train_rated_mutable = mutable
        elif mutable and not _train_rated_mutable:
            train_rated = train_rated.copy()
            _train_rated_mutable = True
        return train_rated

    def _get_val_rated(*, mutable: bool = False) -> pd.DataFrame:
        nonlocal val_rated, _val_rated_mutable
        if val_rated is None:
            if valid_df.empty:
                val_rated = valid_df.copy() if mutable else valid_df
                _val_rated_mutable = mutable
            elif bool(valid_df["is_rated"].all()):
                val_rated = valid_df.copy() if mutable else valid_df
                _val_rated_mutable = mutable
            else:
                val_rated = (
                    valid_df.loc[valid_df["is_rated"]].copy()
                    if mutable
                    else valid_df.loc[valid_df["is_rated"]]
                )
                _val_rated_mutable = mutable
        elif mutable and not _val_rated_mutable:
            val_rated = val_rated.copy()
            _val_rated_mutable = True
        return val_rated

    def _get_test_rated(*, mutable: bool = False) -> Optional[pd.DataFrame]:
        nonlocal test_rated, _test_rated_mutable
        if test_rated is None and test_df is not None:
            if test_df.empty:
                test_rated = test_df.copy() if mutable else test_df
                _test_rated_mutable = mutable
            elif bool(test_df["is_rated"].all()):
                test_rated = test_df.copy() if mutable else test_df
                _test_rated_mutable = mutable
            else:
                test_rated = (
                    test_df.loc[test_df["is_rated"]].copy()
                    if mutable
                    else test_df.loc[test_df["is_rated"]]
                )
                _test_rated_mutable = mutable
        elif mutable and test_rated is not None and not _test_rated_mutable:
            test_rated = test_rated.copy()
            _test_rated_mutable = True
        return test_rated

    def _ensure_inmemory_train_views(feature_names: List[str]) -> None:
        nonlocal _train_views_ready, X_tr, y_tr, X_vl, y_vl
        tr = _get_train_rated(mutable=True)
        vr = _get_val_rated(mutable=True)
        if _train_views_ready:
            return
        # Copy reduction for LibSVM path: only materialize rated pandas matrices when
        # a downstream branch truly needs them (fallback, bakeoff, A4, or in-memory train).
        coerce_feature_dtypes(tr, feature_names)
        if not vr.empty:
            coerce_feature_dtypes(vr, feature_names)
        X_tr = tr[feature_names]
        y_tr = tr["label"]
        X_vl = vr[feature_names] if not vr.empty else X_tr.head(0)
        if not isinstance(y_vl, np.ndarray):
            y_vl = vr["label"] if not vr.empty else y_tr.head(0)
        _train_views_ready = True

    if not use_from_libsvm and _get_train_rated().empty:
        logger.warning("rated model: no training rows, skipping")
        return None, None, {"rated": None}

    recipe_use = resolve_ranking_recipe(ranking_recipe)
    _valid_cols = valid_df.columns if not valid_df.empty else pd.Index([])
    if use_from_libsvm:
        avail_cols = [c for c in feature_cols if c in _valid_cols]
        if not avail_cols:
            avail_cols = list(feature_cols)
    else:
        avail_cols = [c for c in feature_cols if c in _get_train_rated().columns]
        if len(_valid_cols) > 0:
            avail_cols = [c for c in avail_cols if c in _valid_cols]

    if (not use_from_libsvm) or gbm_bakeoff or A4_TWO_STAGE_ENABLE_TRAINING:
        _ensure_inmemory_train_views(avail_cols)

    _train_rated_for_weights = _get_train_rated()
    sw_base = (
        compute_sample_weights(_train_rated_for_weights)
        if not _train_rated_for_weights.empty
        else pd.Series(dtype=float)
    )
    _dupe_cols = [c for c in set(avail_cols) if avail_cols.count(c) > 1]
    # #region agent log
    _agent_debug_log(
        hypothesis_id="H2",
        location="trainer/training/trainer.py:train_single_rated_model:avail-cols",
        message="Prepared avail_cols for LightGBM Dataset",
        data={
            "use_from_libsvm": bool(use_from_libsvm),
            "feature_cols_len": len(feature_cols),
            "avail_cols_len": len(avail_cols),
            "avail_cols_unique_len": len(set(avail_cols)),
            "avail_cols_duplicates": _dupe_cols[:10],
            "has_label_in_avail_cols": ("label" in avail_cols),
            "avail_cols_head": avail_cols[:8],
        },
    )
    # #endregion

    # A2 / R2 canonical stage-1 weights (shared source for all backends).
    if not _get_train_rated().empty:
        sw_rated, ranking_meta_pre = build_final_ranking_weights_in_memory(
            _get_train_rated(),
            sw_base,
            recipe_use,
            avail_cols,
            lgb_classifier_params=None,
        )
    elif use_from_libsvm:
        train_libsvm_p, _valid_libsvm_p = train_libsvm_paths  # type: ignore[misc]
        _weight_path = Path(str(train_libsvm_p) + ".weight")
        sw_base_file = read_libsvm_weight_file(_weight_path, expected_rows=int(_n_lines))
        sw_rated, ranking_meta_pre = build_final_ranking_weights_from_libsvm_proxy(
            train_libsvm_p,
            sw_base_file,
            recipe_use,
        )
    else:
        sw_rated = sw_base.astype(float).copy()
        ranking_meta_pre = {
            "ranking_recipe": recipe_use,
            "ranking_weight_source": "empty_train",
            "ranking_weight_finalized": True,
            "ranking_hnm_mode": "none",
        }
    if not _get_train_rated().empty:
        logger.info(
            "R2 ranking recipe=%s stage1 weight_mean=%.6f max=%.6f source=%s",
            recipe_use,
            float(ranking_meta_pre.get("ranking_recipe_weight_mean", 0.0)),
            float(ranking_meta_pre.get("ranking_recipe_weight_max", 0.0)),
            ranking_meta_pre.get("ranking_weight_source", "unknown"),
        )

    _ft_pre_path_raw = (os.environ.get(FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV) or "").strip()
    if _ft_pre_path_raw:
        _ft_pre_doc = try_load_precondition_json(Path(_ft_pre_path_raw))
        if _ft_pre_doc is None:
            logger.warning(
                "%s set but file missing or invalid: %s",
                FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV,
                _ft_pre_path_raw,
            )
        else:
            log_precondition_block_warning(_ft_pre_doc)

    _ft_allowed = precondition_constrained_optuna_allowed(_ft_pre_doc)
    _ft_thr_wh, _ft_thr_mah = _rated_field_test_val_pick_per_hour_kwargs(
        label="rated",
        field_test_constrained_optuna_objective_allowed=_ft_allowed,
        val_df=_get_val_rated(),
    )

    # PLAN 方案 B §6: HPO on in-memory (train/valid) for both paths; from-file then uses best params for lgb.train.
    # B+ §4.4: from LibSVM we use default hp (no in-memory HPO).
    if use_from_libsvm:
        hp = {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "max_depth": 8,
            "min_child_samples": 20,
        }
    elif run_optuna and not _get_val_rated().empty and y_vl.sum() > 0:
        _val_wh = _val_window_hours_from_payout_df(_get_val_rated())
        _ft_hpo_active = _ft_allowed and _val_wh is not None
        log_optuna_precondition_context(
            _ft_pre_doc, uses_field_test_hpo_objective=_ft_hpo_active
        )
        hp = run_optuna_search(
            X_tr,
            y_tr,
            X_vl,
            y_vl,
            sw_rated,
            label="rated",
            field_test_constrained_optuna_objective_allowed=_ft_allowed,
            val_window_hours=_val_wh,
            hpo_objective_manifest=_optuna_hpo_manifest,
        )
    else:
        hp = {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "max_depth": 8,
            "min_child_samples": 20,
        }

    ranking_meta_hnm: Dict[str, Any] = {}
    if recipe_use in (RANKING_RECIPE_HNM, RANKING_RECIPE_COMBINED) and not _get_train_rated().empty:
        sw_rated, ranking_meta_hnm = refine_weights_hnm_shallow_lgbm(
            X_tr,
            y_tr,
            sw_rated,
            {**_lgb_params_for_pipeline(), **hp},
        )
        ranking_meta_hnm["ranking_hnm_mode"] = "in_memory_shallow_lgbm"
        logger.info(
            "R2 final weights built with HNM mode=%s boosted_negs=%s",
            ranking_meta_hnm.get("ranking_hnm_mode"),
            ranking_meta_hnm.get("ranking_recipe_hnm_shallow_neg_boosted"),
        )
    if use_from_libsvm:
        train_libsvm_p, _valid_libsvm_p = train_libsvm_paths  # type: ignore[misc]
        _weight_path = Path(str(train_libsvm_p) + ".weight")
        write_libsvm_weight_file(_weight_path, sw_rated)
        _invalidated_bin = invalidate_lgb_binary_cache_for_libsvm(train_libsvm_p)
        if _invalidated_bin is not None:
            logger.info("R2 LibSVM parity: invalidated stale binary cache %s", _invalidated_bin)
        ranking_meta_hnm["ranking_weight_source"] = "libsvm_rewritten"
        ranking_meta_hnm["ranking_weight_finalized"] = True

    if use_from_libsvm:
        # PLAN B+ §4.4: train from LibSVM file; LightGBM auto-loads .weight when beside .libsvm.
        train_libsvm_p, valid_libsvm_p = train_libsvm_paths  # type: ignore[misc]
        # PLAN B+ 階段 6: validation labels from file when valid_df not in memory (R216 Review #6: path under DATA_DIR only)
        _valid_path_under_data_dir = True
        if valid_df is None or (valid_df is not None and valid_df.empty):
            try:
                valid_libsvm_p.resolve().relative_to(DATA_DIR.resolve())
            except ValueError:
                logger.warning(
                    "Plan B+: valid LibSVM path %s is not under DATA_DIR; skipping validation from file.",
                    valid_libsvm_p,
                )
                y_vl = np.array([], dtype=np.float64)
                _valid_path_under_data_dir = False
            else:
                y_vl = _labels_from_libsvm(valid_libsvm_p)
        _has_val_from_file = (
            len(y_vl) >= MIN_VALID_TEST_ROWS
            and (int(y_vl.isna().sum()) if hasattr(y_vl, "isna") else int(np.isnan(y_vl).sum())) == 0
            and int(np.asarray(y_vl).sum()) >= 1
            and int((np.asarray(y_vl) == 0).sum()) >= 1
        )
        _bin_path = train_libsvm_p.parent / (train_libsvm_p.stem + ".bin")
        # R207 #2: use .bin only when _bin_path.is_file() (avoid using a directory as .bin).
        # LibSVM export uses 0-based feature indices (0..49 for 50 features) so LightGBM infers num_feature=50 and matches feature_name.
        # #region agent log
        _agent_debug_log(
            hypothesis_id="H4",
            location="trainer/training/trainer.py:train_single_rated_model:libsvm-branch",
            message="LibSVM training branch path status before Dataset construction",
            data={
                "train_libsvm_path": str(train_libsvm_p),
                "valid_libsvm_path": str(valid_libsvm_p),
                "bin_path": str(_bin_path),
                "bin_exists": bool(_bin_path.is_file()),
                "train_libsvm_lines": int(_n_lines),
                "avail_cols_len": len(avail_cols),
            },
        )
        # #endregion
        _libsvm_temp_to_remove: Optional[Path] = None
        if _bin_path.is_file():
            dtrain = lgb.Dataset(str(_bin_path))
            dvalid = lgb.Dataset(
                str(valid_libsvm_p),
                reference=dtrain,
                feature_name=list(avail_cols),
            )
        else:
            weight_path = Path(str(train_libsvm_p) + ".weight")
            _train_path_for_lgb: Union[str, Path] = train_libsvm_p
            if weight_path.exists():
                _train_weights_s = read_libsvm_weight_file(weight_path, expected_rows=int(_n_lines))
                _train_weights = _train_weights_s.to_list()
                if len(_train_weights) != _n_lines:
                    logger.warning(
                        "Plan B+: .weight file line count (%s) does not match train LibSVM line count (%s); ignoring weights.",
                        len(_train_weights),
                        _n_lines,
                    )
                    _train_weights = [1.0] * _n_lines
                    _fd, _tmp = tempfile.mkstemp(suffix=".libsvm")
                    os.close(_fd)
                    _libsvm_temp_to_remove = Path(_tmp)
                    _libsvm_temp_to_remove.write_text(
                        train_libsvm_p.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    _train_path_for_lgb = _tmp
            else:
                _train_weights = None
            dtrain = lgb.Dataset(
                str(_train_path_for_lgb),
                weight=_train_weights,
                feature_name=list(avail_cols),
            )
            dvalid = lgb.Dataset(
                str(valid_libsvm_p),
                reference=dtrain,
                feature_name=list(avail_cols),
            )
            if STEP9_SAVE_LGB_BINARY:
                try:
                    _max_idx_train = -1
                    _idx_51_cnt = 0
                    _min_idx_train = 10**9
                    with open(_train_path_for_lgb, encoding="utf-8") as _scanf:
                        for _li, _line in enumerate(_scanf):
                            if _li >= 100_000:
                                break
                            _line = _line.strip()
                            if not _line:
                                continue
                            _parts = _line.split()
                            for _tok in _parts[1:]:
                                if ":" not in _tok:
                                    continue
                                try:
                                    _idx = int(_tok.split(":", 1)[0])
                                except ValueError:
                                    continue
                                if _idx > _max_idx_train:
                                    _max_idx_train = _idx
                                if _idx < _min_idx_train:
                                    _min_idx_train = _idx
                                if _idx == 51:
                                    _idx_51_cnt += 1
                    # #region agent log
                    _agent_debug_log(
                        hypothesis_id="H1",
                        location="trainer/training/trainer.py:train_single_rated_model:pre-save-binary-scan",
                        message="Pre-save_binary sampled index stats from train LibSVM",
                        data={
                            "train_path_for_lgb": str(_train_path_for_lgb),
                            "sampled_lines": 100000,
                            "min_feature_index": (None if _max_idx_train < 0 else _min_idx_train),
                            "max_feature_index": (None if _max_idx_train < 0 else _max_idx_train),
                            "index_51_count_in_sample": _idx_51_cnt,
                            "avail_cols_len": len(avail_cols),
                        },
                    )
                    # #endregion
                    dtrain.save_binary(str(_bin_path))
                    logger.info("Plan B+: saved train Dataset to %s", _bin_path)
                except OSError as _e:
                    logger.warning(
                        "Plan B+: failed to save train Dataset to %s (%s); continuing without .bin.",
                        _bin_path,
                        _e,
                    )
                except Exception as _e:
                    # #region agent log
                    _agent_debug_log(
                        hypothesis_id="H3",
                        location="trainer/training/trainer.py:train_single_rated_model:save-binary-exception",
                        message="save_binary raised exception",
                        data={
                            "error_type": type(_e).__name__,
                            "error": str(_e),
                            "bin_path": str(_bin_path),
                            "avail_cols_len": len(avail_cols),
                        },
                    )
                    # #endregion
                    raise
        _default_hp = {
            "n_estimators": 400,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "max_depth": 8,
            "min_child_samples": 20,
        }
        hp_resolved = {**_default_hp, **hp}
        hp_lgb = {
            **_lgb_params_for_pipeline(),
            "learning_rate": hp_resolved["learning_rate"],
            "num_leaves": hp_resolved["num_leaves"],
            "max_depth": hp_resolved["max_depth"],
            "min_child_samples": hp_resolved["min_child_samples"],
        }
        num_boost_round = max(1, int(hp_resolved.get("n_estimators", 400)))
        if _has_val_from_file:
            booster = lgb.train(
                hp_lgb,
                dtrain,
                num_boost_round=num_boost_round,
                valid_sets=[dvalid],
                callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
            )
        else:
            booster = lgb.train(
                hp_lgb,
                dtrain,
                num_boost_round=num_boost_round,
            )
        avail_cols = list(booster.feature_name())
        # PLAN B+ 階段 6: when valid_df not in memory, predict from file path; else in-memory (backward compat).
        _val_rated_eval = _get_val_rated()
        _missing_val_cols = (
            [c for c in avail_cols if c not in _val_rated_eval.columns]
            if not _val_rated_eval.empty
            else []
        )
        if _missing_val_cols:
            val_scores = np.array([], dtype=np.float64)
            _has_val = False
        elif valid_df is None or (valid_df is not None and valid_df.empty):
            # Validation from file: Booster.predict(path) only when path under DATA_DIR and len(y_vl) > 0 (R216 #4, #6)
            if not _valid_path_under_data_dir or len(y_vl) == 0:
                val_scores = np.array([], dtype=np.float64)
                _has_val = False
            else:
                _raw = booster.predict(str(valid_libsvm_p))
                val_scores = np.asarray(_raw).reshape(-1) if np.ndim(_raw) else np.asarray([_raw]).reshape(-1)
                if len(val_scores) != len(y_vl):
                    logger.warning(
                        "Plan B+: valid LibSVM label count (%d) != predict count (%d); trimming to min.",
                        len(y_vl),
                        len(val_scores),
                    )
                    _n = min(len(val_scores), len(y_vl))
                    val_scores = val_scores[:_n]
                    y_vl = y_vl[:_n] if hasattr(y_vl, "__getitem__") else np.asarray(y_vl)[:_n]
                _has_val = _has_val_from_file
        else:
            val_scores = np.asarray(booster.predict(_val_rated_eval[avail_cols])).reshape(-1)
            _has_val = _has_val_from_file
        if _has_val and np.asarray(y_vl).sum() > 0:
            prauc = float(average_precision_score(y_vl, val_scores))
            _pick = pick_threshold_dec026(
                np.asarray(y_vl, dtype=float),
                np.asarray(val_scores, dtype=float),
                recall_floor=THRESHOLD_MIN_RECALL,
                min_alert_count=THRESHOLD_MIN_ALERT_COUNT,
                min_alerts_per_hour=_ft_thr_mah,
                window_hours=_ft_thr_wh,
                fbeta_beta=THRESHOLD_FBETA,
            )
            if _pick.is_fallback:
                best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
                best_fbeta = 0.0
            else:
                best_t = _pick.threshold
                best_prec = _pick.precision
                best_rec = _pick.recall
                best_fbeta = _pick.fbeta
                best_f1 = _pick.f1
        else:
            prauc = 0.0
            best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
            best_fbeta = 0.0
        n_val = int(len(y_vl))
        n_val_pos = int(y_vl.sum())
        val_random_ap = (n_val_pos / n_val) if n_val > 0 else 0.0
        metrics = {
            "label": "rated",
            "val_ap": prauc,
            "val_precision": best_prec,
            "val_recall": best_rec,
            "val_f1": best_f1,
            "val_fbeta_05": best_fbeta,
            "threshold": best_t,
            "val_samples": n_val,
            "val_positives": n_val_pos,
            "val_random_ap": val_random_ap,
            "best_hyperparams": hp_resolved,
            "_uncalibrated": not _has_val,
        }
        if _ft_thr_wh is not None and _ft_thr_mah is not None:
            metrics["val_dec026_pick_window_hours"] = float(_ft_thr_wh)
            metrics["val_dec026_pick_min_alerts_per_hour"] = float(_ft_thr_mah)
        model = _BoosterWrapper(booster)
        if _libsvm_temp_to_remove is not None and _libsvm_temp_to_remove.exists():
            _libsvm_temp_to_remove.unlink()

    if use_from_file:
        # Plan B §4: train from CSV; §5: wrap Booster for scorer/artifact compatibility.
        train_path = DATA_DIR / "export" / "train_for_lgb.csv"
        valid_path = DATA_DIR / "export" / "valid_for_lgb.csv"
        # R188 Review #2: 0-row train CSV => fallback to in-memory (avoid LightGBM "at least one line" error).
        with open(train_path, encoding="utf-8") as _f:
            _n_lines = sum(1 for _ in _f)
        if _n_lines < 2:
            use_from_file = False
            logger.warning(
                "Plan B: train CSV has < 2 lines (header-only or empty); using in-memory training."
            )
        if use_from_file:
            # R188 Review #3: single-class train CSV => fallback (align with R1509 semantics).
            _train_labels = pd.read_csv(train_path, usecols=["label"])
            if _train_labels["label"].nunique() < 2:
                use_from_file = False
                logger.warning(
                    "Plan B: train CSV has only one class; using in-memory training."
                )
        if use_from_file:
            # Load train from CSV so feature set is explicit (avoid weight column as feature in some LightGBM builds).
            _train_csv = pd.read_csv(train_path)
            _train_feature_cols = [c for c in _train_csv.columns if c not in ("label", "weight")]
            dtrain = lgb.Dataset(
                _train_csv[_train_feature_cols],
                label=_train_csv["label"],
                weight=_train_csv["weight"] if "weight" in _train_csv.columns else None,
            )
            # R191 Review #1: run_optuna_search may return {} or partial keys; merge with defaults to avoid KeyError.
            _default_rated_hp = {
                "n_estimators": 400,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "max_depth": 8,
                "min_child_samples": 20,
            }
            hp_resolved = {**_default_rated_hp, **hp}
            hp_lgb = {
                **_lgb_params_for_pipeline(),
                "learning_rate": hp_resolved["learning_rate"],
                "num_leaves": hp_resolved["num_leaves"],
                "max_depth": hp_resolved["max_depth"],
                "min_child_samples": hp_resolved["min_child_samples"],
            }
            # R191 Review #3: ensure at least 1 round (guard 0/negative from Optuna).
            num_boost_round = max(1, int(hp_resolved.get("n_estimators", 400)))
            # R196: align with in-memory path — use in-memory val_rated for early_stopping so parity test passes.
            _val_rated_eval = _get_val_rated()
            _has_val_from_file = (
                not _val_rated_eval.empty
                and len(y_vl) >= MIN_VALID_TEST_ROWS
                and int(y_vl.isna().sum()) == 0
                and int(y_vl.sum()) >= 1
                and int((y_vl == 0).sum()) >= 1
            )
            # R199 Review #1: val_rated must contain all _train_feature_cols (from CSV); else skip early_stopping to avoid KeyError.
            _missing_val_cols = [c for c in _train_feature_cols if c not in _val_rated_eval.columns]
            if _missing_val_cols:
                logger.warning(
                    "Plan B: valid_df missing columns %s present in train CSV; skipping early_stopping for from-file training.",
                    _missing_val_cols,
                )
                _has_val_from_file = False
            dvalid = None
            if _has_val_from_file:
                dvalid = lgb.Dataset(
                    _val_rated_eval[_train_feature_cols],
                    label=_val_rated_eval["label"],
                    reference=dtrain,
                )
                booster = lgb.train(
                    hp_lgb,
                    dtrain,
                    num_boost_round=num_boost_round,
                    valid_sets=[dvalid],
                    callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
                )
            else:
                booster = lgb.train(
                    hp_lgb,
                    dtrain,
                    num_boost_round=num_boost_round,
                )
            # From-file peak-RAM cleanup: once LightGBM has built the Booster, the
            # temporary CSV DataFrame / Dataset objects are no longer needed.
            _train_csv = None
            dtrain = None
            dvalid = None
            gc.collect()
            # R188 Review #1: artifact features must match Booster (common_cols from export).
            avail_cols = list(booster.feature_name())
            # R199 #1: if val_rated is missing any feature column, do not predict (would KeyError).
            if _missing_val_cols:
                val_scores = np.array([], dtype=np.float64)
                _has_val = False
            else:
                val_scores = np.asarray(booster.predict(_val_rated_eval[avail_cols])).reshape(-1)
                _has_val = (
                    not _val_rated_eval.empty
                    and len(y_vl) >= MIN_VALID_TEST_ROWS
                    and int(y_vl.isna().sum()) == 0
                    and int(y_vl.sum()) >= 1
                    and int((y_vl == 0).sum()) >= 1
                )
            if _has_val and y_vl.sum() > 0:
                prauc = float(average_precision_score(y_vl, val_scores))
                _pick = pick_threshold_dec026(
                    np.asarray(y_vl, dtype=float),
                    np.asarray(val_scores, dtype=float),
                    recall_floor=THRESHOLD_MIN_RECALL,
                    min_alert_count=THRESHOLD_MIN_ALERT_COUNT,
                    min_alerts_per_hour=_ft_thr_mah,
                    window_hours=_ft_thr_wh,
                    fbeta_beta=THRESHOLD_FBETA,
                )
                if _pick.is_fallback:
                    best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
                    best_fbeta = 0.0
                else:
                    best_t = _pick.threshold
                    best_prec = _pick.precision
                    best_rec = _pick.recall
                    best_fbeta = _pick.fbeta
                    best_f1 = _pick.f1
            else:
                prauc = 0.0
                best_t, best_f1, best_prec, best_rec = 0.5, 0.0, 0.0, 0.0
                best_fbeta = 0.0
            n_val = int(len(y_vl))
            n_val_pos = int(y_vl.sum())
            val_random_ap = (n_val_pos / n_val) if n_val > 0 else 0.0
            metrics = {
                "label": "rated",
                "val_ap": prauc,
                "val_precision": best_prec,
                "val_recall": best_rec,
                "val_f1": best_f1,
                "val_fbeta_05": best_fbeta,
                "threshold": best_t,
                "val_samples": n_val,
                "val_positives": n_val_pos,
                "val_random_ap": val_random_ap,
                "best_hyperparams": hp_resolved,
                "_uncalibrated": not _has_val,
            }
            if _ft_thr_wh is not None and _ft_thr_mah is not None:
                metrics["val_dec026_pick_window_hours"] = float(_ft_thr_wh)
                metrics["val_dec026_pick_min_alerts_per_hour"] = float(_ft_thr_mah)
            model = _BoosterWrapper(booster)
    if not use_from_file and not use_from_libsvm:
        model, metrics = _train_one_model(
            X_tr,
            y_tr,
            X_vl,
            y_vl,
            sw_rated,
            hp,
            label="rated",
            log_results=False,
            val_dec026_window_hours=_ft_thr_wh,
            val_dec026_min_alerts_per_hour=_ft_thr_mah,
        )

    train_thr = cast(float, metrics["threshold"])
    _train_booster = getattr(model, "booster_", None)
    used_libsvm_train_metrics = False
    if use_from_libsvm and train_libsvm_paths is not None and _train_booster is not None:
        _train_libsvm_p = train_libsvm_paths[0]
        _train_under_data_dir = False
        try:
            _train_libsvm_p.resolve().relative_to(DATA_DIR.resolve())
            _train_under_data_dir = True
        except ValueError:
            pass
        if _train_under_data_dir and _train_libsvm_p.is_file():
            y_tr_file = _labels_from_libsvm(_train_libsvm_p)
            if len(y_tr_file) > 0:
                try:
                    _raw_tr = _train_booster.predict(str(_train_libsvm_p))
                    tr_scores = (
                        np.asarray(_raw_tr).reshape(-1)
                        if np.ndim(_raw_tr)
                        else np.asarray([_raw_tr]).reshape(-1)
                    )
                    if len(tr_scores) != len(y_tr_file):
                        _ntr = min(len(tr_scores), len(y_tr_file))
                        tr_scores = tr_scores[:_ntr]
                        y_tr_file = y_tr_file[:_ntr]
                    train_m = _train_metrics_dict_from_y_scores(
                        y_tr_file,
                        tr_scores,
                        train_thr,
                        label="rated",
                        log_results=False,
                    )
                    used_libsvm_train_metrics = True
                except Exception as exc:
                    logger.warning(
                        "Plan B+: train metrics via LibSVM file failed (%s); "
                        "falling back to batched in-memory predict.",
                        exc,
                    )
    if not used_libsvm_train_metrics:
        _ensure_inmemory_train_views(avail_cols)
        X_tr_pred = _dataframe_for_lgb_predict(model, _get_train_rated(), avail_cols)
        train_m = _compute_train_metrics(
            model,
            train_thr,
            X_tr_pred,
            y_tr,
            label="rated",
            log_results=False,
        )
    metrics.update(train_m)

    test_rated = _get_test_rated()
    if test_rated is not None and not test_rated.empty:
        _missing_test_cols = [c for c in avail_cols if c not in test_rated.columns]
        if _missing_test_cols:
            logger.warning(
                "rated: test_df missing columns %s; skipping test evaluation.",
                _missing_test_cols,
            )
            test_m = {}
        else:
            X_te = _dataframe_for_lgb_predict(model, test_rated, avail_cols)
            y_te = test_rated["label"]
            test_m = _compute_test_metrics(
                model,
                cast(float, metrics["threshold"]),
                X_te,
                y_te,
                label="rated",
                _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                log_results=False,
                production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
            )
            metrics.update(test_m)
    elif (
        use_from_libsvm
        and test_libsvm_path is not None
        and test_libsvm_path.exists()
    ):
        # PLAN B+ 階段 6 第 3 步: test from file (path under DATA_DIR, same contract as valid)
        _test_path_under_data_dir = True
        try:
            test_libsvm_path.resolve().relative_to(DATA_DIR.resolve())
        except ValueError:
            logger.warning(
                "Plan B+: test LibSVM path %s not under DATA_DIR; skipping test from file.",
                test_libsvm_path,
            )
            _test_path_under_data_dir = False
            test_m = {}
        else:
            y_te = _labels_from_libsvm(test_libsvm_path)
            if len(y_te) == 0:
                test_m = {}
            else:
                _test_booster = getattr(model, "booster_", None)
                if _test_booster is None:
                    test_m = {}
                else:
                    _raw = _test_booster.predict(str(test_libsvm_path))
                    test_scores = np.asarray(_raw).reshape(-1) if np.ndim(_raw) else np.asarray([_raw]).reshape(-1)
                    if len(test_scores) != len(y_te):
                        _n = min(len(test_scores), len(y_te))
                        test_scores = test_scores[:_n]
                        y_te = y_te[:_n]
                    test_m = _compute_test_metrics_from_scores(
                        y_te,
                        test_scores,
                        cast(float, metrics["threshold"]),
                        label="rated",
                        _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                        log_results=False,
                        production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                    )
                    metrics.update(test_m)
    else:
        test_m = {}

    # A3 / R3: always compare LGBM / CatBoost / XGBoost on the same rated split matrices.
    if gbm_bakeoff and not _get_train_rated().empty:
        _compare_valid = None
        _compare_test = None
        _x_vl_cmp = None
        _y_vl_cmp = None
        _x_te_cmp = None
        _y_te_cmp = None
        try:
            from trainer.training.gbm_bakeoff import train_and_select_rated_gbm_family

            _compare_valid = _get_val_rated()
            if (
                (_compare_valid is None or _compare_valid.empty)
                and valid_split_parquet_path is not None
                and valid_split_parquet_path.exists()
            ):
                _compare_valid = _load_rated_eval_split_from_parquet(
                    valid_split_parquet_path,
                    avail_cols,
                )
            _compare_test = test_rated
            if (
                (_compare_test is None or _compare_test.empty)
                and test_split_parquet_path is not None
                and test_split_parquet_path.exists()
            ):
                _compare_test = _load_rated_eval_split_from_parquet(
                    test_split_parquet_path,
                    avail_cols,
                )

            _x_vl_cmp = (
                _compare_valid[avail_cols]
                if _compare_valid is not None and not _compare_valid.empty
                else X_vl
            )
            _y_vl_cmp = (
                _compare_valid["label"]
                if _compare_valid is not None and not _compare_valid.empty
                else y_vl
            )
            _x_te_cmp = (
                _compare_test[avail_cols]
                if _compare_test is not None and not _compare_test.empty
                else None
            )
            _y_te_cmp = (
                _compare_test["label"]
                if _compare_test is not None and not _compare_test.empty
                else None
            )

            _winner_backend, _winner_art, _bake_report = train_and_select_rated_gbm_family(
                X_tr,
                y_tr,
                _x_vl_cmp,
                _y_vl_cmp,
                sw_rated,
                hp,
                lightgbm_artifact={
                    "model": model,
                    "threshold": metrics["threshold"],
                    "features": avail_cols,
                    "metrics": metrics,
                },
                run_optuna=bool(run_optuna),
                field_test_constrained_optuna_objective_allowed=_ft_allowed,
                X_test=_x_te_cmp,
                y_test=_y_te_cmp,
                val_dec026_window_hours=_ft_thr_wh,
                val_dec026_min_alerts_per_hour=_ft_thr_mah,
            )
            model = _winner_art["model"]
            metrics = dict(_winner_art["metrics"])
            metrics["gbm_bakeoff"] = _bake_report
            metrics["selected_backend"] = _winner_backend
            metrics["selected_backend_source"] = "a3_gbm_family_compare"
            metrics["model_kind"] = _winner_art.get("model_kind", _winner_backend)
            metrics["reason_codes_enabled"] = bool(
                _winner_art.get("reason_codes_enabled", True)
            )
            if _winner_art.get("component_backends") is not None:
                metrics["component_backends"] = list(_winner_art.get("component_backends") or [])
            train_m = {
                k: metrics[k]
                for k in (
                    "train_ap",
                    "train_precision",
                    "train_recall",
                    "train_f1",
                    "train_samples",
                    "train_positives",
                    "train_random_ap",
                )
                if k in metrics
            }
            test_m = {
                k: metrics[k]
                for k in metrics
                if k.startswith("test_")
                or k in ("test_neg_pos_ratio", "production_neg_pos_ratio_assumed")
            }
        except Exception as _bake_exc:
            logger.warning("gbm_bakeoff failed (non-fatal): %s", _bake_exc)
            metrics["gbm_bakeoff"] = {"schema_version": "a3_v2", "error": str(_bake_exc)}
            metrics["model_backend"] = "lightgbm"
            metrics["model_kind"] = "lightgbm"
            metrics["reason_codes_enabled"] = True
            metrics["selected_backend"] = "lightgbm"
            metrics["selected_backend_source"] = "primary_train_fallback"
        finally:
            # Peak-RAM cleanup: A3 may materialize rated valid/test splits from parquet
            # and comparison matrices purely for bakeoff. Once the winner is chosen,
            # these intermediate objects should not remain resident through A4/artifacts.
            _compare_valid = None
            _compare_test = None
            _x_vl_cmp = None
            _y_vl_cmp = None
            _x_te_cmp = None
            _y_te_cmp = None
            gc.collect()
    else:
        metrics["model_backend"] = "lightgbm"
        metrics["model_kind"] = "lightgbm"
        metrics["reason_codes_enabled"] = True
        metrics["selected_backend"] = "lightgbm"
        metrics["selected_backend_source"] = "primary_train_only"

    # A4 / R4 MVP: two-stage FP detector with product fusion on Stage-1 candidate pool.
    metrics["a4_enabled"] = False
    metrics["a4_fusion_mode"] = validate_fusion_mode(A4_TWO_STAGE_FUSION_MODE)
    if A4_TWO_STAGE_ENABLE_TRAINING and not _get_train_rated().empty:
        _fusion_mode = validate_fusion_mode(A4_TWO_STAGE_FUSION_MODE)
        _stage1_threshold = float(metrics.get("threshold", 0.5))
        _candidate_cutoff = candidate_cutoff_from_threshold(
            _stage1_threshold,
            A4_TWO_STAGE_CANDIDATE_MULTIPLIER,
        )
        _x_tr_s1 = _dataframe_for_lgb_predict(model, _get_train_rated(), avail_cols)
        _s1_tr = _batched_model_positive_class_scores(
            model,
            _x_tr_s1,
            int(max(1, A4_TWO_STAGE_PREDICT_BATCH_ROWS)),
        )
        _cand_mask_tr = candidate_mask_from_scores(_s1_tr, cutoff=_candidate_cutoff)
        _n_cand_tr = int(np.sum(_cand_mask_tr))
        _y2_tr = np.asarray(y_tr, dtype=float).reshape(-1)[_cand_mask_tr]
        _pos2_tr = int(np.sum(_y2_tr == 1))
        _neg2_tr = int(np.sum(_y2_tr == 0))
        _stage2_ready = (
            _n_cand_tr >= int(max(1, A4_TWO_STAGE_MIN_TRAIN_ROWS))
            and _pos2_tr >= int(max(1, A4_TWO_STAGE_MIN_TRAIN_POSITIVES))
            and _neg2_tr >= 1
        )
        metrics["a4_candidate_cutoff"] = float(_candidate_cutoff)
        metrics["a4_candidate_rows_train"] = _n_cand_tr
        metrics["a4_stage2_train_positives"] = _pos2_tr
        metrics["a4_stage2_train_negatives"] = _neg2_tr
        _x2_tr = None
        _x_vl_s1 = None
        _x2_vl = None
        _x_te_s1 = None
        _x2_te = None
        _s2_tr = None
        _s2_vl = None
        _s2_te = None
        _fused_tr = None
        _fused_vl = None
        _fused_te = None
        _a4_train = None
        _a4_valid = None
        _a4_test = None
        _val_rated_eval = None
        _cand_mask_vl = None
        _cand_mask_te = None
        try:
            if _stage2_ready and _fusion_mode == A4_FUSION_MODE_PRODUCT:
                _x2_tr = _x_tr_s1.loc[_cand_mask_tr, :].copy()
                _sw2 = (
                    np.asarray(sw_rated, dtype=float).reshape(-1)[: len(_cand_mask_tr)][_cand_mask_tr]
                    if len(sw_rated) >= len(_cand_mask_tr)
                    else None
                )
                _stage2_hp = {
                    "n_estimators": 200,
                    "learning_rate": 0.05,
                    "num_leaves": 31,
                    "max_depth": 6,
                    "min_child_samples": 20,
                }
                _stage2 = lgb.LGBMClassifier(**_lgb_params_for_pipeline(), **_stage2_hp)
                try:
                    _stage2.fit(
                        _x2_tr,
                        _y2_tr,
                        sample_weight=_sw2 if _sw2 is not None else None,
                    )
                    _s2_tr = np.ones(len(_s1_tr), dtype=np.float64)
                    _s2_tr[_cand_mask_tr] = _batched_model_positive_class_scores(
                        _stage2,
                        _x2_tr,
                        int(max(1, A4_TWO_STAGE_PREDICT_BATCH_ROWS)),
                    )
                    _fused_tr = fuse_product_scores(_s1_tr, _s2_tr)
                    _a4_train = _train_metrics_dict_from_y_scores(
                        y_tr,
                        _fused_tr,
                        _stage1_threshold,
                        label="rated_a4_fused_train",
                        log_results=False,
                    )
                    metrics.update({f"a4_{k}": v for k, v in _a4_train.items()})
                    _val_rated_eval = _get_val_rated()
                    if not _val_rated_eval.empty:
                        _x_vl_s1 = _dataframe_for_lgb_predict(model, _val_rated_eval, avail_cols)
                        _s1_vl = _batched_model_positive_class_scores(
                            model,
                            _x_vl_s1,
                            int(max(1, A4_TWO_STAGE_PREDICT_BATCH_ROWS)),
                        )
                        _cand_mask_vl = candidate_mask_from_scores(_s1_vl, cutoff=_candidate_cutoff)
                        _s2_vl = np.ones(len(_s1_vl), dtype=np.float64)
                        if int(np.sum(_cand_mask_vl)) > 0:
                            _x2_vl = _x_vl_s1.loc[_cand_mask_vl, :]
                            _s2_vl[_cand_mask_vl] = _batched_model_positive_class_scores(
                                _stage2,
                                _x2_vl,
                                int(max(1, A4_TWO_STAGE_PREDICT_BATCH_ROWS)),
                            )
                        _fused_vl = fuse_product_scores(_s1_vl, _s2_vl)
                        _a4_valid = _compute_valid_metrics_from_scores(
                            _val_rated_eval["label"].to_numpy(dtype=float),
                            _fused_vl,
                            _stage1_threshold,
                        )
                        metrics.update({f"a4_{k}": v for k, v in _a4_valid.items()})
                        metrics["a4_candidate_rows_valid"] = int(np.sum(_cand_mask_vl))
                    else:
                        metrics["a4_candidate_rows_valid"] = 0
                    if test_rated is not None and not test_rated.empty:
                        _x_te_s1 = _dataframe_for_lgb_predict(model, test_rated, avail_cols)
                        _s1_te = _batched_model_positive_class_scores(
                            model,
                            _x_te_s1,
                            int(max(1, A4_TWO_STAGE_PREDICT_BATCH_ROWS)),
                        )
                        _cand_mask_te = candidate_mask_from_scores(_s1_te, cutoff=_candidate_cutoff)
                        _s2_te = np.ones(len(_s1_te), dtype=np.float64)
                        if int(np.sum(_cand_mask_te)) > 0:
                            _x2_te = _x_te_s1.loc[_cand_mask_te, :]
                            _s2_te[_cand_mask_te] = _batched_model_positive_class_scores(
                                _stage2,
                                _x2_te,
                                int(max(1, A4_TWO_STAGE_PREDICT_BATCH_ROWS)),
                            )
                        _fused_te = fuse_product_scores(_s1_te, _s2_te)
                        _a4_test = _compute_test_metrics_from_scores(
                            test_rated["label"].to_numpy(dtype=float),
                            _fused_te,
                            _stage1_threshold,
                            label="rated_a4_fused_test",
                            _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                            log_results=False,
                            production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                        )
                        metrics.update({f"a4_{k}": v for k, v in _a4_test.items()})
                        metrics["a4_candidate_rows_test"] = int(np.sum(_cand_mask_te))
                    else:
                        metrics["a4_candidate_rows_test"] = 0
                    metrics["a4_enabled"] = True
                    metrics["a4_fusion_mode"] = _fusion_mode
                    metrics["a4_stage2_model_backend"] = "lightgbm"
                    metrics["a4_stage2_features"] = list(avail_cols)
                    metrics["_a4_stage2_model"] = _stage2
                except Exception as _a4_exc:
                    logger.warning("A4 two-stage training failed (fallback to Stage-1 only): %s", _a4_exc)
                    metrics["a4_enabled"] = False
                    metrics["a4_failure_reason"] = str(_a4_exc)
            else:
                metrics["a4_enabled"] = False
                metrics["a4_failure_reason"] = (
                    "insufficient_stage2_candidate_rows_or_class_balance"
                    if _fusion_mode == A4_FUSION_MODE_PRODUCT
                    else "unsupported_fusion_mode"
                )
        finally:
            # Peak-RAM cleanup: A4 builds several large stage-1 / stage-2 matrices and
            # score arrays; once the derived metrics are recorded, they are no longer
            # needed and should not remain resident through artifact save / MLflow.
            _x_tr_s1 = None
            _s1_tr = None
            _cand_mask_tr = None
            _y2_tr = None
            _x2_tr = None
            _x_vl_s1 = None
            _x2_vl = None
            _x_te_s1 = None
            _x2_te = None
            _s2_tr = None
            _s2_vl = None
            _s2_te = None
            _fused_tr = None
            _fused_vl = None
            _fused_te = None
            _a4_train = None
            _a4_valid = None
            _a4_test = None
            _val_rated_eval = None
            _cand_mask_vl = None
            _cand_mask_te = None
            gc.collect()

    # Log in order: train → valid → test (clear labels; valid was previously unlabeled).
    logger.info(
        "rated train: AP=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  random_ap=%.4f",
        train_m.get("train_ap", 0.0),
        train_m.get("train_f1", 0.0),
        train_m.get("train_precision", 0.0),
        train_m.get("train_recall", 0.0),
        train_m.get("train_random_ap", 0.0),
    )
    logger.info(
        "rated valid: AP=%.4f  F0.5=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f",
        metrics.get("val_ap", 0.0),
        metrics.get("val_fbeta_05", 0.0),
        metrics.get("val_f1", 0.0),
        metrics.get("val_precision", 0.0),
        metrics.get("val_recall", 0.0),
        metrics.get("threshold", 0.5),
    )
    if test_m:
        _adj = test_m.get("test_precision_prod_adjusted")
        _adj_str = f"  prec_prod_adj={_adj:.4f}" if _adj is not None else ""
        logger.info(
            "rated test:  AP=%.4f  F1=%.4f  prec=%.4f  rec=%.4f  thr=%.4f%s",
            test_m.get("test_ap", 0.0),
            test_m.get("test_f1", 0.0),
            test_m.get("test_precision", 0.0),
            test_m.get("test_recall", 0.0),
            metrics.get("threshold", 0.5),
            _adj_str,
        )
        _par_parts = []
        for _r in (0.01, 0.1, 0.5):
            _par_val = test_m.get(f"test_precision_at_recall_{_r}")
            _par_parts.append(
                f"prec@rec{_r}={_par_val:.4f}" if _par_val is not None else f"prec@rec{_r}=N/A"
            )
        logger.info("rated test PR-curve: %s", "  ".join(_par_parts))

    metrics["lightgbm_device_requested"] = _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS
    metrics["lightgbm_device_type"] = _EFFECTIVE_LIGHTGBM_DEVICE
    metrics["lightgbm_device_fallback"] = bool(_LIGHTGBM_GPU_FALLBACK_USED)

    if _optuna_hpo_manifest:
        metrics.update(_optuna_hpo_manifest[0])

    if _ft_pre_doc is not None and _ft_pre_path_raw:
        metrics.update(
            training_metrics_overlay_from_precondition(
                _ft_pre_doc, source_path=_ft_pre_path_raw
            )
        )

    if "feature_importance" not in metrics:
        metrics["feature_importance"] = _compute_feature_importance(model, avail_cols)
    if "importance_method" not in metrics:
        metrics["importance_method"] = "gain"
    metrics.update(ranking_meta_pre)
    if ranking_meta_hnm:
        metrics.update(ranking_meta_hnm)

    _a4_stage2_model = metrics.pop("_a4_stage2_model", None)
    rated_art = {
        "model": model,
        "threshold": metrics["threshold"],
        "features": avail_cols,
        "metrics": metrics,
        "model_kind": metrics.get("model_kind", metrics.get("model_backend")),
        "reason_codes_enabled": bool(metrics.get("reason_codes_enabled", True)),
        "component_backends": list(metrics.get("component_backends") or []),
        "a4_enabled": bool(metrics.get("a4_enabled", False)),
        "a4_fusion_mode": metrics.get("a4_fusion_mode", A4_FUSION_MODE_PRODUCT),
        "a4_candidate_cutoff": metrics.get("a4_candidate_cutoff"),
        "stage2_model": _a4_stage2_model,
        "stage2_features": list(metrics.get("a4_stage2_features") or avail_cols),
    }
    return rated_art, None, {"rated": metrics}


# ---------------------------------------------------------------------------
# Model bundle metadata (train/valid/test time bounds + run params)
# ---------------------------------------------------------------------------


def _payout_bounds_iso_from_series(series: pd.Series) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(min_iso, max_iso)`` for ``payout_complete_dtm``-like *series*; empty → (None, None)."""
    if series is None or len(series) == 0:
        return (None, None)
    ts = pd.to_datetime(series, errors="coerce")
    if bool(ts.isna().all()):
        return (None, None)
    ts_naive = ts.dt.tz_localize(None) if getattr(ts.dt, "tz", None) is not None else ts
    mn = ts_naive.min()
    mx = ts_naive.max()
    if pd.isna(mn) or pd.isna(mx):
        return (None, None)
    return (str(pd.Timestamp(mn).isoformat()), str(pd.Timestamp(mx).isoformat()))


def _load_rated_eval_split_from_parquet(
    split_path: Path,
    feature_cols: List[str],
) -> pd.DataFrame:
    """Load a minimal rated eval split from parquet for A3 family comparison.

    This keeps Plan B / B+ semantics for the main LightGBM training path, while still
    giving CatBoost / XGBoost the exact same time split and feature matrix for a fair
    comparison on the selected columns.
    """
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(split_path)
    available = set(pf.schema.names)
    cols = [c for c in feature_cols if c in available]
    for extra in ("label", "is_rated"):
        if extra in available and extra not in cols:
            cols.append(extra)
    if "label" not in cols:
        raise ValueError(f"A3 compare split missing label column: {split_path}")
    df = pd.read_parquet(split_path, columns=cols)
    if "is_rated" in df.columns:
        df = df[df["is_rated"]].copy()
    for col in feature_cols:
        if col not in df.columns:
            df[col] = np.nan
    return df


def _one_split_block_from_dataframe(df: Optional[pd.DataFrame]) -> dict[str, Any]:
    """Build one split summary dict from an in-memory DataFrame (may be empty)."""
    if df is None or df.empty:
        return {
            "start": None,
            "end": None,
            "rows": 0,
            "positives": 0,
            "negatives": 0,
        }
    if "payout_complete_dtm" not in df.columns:
        return {
            "start": None,
            "end": None,
            "rows": int(len(df)),
            "positives": 0,
            "negatives": int(len(df)),
        }
    start_iso, end_iso = _payout_bounds_iso_from_series(df["payout_complete_dtm"])
    n = int(len(df))
    if "label" in df.columns:
        pos = int(pd.to_numeric(df["label"], errors="coerce").fillna(0).astype(int).sum())
        pos = max(0, min(n, pos))
        neg = n - pos
    else:
        pos, neg = 0, n
    return {
        "start": start_iso,
        "end": end_iso,
        "rows": n,
        "positives": pos,
        "negatives": neg,
    }


def split_row_metadata_from_dataframes(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    test_df: pd.DataFrame,
) -> dict[str, dict[str, Any]]:
    """Row-level split summaries from in-memory train/valid/test frames."""
    return {
        "train": _one_split_block_from_dataframe(train_df),
        "valid": _one_split_block_from_dataframe(valid_df),
        "test": _one_split_block_from_dataframe(test_df),
    }


def split_row_metadata_from_parquet_paths(
    train_path: Path,
    valid_path: Path,
    test_path: Path,
) -> dict[str, dict[str, Any]]:
    """Row-level split summaries via DuckDB aggregates (no full-frame load)."""
    import duckdb

    def _q_one(p: Path) -> dict[str, Any]:
        s = str(p).replace("'", "''")
        con = duckdb.connect(":memory:")
        try:
            row = con.execute(
                f"SELECT count(*) AS n, "
                f"coalesce(sum(cast(label AS INTEGER)), 0) AS pos, "
                f"min(payout_complete_dtm) AS dt_min, "
                f"max(payout_complete_dtm) AS dt_max "
                f"FROM read_parquet('{s}')"
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return {"start": None, "end": None, "rows": 0, "positives": 0, "negatives": 0}
        n = int(row[0]) if row[0] is not None else 0
        pos = int(row[1]) if row[1] is not None else 0
        pos = max(0, min(n, pos))
        neg = n - pos
        dt_min = row[2]
        dt_max = row[3]
        start_iso = str(pd.Timestamp(dt_min).isoformat()) if dt_min is not None else None
        end_iso = str(pd.Timestamp(dt_max).isoformat()) if dt_max is not None else None
        return {
            "start": start_iso,
            "end": end_iso,
            "rows": n,
            "positives": pos,
            "negatives": neg,
        }

    return {
        "train": _q_one(train_path),
        "valid": _q_one(valid_path),
        "test": _q_one(test_path),
    }


def split_row_metadata_to_mlflow_string_params(
    splits: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Flatten split ``start``/``end`` into MLflow string params (length-capped)."""
    _max = 200
    out: dict[str, str] = {}
    for split_name in ("train", "valid", "test"):
        block = splits.get(split_name) or {}
        for k in ("start", "end"):
            v = block.get(k)
            if v is None:
                continue
            key = f"split_{split_name}_{k}"
            s = str(v)
            out[key] = s if len(s) <= _max else s[:_max]
    return out


def _git_commit_short_or_nogit() -> str:
    """Return ``git rev-parse --short HEAD`` or ``\"nogit\"`` (same semantics as provenance)."""
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=BASE_DIR,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "nogit"


def build_model_metadata_document(
    *,
    model_version: str,
    effective_start: Any,
    effective_end: Any,
    splits: dict[str, dict[str, Any]],
    use_local_parquet: bool,
    recent_chunks: Optional[int],
    sample_rated_n: Optional[int],
    skip_optuna: bool,
    neg_sample_frac_effective: float,
    bundle_dir: Path,
    combined_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Assemble ``model_metadata.json`` payload (versioned schema v1)."""
    def _iso_any(x: Any) -> Any:
        if x is None:
            return None
        if hasattr(x, "isoformat"):
            return x.isoformat()
        return str(x)

    _test_frac = max(0.0, 1.0 - float(TRAIN_SPLIT_FRAC) - float(VALID_SPLIT_FRAC))
    _rated = (combined_metrics or {}).get("rated") if isinstance(combined_metrics, dict) else None
    _rated_d = _rated if isinstance(_rated, dict) else {}
    return {
        "schema_version": "v1",
        "model_version": model_version,
        "git_commit": _git_commit_short_or_nogit(),
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_source": {
            "type": "local_parquet" if use_local_parquet else "clickhouse",
            "use_local_parquet": bool(use_local_parquet),
            "recent_chunks": recent_chunks,
        },
        "global_window": {
            "start": _iso_any(effective_start),
            "end": _iso_any(effective_end),
        },
        "split_method": {
            "type": "temporal_row_frac_sorted_by_payout_complete_dtm",
            "train_frac": float(TRAIN_SPLIT_FRAC),
            "valid_frac": float(VALID_SPLIT_FRAC),
            "test_frac": float(_test_frac),
        },
        "splits": splits,
        "training_params": {
            "skip_optuna": bool(skip_optuna),
            "sample_rated_n": sample_rated_n,
            "neg_sample_frac_effective": float(neg_sample_frac_effective),
            "threshold_min_recall": THRESHOLD_MIN_RECALL,
            "threshold_min_alert_count": int(THRESHOLD_MIN_ALERT_COUNT),
            # A2 / DEC-044: echo rated training recipe (same key as training_metrics.json rated block).
            "ranking_recipe": _rated_d.get("ranking_recipe"),
            "lightgbm_device_requested": _rated_d.get("lightgbm_device_requested"),
            "lightgbm_device_effective": _rated_d.get("lightgbm_device_type"),
            "lightgbm_device_fallback": _rated_d.get("lightgbm_device_fallback"),
            "gbm_bakeoff_enabled": bool(isinstance(_rated_d.get("gbm_bakeoff"), dict)),
            "gbm_bakeoff_winner_backend": (
                (_rated_d.get("gbm_bakeoff") or {}).get("winner_backend")
                if isinstance(_rated_d.get("gbm_bakeoff"), dict)
                else None
            ),
            "gbm_bakeoff_candidate_backends": (
                list(((_rated_d.get("gbm_bakeoff") or {}).get("per_backend") or {}).keys())
                if isinstance(_rated_d.get("gbm_bakeoff"), dict)
                else []
            ),
            "model_backend": _rated_d.get("model_backend"),
            "model_kind": _rated_d.get("model_kind"),
            "reason_codes_enabled": _rated_d.get("reason_codes_enabled"),
            "component_backends": _rated_d.get("component_backends"),
            "selected_backend": _rated_d.get("selected_backend"),
            "selected_backend_source": _rated_d.get("selected_backend_source"),
            "a4_enabled": bool(_rated_d.get("a4_enabled", False)),
            "a4_fusion_mode": _rated_d.get("a4_fusion_mode"),
            "a4_candidate_cutoff": _rated_d.get("a4_candidate_cutoff"),
            "a4_candidate_rows_train": _rated_d.get("a4_candidate_rows_train"),
            "a4_candidate_rows_valid": _rated_d.get("a4_candidate_rows_valid"),
            "a4_candidate_rows_test": _rated_d.get("a4_candidate_rows_test"),
        },
        "artifacts": {
            "bundle_dir": str(bundle_dir.resolve()),
            "training_metrics_path": str((bundle_dir / "training_metrics.json").resolve()),
            "pipeline_diagnostics_path": str((bundle_dir / "pipeline_diagnostics.json").resolve()),
            "model_metadata_path": str((bundle_dir / "model_metadata.json").resolve()),
        },
    }


# ---------------------------------------------------------------------------
# Artifact bundle
# ---------------------------------------------------------------------------

def _log_training_provenance_to_mlflow(
    model_version: str,
    artifact_dir: str,
    training_window_start: Union[datetime, str],
    training_window_end: Union[datetime, str],
    feature_spec_path: str,
    training_metrics_path: str,
    git_commit: Optional[str] = None,
    pipeline_diagnostics_path: Optional[str] = None,
    pipeline_diagnostics_rel_path: Optional[str] = None,
    model_metadata_path: Optional[str] = None,
    model_metadata_rel_path: Optional[str] = None,
    split_boundary_params: Optional[dict[str, str]] = None,
) -> None:
    """Phase 2 T2: Log training provenance to MLflow (no-op when URI unset/unreachable).

    See doc/phase2_provenance_schema.md for key names. On failure (no URI, network
    error), logs warning only; training is still considered successful.

    ``pipeline_diagnostics_*`` default from ``artifact_dir`` when omitted (same directory
    as ``training_metrics.json`` convention). Provenance may run before the diagnostics
    file is written; paths still denote the canonical bundle location.

    Optional ``model_metadata_*`` and ``split_boundary_params`` extend the Phase 2 schema
    with per-split time bounds (string params) and ``model_metadata.json`` paths.
    """
    if git_commit is None:
        try:
            git_commit = (
                subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=BASE_DIR,
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
        except Exception:
            git_commit = "nogit"
    _start = training_window_start.isoformat() if hasattr(training_window_start, "isoformat") else str(training_window_start)
    _end = training_window_end.isoformat() if hasattr(training_window_end, "isoformat") else str(training_window_end)
    _artifact = Path(artifact_dir)
    _pd_path = pipeline_diagnostics_path
    if _pd_path is None:
        _pd_path = str(_artifact / "pipeline_diagnostics.json")
    _pd_rel = pipeline_diagnostics_rel_path
    if _pd_rel is None:
        _pd_rel = f"{_artifact.name}/pipeline_diagnostics.json"
    params = {
        "model_version": model_version,
        "git_commit": git_commit,
        "training_window_start": _start,
        "training_window_end": _end,
        "artifact_dir": artifact_dir,
        "feature_spec_path": feature_spec_path,
        "training_metrics_path": training_metrics_path,
        "pipeline_diagnostics_path": _pd_path,
        "pipeline_diagnostics_rel_path": _pd_rel,
    }
    if model_metadata_path:
        params["model_metadata_path"] = model_metadata_path
    if model_metadata_rel_path:
        params["model_metadata_rel_path"] = model_metadata_rel_path
    if split_boundary_params:
        params.update(split_boundary_params)
    # T12: if pipeline already started a run (e.g. at pipeline entry), log to it; else start one.
    if has_active_run():
        log_params_safe(params)
    else:
        with safe_start_run(run_name=model_version):
            log_params_safe(params)


def _sha256_file_hex(path: Path, chunk_bytes: int = 1 << 20) -> str:
    """Return lowercase hex SHA-256 of the file at *path* (streaming, bounded chunk size)."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _make_baseline_training_alignment_payload(
    effective_start: Any,
    effective_end: Any,
    train_split_frac: float,
    valid_split_frac_of_total: float,
) -> dict[str, Any]:
    """供 ``baseline_data_alignment``／``training_provenance.json``：與 baseline 契約對齊。"""
    def _iso(x: Any) -> Any:
        if x is None:
            return None
        if hasattr(x, "isoformat"):
            return x.isoformat()
        return str(x)

    den = 1.0 - float(train_split_frac)
    baseline_valid = (
        float(valid_split_frac_of_total) / den if den > 1e-15 else 0.5
    )
    return {
        "data_window": {
            "start": _iso(effective_start),
            "end": _iso(effective_end),
        },
        "split": {
            "train_frac": float(train_split_frac),
            "valid_frac": float(baseline_valid),
        },
        "_trainer_split_row_fractions": {
            "TRAIN_SPLIT_FRAC": float(train_split_frac),
            "VALID_SPLIT_FRAC": float(valid_split_frac_of_total),
            "note": "Baseline valid_frac = VALID_SPLIT_FRAC / (1 - TRAIN_SPLIT_FRAC).",
        },
    }


def save_artifact_bundle(
    rated: Optional[dict],
    feature_cols: List[str],
    combined_metrics: dict,
    model_version: str,
    sample_rated_n: Optional[int] = None,
    feature_spec_path: Optional[Path] = None,
    neg_sample_frac: float = 1.0,
    bundle_dir: Optional[Path] = None,
    baseline_training_alignment: Optional[dict[str, Any]] = None,
    model_metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Write all model artifacts atomically (v10 rated artifact entry, DEC-021).

    When *bundle_dir* is set, artifacts are written there; otherwise :data:`MODEL_DIR`
    (typically ``out/models``). Versioned training uses
    ``out/models/<model_version>/`` (see Priority 1 investigation plan).

    v10 single-entry format
    ----------------------
    models/model.pkl               {"model", "threshold", "features", "model_kind", ...}
    models/feature_list.json       [{name, track}]
    models/reason_code_map.json   {feature_name: reason_code} for scorer SHAP lookup
    models/model_version          <version string>
    models/training_metrics.json  legacy v1 per-model metrics (rated only)
    models/training_metrics.v2.json  v2 metrics (datasets + selection; large blobs split out)
    models/feature_importance.json  winner feature importance (gain list)
    models/comparison_metrics.json  comparison families (e.g. gbm_bakeoff)
    models/feature_spec.yaml      frozen feature spec snapshot (DEC-024, R3501)
    models/model_metadata.json    train/valid/test time bounds + run params (schema v1)
    """
    _out: Path = bundle_dir if bundle_dir is not None else MODEL_DIR
    _out.mkdir(parents=True, exist_ok=True)
    # DEC-024 / R3501: freeze a copy of the feature spec into the artifact bundle so
    # the scorer can load an exact match to training-time spec_hash for reproducibility.
    spec_hash: Optional[str] = None
    feature_spec: Optional[dict] = None
    _fsp = Path(feature_spec_path) if feature_spec_path is not None else FEATURE_SPEC_PATH
    if _fsp.exists():
        import shutil as _shutil
        _shutil.copy2(_fsp, _out / "feature_spec.yaml")
        spec_hash = hashlib.md5(_fsp.read_bytes()).hexdigest()[:12]
        feature_spec = load_feature_spec(_fsp)
    # v10 single-entry format (DEC-021 / ensemble-capable): one model.pkl only
    if rated:
        _pkl_path = _out / "model.pkl"
        _tmp = _pkl_path.with_suffix(".pkl.tmp")
        joblib.dump(
            {
                "model": rated["model"],
                "threshold": rated["threshold"],
                "features": rated["features"],
                "model_kind": rated.get("model_kind", "lightgbm"),
                "reason_codes_enabled": bool(rated.get("reason_codes_enabled", True)),
                "component_backends": list(rated.get("component_backends") or []),
                "a4_enabled": bool(rated.get("a4_enabled", False)),
                "a4_fusion_mode": rated.get("a4_fusion_mode", A4_FUSION_MODE_PRODUCT),
                "a4_candidate_cutoff": rated.get("a4_candidate_cutoff"),
                "stage2_model": rated.get("stage2_model"),
                "stage2_features": list(rated.get("stage2_features") or rated.get("features") or []),
            },
            _tmp,
        )
        os.replace(_tmp, _pkl_path)

    _profile_set = set(get_candidate_feature_ids(feature_spec, "track_profile", screening_only=False)) if feature_spec else set(PROFILE_FEATURE_COLS)
    _llm_set = set(get_candidate_feature_ids(feature_spec, "track_llm", screening_only=False)) if feature_spec else set()
    _human_set = set(get_candidate_feature_ids(feature_spec, "track_human", screening_only=False)) if feature_spec else set()

    feature_list = [
        {
            "name": c,
            "track": (
                "track_profile" if c in _profile_set
                else "track_human" if c in _human_set
                else "track_llm"
            ),
        }
        for c in feature_cols
    ]
    (_out / "feature_list.json").write_text(
        json.dumps(feature_list, indent=2), encoding="utf-8"
    )

    # reason_code_map.json: feature name -> short reason code for SHAP output.
    # Generated from feature_spec (DEC-024 / TRN-XX).
    reason_code_map: dict[str, str] = {}
    if feature_spec is not None:
        for track in ["track_llm", "track_human", "track_profile"]:
            for c in feature_spec.get(track, {}).get("candidates", []):
                fid = c.get("feature_id")
                rcode = c.get("reason_code_category")
                if fid and rcode:
                    reason_code_map[fid] = rcode

    # Fallback for any missing code
    for feat in feature_cols:
        if feat not in reason_code_map:
            if feat in PROFILE_FEATURE_COLS:
                reason_code_map[feat] = f"PROFILE_{feat[:28].upper()}"
            else:
                reason_code_map[feat] = f"FEAT_{feat[:30].upper()}"

    (_out / "reason_code_map.json").write_text(
        json.dumps(reason_code_map, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    (_out / "model_version").write_text(model_version, encoding="utf-8")
    # R703: flag when the fallback (uncalibrated) 0.5 threshold was used.
    # R804: read from the _uncalibrated code-path flag set by _train_one_model,
    # not from `threshold == 0.5` — a legitimately-optimised threshold of 0.5
    # must not be falsely flagged as uncalibrated.
    # R2207: _uncalibrated is stored inside rated["metrics"], not at the top level.
    # v10 rated artifact entry: only rated threshold is relevant; nonrated removed (R1606/R1908).
    _uncalibrated_threshold = {
        "rated": rated is not None and bool(
            rated["metrics"].get("_uncalibrated", False)
            if isinstance(rated.get("metrics"), dict)
            else rated.get("_uncalibrated", False)
        ),
    }
    _metrics_root: dict[str, Any] = {
        **combined_metrics,
        "model_version": model_version,
        # R301: record sampling metadata so artifacts can be audited
        # even when loaded later.  None = full rated population was used.
        "sample_rated_n": sample_rated_n,
        # R-NEG-2: record effective neg_sample_frac for auditability.
        # 1.0 = no downsampling; < 1.0 = negatives were downsampled.
        "neg_sample_frac": neg_sample_frac,
        # Production neg/pos ratio assumed for test_precision_prod_adjusted.
        # None = feature disabled (PRODUCTION_NEG_POS_RATIO not set in config).
        "production_neg_pos_ratio": PRODUCTION_NEG_POS_RATIO,
        # W2: operating contract for threshold objective / prod-adjusted semantics.
        "selection_mode": str(SELECTION_MODE or "field_test").strip() or "field_test",
        # R703: uncalibrated_threshold=True means the 0.5 fallback was used.
        "uncalibrated_threshold": _uncalibrated_threshold,
        # DEC-032 / PLAN: artifact threshold is chosen at this recall floor (vs multi-recall backtester keys).
        "threshold_selected_at_recall_floor": THRESHOLD_MIN_RECALL,
        # DEC-024 / R3501: SHA-256 prefix of the frozen feature spec for audit.
        "spec_hash": spec_hash,
    }
    if baseline_training_alignment is not None:
        _metrics_root["baseline_data_alignment"] = baseline_training_alignment
        (_out / "training_provenance.json").write_text(
            json.dumps(baseline_training_alignment, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    (_out / "training_metrics.json").write_text(
        json.dumps(
            _metrics_root,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    write_training_metrics_v2_sidecars(
        _out,
        model_version=model_version,
        metrics_root=_metrics_root,
        model_metadata=model_metadata,
    )
    if model_metadata is not None:
        (_out / "model_metadata.json").write_text(
            json.dumps(model_metadata, indent=2, default=str, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # Contract: precision uplift phase2 orchestrator regex-parses this line
    # (``investigations/precision_uplift_recall_1pct/orchestrator/runner.py``).
    logger.info("Artifacts saved to %s  (version=%s)", _out, model_version)


def _write_pipeline_diagnostics_json(
    *,
    model_version: str,
    pipeline_started_at: str,
    pipeline_finished_at: str,
    total_duration_sec: float,
    step1_duration_sec: Optional[float] = None,
    step2_duration_sec: Optional[float] = None,
    step3_duration_sec: Optional[float] = None,
    step4_duration_sec: Optional[float] = None,
    step5_duration_sec: Optional[float] = None,
    step6_duration_sec: Optional[float] = None,
    step7_duration_sec: Optional[float] = None,
    step8_duration_sec: Optional[float] = None,
    step9_duration_sec: Optional[float] = None,
    step10_duration_sec: Optional[float] = None,
    oom_precheck_est_peak_ram_gb: Optional[float] = None,
    oom_precheck_step7_rss_error_ratio: Optional[float] = None,
    step7_rss_start_gb: Optional[float] = None,
    step7_rss_peak_gb: Optional[float] = None,
    step7_rss_end_gb: Optional[float] = None,
    step7_sys_available_min_gb: Optional[float] = None,
    step7_sys_used_percent_peak: Optional[float] = None,
    step7_chunk_parquet_total_bytes: Optional[int] = None,
    step7_chunk_parquet_est_ram_gb: Optional[float] = None,
    step8_screening_source: Optional[str] = None,
    step8_screening_stats_source: Optional[str] = None,
    step8_screening_sample_rows: Optional[int] = None,
    step8_screening_full_train_rows: Optional[int] = None,
    step8_screening_candidate_cols: Optional[int] = None,
    step8_screened_feature_count: Optional[int] = None,
    chunk_cache_stats: Optional[Dict[str, int]] = None,
    output_dir: Optional[Path] = None,
) -> None:
    """Write resource/timing diagnostics to ``output_dir/pipeline_diagnostics.json`` (omit None keys).

    *output_dir* defaults to :data:`MODEL_DIR` when omitted.

    See doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md — RSS/OOM fields align with
    run_pipeline sampling and oom_precheck estimate, not OOM helper return values.

    ``chunk_cache_stats``: optional Step 6 cache counters from :func:`process_chunk`
    (keys ``step6_chunk_cache_*``) for Task 7 DoD / hit-ratio analysis.
    """
    payload: dict[str, Any] = {
        "model_version": model_version,
        "pipeline_started_at": pipeline_started_at,
        "pipeline_finished_at": pipeline_finished_at,
        "total_duration_sec": total_duration_sec,
        "step1_duration_sec": step1_duration_sec,
        "step2_duration_sec": step2_duration_sec,
        "step3_duration_sec": step3_duration_sec,
        "step4_duration_sec": step4_duration_sec,
        "step5_duration_sec": step5_duration_sec,
        "step6_duration_sec": step6_duration_sec,
        "step7_duration_sec": step7_duration_sec,
        "step8_duration_sec": step8_duration_sec,
        "step9_duration_sec": step9_duration_sec,
        "step10_duration_sec": step10_duration_sec,
        "oom_precheck_est_peak_ram_gb": oom_precheck_est_peak_ram_gb,
        "oom_precheck_step7_rss_error_ratio": oom_precheck_step7_rss_error_ratio,
        "step7_rss_start_gb": step7_rss_start_gb,
        "step7_rss_peak_gb": step7_rss_peak_gb,
        "step7_rss_end_gb": step7_rss_end_gb,
        "step7_sys_available_min_gb": step7_sys_available_min_gb,
        "step7_sys_used_percent_peak": step7_sys_used_percent_peak,
        "step7_chunk_parquet_total_bytes": step7_chunk_parquet_total_bytes,
        "step7_chunk_parquet_est_ram_gb": step7_chunk_parquet_est_ram_gb,
        "step8_screening_source": step8_screening_source,
        "step8_screening_stats_source": step8_screening_stats_source,
        "step8_screening_sample_rows": step8_screening_sample_rows,
        "step8_screening_full_train_rows": step8_screening_full_train_rows,
        "step8_screening_candidate_cols": step8_screening_candidate_cols,
        "step8_screened_feature_count": step8_screened_feature_count,
    }
    out = {k: v for k, v in payload.items() if v is not None}
    if chunk_cache_stats:
        for _ck, _cv in chunk_cache_stats.items():
            if _cv is not None:
                out[_ck] = _cv
    _dir = output_dir if output_dir is not None else MODEL_DIR
    (_dir / "pipeline_diagnostics.json").write_text(
        json.dumps(out, indent=2, default=str),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main training pipeline
# ---------------------------------------------------------------------------

def run_pipeline(args) -> None:
    """Phase-1 training pipeline entry point."""
    logger.info(
        "DBG bc1669: run_pipeline entry reached (pid=%s cwd=%s days=%s local=%s)",
        os.getpid(),
        os.getcwd(),
        getattr(args, "days", None),
        bool(getattr(args, "use_local_parquet", False)),
    )
    # #region agent log
    _agent_debug_log(
        hypothesis_id="H5",
        location="trainer/training/trainer.py:run_pipeline:entry",
        message="run_pipeline entry instrumentation reached",
        data={
            "days": getattr(args, "days", None),
            "use_local_parquet": bool(getattr(args, "use_local_parquet", False)),
            "pid": os.getpid(),
        },
    )
    # #endregion
    pipeline_start = time.perf_counter()
    pipeline_started_at_iso = datetime.now(timezone.utc).isoformat()
    start, end = parse_window(args)
    use_local = getattr(args, "use_local_parquet", False)
    force = getattr(args, "force_recompute", False)
    skip_optuna = getattr(args, "skip_optuna", False)
    pipeline_ranking_recipe = resolve_ranking_recipe(getattr(args, "ranking_recipe", None))
    logger.info("Precision uplift A2 ranking_recipe=%s", pipeline_ranking_recipe)
    pipeline_gbm_bakeoff = bool(getattr(_cfg, "STEP9_COMPARE_ALL_GBMS", True)) and not bool(
        getattr(args, "no_gbm_bakeoff", False)
    )
    logger.info("Precision uplift A3 gbm_bakeoff_enabled=%s", pipeline_gbm_bakeoff)
    configure_lightgbm_device_for_run(args)
    # --no-preload: disable session full-table preload; use per-day PyArrow
    # pushdown reads instead.  Reduces peak RAM for low-RAM machines.
    no_preload = getattr(args, "no_preload", False)
    # --sample-rated N: restrict training to a deterministic subset of rated patrons.
    # None means "use all rated canonical_ids" (default).
    sample_rated_n: Optional[int] = getattr(args, "sample_rated", None)
    # R302: reject invalid sampling sizes early with an actionable error.
    if sample_rated_n is not None and sample_rated_n < 1:
        raise SystemExit(
            f"--sample-rated N must be >= 1, got {sample_rated_n}. "
            "Pass a positive integer or omit the flag to use all rated patrons."
        )

    # Log the config-file NEG_SAMPLE_FRAC at startup.  The OOM pre-check (run
    # after Step 1) may further lower this to _effective_neg_sample_frac.
    if NEG_SAMPLE_FRAC < 1.0:
        print(
            f"[Config] NEG_SAMPLE_FRAC={NEG_SAMPLE_FRAC:.2f}: "
            f"negatives will be downsampled to {NEG_SAMPLE_FRAC * 100:.0f}% per chunk "
            f"(OOM mitigation — positives always kept in full)",
            flush=True,
        )
        logger.info(
            "NEG_SAMPLE_FRAC=%.2f (config): negatives downsampled per chunk (OOM mitigation)",
            NEG_SAMPLE_FRAC,
        )
    else:
        logger.info("NEG_SAMPLE_FRAC=1.0 (config): negative downsampling disabled (all rows kept)")

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

    # T12: one MLflow run for the whole pipeline; on failure log status=FAILED and re-raise.
    _mlflow_run_name = f"train-{start.date()}-{end.date()}-{int(time.time())}"
    with safe_start_run(
        experiment_name=MLFLOW_EXPERIMENT_TRAIN,
        run_name=_mlflow_run_name,
    ):
        try:
            # T12.2 / T-PipelineStepDurations: best-effort per-step wall times (Step 1–10).
            # Note: all values are optional; log_*_safe helpers will skip None.
            chunks: list = []
            recent_chunks: Optional[int] = getattr(args, "recent_chunks", None)
            effective_start = start
            effective_end = end
            _effective_neg_sample_frac: float = NEG_SAMPLE_FRAC
            step1_duration_sec: Optional[float] = None
            step2_duration_sec: Optional[float] = None
            step3_duration_sec: Optional[float] = None
            step4_duration_sec: Optional[float] = None
            step5_duration_sec: Optional[float] = None
            step6_duration_sec: Optional[float] = None
            step7_duration_sec: Optional[float] = None
            step8_duration_sec: Optional[float] = None
            step9_duration_sec: Optional[float] = None
            step10_duration_sec: Optional[float] = None
            # OOM pre-check estimate (Step 1) and post-check RSS peak (Step 7-9 checkpoint).
            oom_precheck_est_peak_ram_gb: Optional[float] = None
            oom_precheck_step7_rss_error_ratio: Optional[float] = None
            # Process RSS (peak := max(start,end)) and system RAM min/max across Step 7-9.
            step7_rss_start_gb: Optional[float] = None
            step7_rss_end_gb: Optional[float] = None
            step7_rss_peak_gb: Optional[float] = None
            step7_sys_available_min_gb: Optional[float] = None
            step7_sys_used_percent_peak: Optional[float] = None
            _step7_sys_available_start_gb: Optional[float] = None
            _step7_sys_used_percent_start: Optional[float] = None
            step7_chunk_parquet_total_bytes: Optional[int] = None
            step7_chunk_parquet_est_ram_gb: Optional[float] = None
            step8_screening_source: Optional[str] = None
            step8_screening_stats_source: Optional[str] = None
            step8_screening_sample_rows: Optional[int] = None
            step8_screening_full_train_rows: Optional[int] = None
            step8_screening_candidate_cols: Optional[int] = None
            step8_screened_feature_count: Optional[int] = None
            # Task 7 DoD: Step 6 chunk cache counters -> pipeline_diagnostics.json
            chunk_cache_stats: Dict[str, int] = {}

            # 1. Monthly chunks (DEC-008 / SSOT §4.3)
            print("[Step 1/10] Training window and monthly chunks…", flush=True)
            t0 = time.perf_counter()
            chunks = get_monthly_chunks(start, end)
            _el = time.perf_counter() - t0
            step1_duration_sec = _el
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
        
            # --- OOM pre-check (earliest feasible point: chunk list is final) ---
            # Estimate Step 7 peak RAM and auto-reduce NEG_SAMPLE_FRAC when OOM is likely.
            # Result may equal NEG_SAMPLE_FRAC (no change) or be lower (auto-adjusted).
            _effective_neg_sample_frac = _oom_check_and_adjust_neg_sample_frac(
                chunks, NEG_SAMPLE_FRAC
            )

            # T12.2: best-effort OOM pre-check estimate (for later RSS error ratio).
            # Keep this as a deterministic/side-effect-free computation so logging
            # never changes pipeline behavior.
            try:
                existing_sizes = [
                    _chunk_parquet_path(c).stat().st_size
                    for c in chunks
                    if _chunk_parquet_path(c).exists()
                    and _chunk_parquet_path(c).with_suffix(".cache_key").exists()
                ]
                if existing_sizes:
                    _per_chunk_bytes = sum(existing_sizes) / len(existing_sizes)
                else:
                    _per_chunk_bytes = NEG_SAMPLE_BYTES_PER_CHUNK_DEFAULT

                _n_chunks = len(chunks)
                _estimated_on_disk = _per_chunk_bytes * _n_chunks
                if STEP7_USE_DUCKDB:
                    _estimated_peak_ram = _estimated_on_disk * CHUNK_CONCAT_RAM_FACTOR * TRAIN_SPLIT_FRAC
                else:
                    _estimated_peak_ram = _estimated_on_disk * CHUNK_CONCAT_RAM_FACTOR * (1.0 + TRAIN_SPLIT_FRAC)
                oom_precheck_est_peak_ram_gb = _estimated_peak_ram / (1024**3)
            except Exception:
                oom_precheck_est_peak_ram_gb = None
        
            # 2. Chunk-level split — used ONLY to derive train_end for the canonical
            #    mapping cutoff (B1 / R25 identity-leakage guard).  The actual row
            #    assignment to train/valid/test happens later at row level (SSOT §9.2).
            print("[Step 2/10] Chunk-level split (train_end derivation)…", flush=True)
            t0 = time.perf_counter()
            split = get_train_valid_test_split(chunks)
            _el = time.perf_counter() - t0
            step2_duration_sec = _el
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
            #    PLAN steps 4/7/8: local path may load from artifact; else DuckDB or pandas build; write after build.
            print("[Step 3/10] Build canonical identity mapping…", flush=True)
            t0 = time.perf_counter()
            logger.info("Building canonical identity mapping (cutoff=%s)…", train_end)
            dummy_player_ids: set = set()
            rebuild_canonical = getattr(args, "rebuild_canonical_mapping", False)
            _canonical_built = False
            # PLAN step 8: try load existing artifact once (use_local and ClickHouse paths both skip build if ok)
            loaded_from_artifact = False
            if not rebuild_canonical and CANONICAL_MAPPING_PARQUET.exists() and CANONICAL_MAPPING_CUTOFF_JSON.exists():
                try:
                    with open(CANONICAL_MAPPING_CUTOFF_JSON, encoding="utf-8") as _f:
                        _sidecar = json.load(_f)
                    _cutoff_str = _sidecar.get("cutoff_dtm")
                    _cutoff_ts = pd.Timestamp(_cutoff_str) if _cutoff_str else None
                    if _cutoff_ts is not None:
                        _cutoff_naive = _cutoff_ts.replace(tzinfo=None) if _cutoff_ts.tz else _cutoff_ts
                        if _cutoff_naive >= train_end:
                            canonical_map = pd.read_parquet(CANONICAL_MAPPING_PARQUET)
                            if set(canonical_map.columns) >= {"player_id", "canonical_id"}:
                                dummy_player_ids = set(_sidecar.get("dummy_player_ids") or [])
                                dummy_player_ids = set(int(x) for x in dummy_player_ids)
                                loaded_from_artifact = True
                                logger.info(
                                    "Canonical mapping loaded from %s (cutoff %s >= train_end)",
                                    CANONICAL_MAPPING_PARQUET, _cutoff_str,
                                )
                            else:
                                logger.warning(
                                    "Canonical mapping artifact missing required columns; will rebuild"
                                )
                except Exception as exc:
                    logger.warning("Load canonical mapping artifact failed (%s); will rebuild", exc)
        
            if loaded_from_artifact:
                pass  # canonical_map, dummy_player_ids already set; skip build for both use_local and ClickHouse
            elif use_local:
                sessions_all = None  # R403 guardrail: ensure release in every path; set again in pandas branch
                use_full_sessions_pandas = getattr(_cfg, "CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS", False)
                if use_full_sessions_pandas:
                    logger.warning(
                        "CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS=True: loading full session window into pandas (high OOM risk, A03). Use only for debugging; keep DuckDB path in production."
                    )
                    _, sessions_all = load_local_parquet(
                        effective_start,
                        effective_end + timedelta(days=1),
                        sessions_only=True,
                    )
                    _, sessions_all = normalize_bets_sessions(pd.DataFrame(), sessions_all)
                    _, sessions_all = apply_dq(
                        pd.DataFrame(columns=["bet_id"]),
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
                    sess_path = LOCAL_PARQUET_DIR / "gmwds_t_session.parquet"
                    links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(sess_path, train_end)
                    canonical_map = build_canonical_mapping_from_links(links_df, dummy_pids)
                    dummy_player_ids = dummy_pids
                    sessions_all = None  # not used in DuckDB path; clear for peak memory guardrail (R403)
                _canonical_built = True
        
                if _canonical_built:
                    try:
                        canonical_map.to_parquet(CANONICAL_MAPPING_PARQUET, index=False)
                        _cutoff_iso = train_end.isoformat() if hasattr(train_end, "isoformat") else str(train_end)
                        with open(CANONICAL_MAPPING_CUTOFF_JSON, "w", encoding="utf-8") as _f:
                            json.dump(
                                {"cutoff_dtm": _cutoff_iso, "dummy_player_ids": list(dummy_player_ids)},
                                _f,
                                indent=0,
                            )
                        logger.info("Canonical mapping written to %s", CANONICAL_MAPPING_PARQUET)
                    except Exception as exc:
                        logger.warning("Write canonical mapping artifact failed (%s); next run will rebuild", exc)
                sessions_all = None
            else:
                try:
                    client = get_clickhouse_client()
                    canonical_map = build_canonical_mapping(client, cutoff_dtm=train_end)
                    dummy_player_ids = get_dummy_player_ids(client, cutoff_dtm=train_end)
                except Exception as exc:
                    logger.warning("ClickHouse canonical mapping failed (%s); using empty map", exc)
                    canonical_map = pd.DataFrame(columns=["player_id", "canonical_id"])
                    dummy_player_ids = set()
                sessions_all = None
                # PLAN § Canonical mapping 步驟 7：ClickHouse 路徑建完後也寫出，供共用／下次載入
                if set(canonical_map.columns) >= {"player_id", "canonical_id"} and not canonical_map.empty:
                    try:
                        canonical_map.to_parquet(CANONICAL_MAPPING_PARQUET, index=False)
                        _cutoff_iso = train_end.isoformat() if hasattr(train_end, "isoformat") else str(train_end)
                        with open(CANONICAL_MAPPING_CUTOFF_JSON, "w", encoding="utf-8") as _f:
                            json.dump(
                                {"cutoff_dtm": _cutoff_iso, "dummy_player_ids": list(dummy_player_ids)},
                                _f,
                                indent=0,
                            )
                        logger.info("Canonical mapping written to %s (from ClickHouse)", CANONICAL_MAPPING_PARQUET)
                    except Exception as exc:
                        logger.warning("Write canonical mapping artifact failed (%s); next run will rebuild", exc)
        
            _el = time.perf_counter() - t0
            step3_duration_sec = _el
            print("[Step 3/10] Build canonical identity mapping done in %.1fs" % _el, flush=True)
            logger.info(
                "Canonical mapping: %d rows; FND-12 dummy player_ids to exclude: %d  (%.1fs)",
                len(canonical_map), len(dummy_player_ids), _el,
            )
        
            # Rated-patron sampling is an independent option controlled by --sample-rated N.
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
                max_lookback_days=365,
            )
            _el = time.perf_counter() - t0
            step4_duration_sec = _el
            print("[Step 4/10] Ensure player_profile ready done in %.1fs" % _el, flush=True)
            logger.info("ensure_player_profile_ready: %.1fs", _el)
        
            # 3c. Load player_profile once for the entire training window (PLAN Step 4).
            #     Pass the resulting DataFrame to every process_chunk call so each chunk
            #     can do the PIT/as-of join without re-querying.  If load fails, profile
            #     features are 0 for all rows (graceful degradation).
            # R404 Review #1: empty map → [] so load_player_profile does not load full table (train-serve parity with backtester).
            _rated_cids: Optional[List[str]] = (
                list(rated_whitelist)
                if rated_whitelist
                else (
                    canonical_map["canonical_id"].astype(str).tolist()
                    if not canonical_map.empty
                    else []
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
            step5_duration_sec = _el
            if profile_df is not None:
                print("[Step 5/10] Load player_profile done in %.1fs (%d rows)" % (_el, len(profile_df)), flush=True)
                logger.info("player_profile: loaded %d snapshot rows for PIT join (%.1fs)", len(profile_df), _el)
            else:
                print("[Step 5/10] Load player_profile done in %.1fs (not available)" % _el, flush=True)
                logger.info("player_profile: not available — profile features will be NaN (%.1fs)", _el)
        
            feature_spec = load_feature_spec(FEATURE_SPEC_PATH)
            try:
                feature_spec_hash = hashlib.md5(Path(FEATURE_SPEC_PATH).read_bytes()).hexdigest()[:12]
            except Exception:
                feature_spec_hash = "unknown"
            logger.info(
                "Track LLM: loaded feature spec from %s (spec_hash=%s)",
                FEATURE_SPEC_PATH,
                feature_spec_hash,
            )
        
            # 4. Process chunks -> write parquet
            # When NEG_SAMPLE_FRAC_AUTO and there are chunks, run chunk 1 with frac=1.0 (OOM probe),
            # measure size, possibly lower _effective_neg_sample_frac, then process remaining chunks.
            _neg_sample_note = (
                f"  neg-sample={_effective_neg_sample_frac:.2f}" if _effective_neg_sample_frac < 1.0 else ""
            )
            print(
                f"[Step 6/10] Process chunks (DQ, labels, Track Human, Track LLM){_neg_sample_note}…",
                flush=True,
            )
            t0 = time.perf_counter()
            chunk_paths: List[Path] = []
            _step6_disable_bar = getattr(_cfg, "DISABLE_PROGRESS_BAR", False)
            pbar = (
                _ProgressNoop()
                if _step6_disable_bar
                else _tqdm_bar(total=len(chunks), desc="Step 6 chunks", unit="chunk")
            )
            try:
                if NEG_SAMPLE_FRAC_AUTO and len(chunks) > 0:
                    # OOM probe: process chunk 1 with frac=1.0, then decide effective frac.
                    print("[Step 6/10] OOM probe: chunk 1 with neg_sample_frac=1.0…", flush=True)
                    logger.info("OOM probe: processing chunk 1 with neg_sample_frac=1.0")
                    path1 = process_chunk(
                        chunks[0],
                        canonical_map,
                        dummy_player_ids=dummy_player_ids,
                        use_local_parquet=use_local,
                        force_recompute=force,
                        profile_df=profile_df,
                        feature_spec=feature_spec,
                        feature_spec_hash=feature_spec_hash,
                        neg_sample_frac=1.0,
                        chunk_cache_stats=chunk_cache_stats,
                    )
                    if path1 is not None:
                        _path1 = Path(path1) if isinstance(path1, str) else path1
                        if getattr(_path1, "exists", lambda: False)() and _path1.is_file():
                            size_chunk1 = _path1.stat().st_size
                            _effective_neg_sample_frac = _oom_check_after_chunk1(
                                size_chunk1, len(chunks), _effective_neg_sample_frac
                            )
                            if _effective_neg_sample_frac < 1.0:
                                path1_rerun = process_chunk(
                                    chunks[0],
                                    canonical_map,
                                    dummy_player_ids=dummy_player_ids,
                                    use_local_parquet=use_local,
                                    force_recompute=force,
                                    profile_df=profile_df,
                                    feature_spec=feature_spec,
                                    feature_spec_hash=feature_spec_hash,
                                    neg_sample_frac=_effective_neg_sample_frac,
                                    chunk_cache_stats=chunk_cache_stats,
                                )
                                if path1_rerun is not None:
                                    chunk_paths.append(path1_rerun)
                                    pbar.update(1)
                                else:
                                    chunk_paths.append(path1)
                                    pbar.update(1)
                            else:
                                chunk_paths.append(path1)
                                pbar.update(1)
                        else:
                            # Path does not exist (e.g. test mock): skip size-based adjustment
                            chunk_paths.append(path1)
                            pbar.update(1)
                        gc.collect()
                        for chunk in chunks[1:]:
                            path = process_chunk(
                                chunk,
                                canonical_map,
                                dummy_player_ids=dummy_player_ids,
                                use_local_parquet=use_local,
                                force_recompute=force,
                                profile_df=profile_df,
                                feature_spec=feature_spec,
                                feature_spec_hash=feature_spec_hash,
                                neg_sample_frac=_effective_neg_sample_frac,
                                chunk_cache_stats=chunk_cache_stats,
                            )
                            if path is not None:
                                chunk_paths.append(path)
                                pbar.update(1)
                            gc.collect()
                    else:
                        # Chunk 1 empty: no probe decision, use _effective_neg_sample_frac for all.
                        for chunk in chunks:
                            path = process_chunk(
                                chunk,
                                canonical_map,
                                dummy_player_ids=dummy_player_ids,
                                use_local_parquet=use_local,
                                force_recompute=force,
                                profile_df=profile_df,
                                feature_spec=feature_spec,
                                feature_spec_hash=feature_spec_hash,
                                neg_sample_frac=_effective_neg_sample_frac,
                                chunk_cache_stats=chunk_cache_stats,
                            )
                            if path is not None:
                                chunk_paths.append(path)
                                pbar.update(1)
                            gc.collect()
                else:
                    for i, chunk in enumerate(chunks):
                        path = process_chunk(
                            chunk,
                            canonical_map,
                            dummy_player_ids=dummy_player_ids,
                            use_local_parquet=use_local,
                            force_recompute=force,
                            profile_df=profile_df,
                            feature_spec=feature_spec,
                            feature_spec_hash=feature_spec_hash,
                            neg_sample_frac=_effective_neg_sample_frac,
                            chunk_cache_stats=chunk_cache_stats,
                        )
                        if path is not None:
                            chunk_paths.append(path)
                            pbar.update(1)
                        gc.collect()
            finally:
                pbar.close()
        
            _el = time.perf_counter() - t0
            step6_duration_sec = _el
            print("[Step 6/10] Process chunks done in %.1fs (%d chunks)" % (_el, len(chunk_paths)), flush=True)
            logger.info("Process chunks: %d produced  (%.1fs)", len(chunk_paths), _el)
            if not chunk_paths:
                raise SystemExit("No chunks produced any usable data — check data source / time window")
        
            # --- Step 7 helpers (PLAN Step 7 Out-of-Core: DuckDB sort+split) ---
            def _get_step7_available_ram_bytes() -> Optional[int]:
                try:
                    import psutil as _psutil
                    return _psutil.virtual_memory().available
                except Exception:
                    return None
        
            def _compute_step7_duckdb_budget(available_bytes: Optional[int]) -> int:
                """Compute DuckDB memory_limit (bytes) for Step 7 sort+split. DEC-027: uses config helper."""
                get_limit = getattr(_cfg, "get_duckdb_memory_limit_bytes", None)
                if get_limit is not None:
                    return get_limit("step7", available_bytes)
                lo = int(getattr(_cfg, "DUCKDB_MEMORY_LIMIT_MIN_GB", 1.0) * 1024**3)
                if available_bytes is None:
                    return lo
                frac = getattr(_cfg, "DUCKDB_RAM_FRACTION", 0.5)
                hi = int(getattr(_cfg, "DUCKDB_MEMORY_LIMIT_MAX_GB", 24.0) * 1024**3)
                return max(lo, min(hi, int(available_bytes * frac)))
        
            def _configure_step7_duckdb_runtime(con: Any, *, budget_bytes: int) -> None:
                """Set memory_limit, threads, temp_directory, preserve_insertion_order on *con*. DEC-027: from get_duckdb_memory_config('step7')."""
                get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
                if get_cfg is not None:
                    _tup = get_cfg("step7")
                    threads = max(1, int(_tup[4]))
                    preserve_order = _tup[5]
                    temp_dir_raw = _tup[6] or str(DATA_DIR / "duckdb_tmp")
                else:
                    threads = max(1, int(getattr(_cfg, "STEP7_DUCKDB_THREADS", 4)))
                    preserve_order = False
                    temp_dir_raw = getattr(_cfg, "STEP7_DUCKDB_TEMP_DIR", None) or str(DATA_DIR / "duckdb_tmp")
                if "'" in temp_dir_raw:
                    temp_dir = str(DATA_DIR / "duckdb_tmp")
                    logger.warning("Step 7 DuckDB temp_directory contains single quote; using fallback %s", temp_dir)
                else:
                    # DEC-027 Review #7: only allow path under DATA_DIR or exactly DATA_DIR/duckdb_tmp
                    try:
                        effective_resolved = Path(temp_dir_raw).resolve()
                        data_dir_resolved = DATA_DIR.resolve()
                        allowed_duckdb_tmp = (DATA_DIR / "duckdb_tmp").resolve()
                        if effective_resolved != allowed_duckdb_tmp:
                            effective_resolved.relative_to(data_dir_resolved)
                    except (ValueError, OSError):
                        temp_dir = str(DATA_DIR / "duckdb_tmp")
                        logger.warning(
                            "Step 7 DuckDB temp_directory outside DATA_DIR; using fallback %s",
                            temp_dir,
                        )
                    else:
                        temp_dir = temp_dir_raw
                budget_gb = budget_bytes / 1024**3
                temp_dir_sql = temp_dir.replace("'", "''")
                for _stmt, _label in [
                    (f"SET memory_limit='{budget_gb:.2f}GB'", "memory_limit"),
                    (f"SET threads={threads}", "threads"),
                    (f"SET temp_directory='{temp_dir_sql}'", "temp_directory"),
                ]:
                    try:
                        con.execute(_stmt)
                    except Exception as exc:
                        logger.warning("Step 7 DuckDB SET %s failed (non-fatal): %s", _label, exc)
                if not preserve_order:
                    try:
                        con.execute("SET preserve_insertion_order=false")
                    except Exception as exc:
                        logger.warning("Step 7 DuckDB SET preserve_insertion_order failed (non-fatal): %s", exc)
                logger.info(
                    "Step 7 DuckDB runtime: memory_limit=%.2fGB  threads=%d  temp_directory=%s",
                    budget_gb, threads, temp_dir,
                )
        
            def _is_duckdb_oom(exc: BaseException) -> bool:
                """Return True if *exc* is DuckDB OOM or MemoryError or 'unable to allocate' message."""
                try:
                    import duckdb as _duckdb
                    oom_cls = getattr(_duckdb, "OutOfMemoryException", None)
                    if oom_cls is not None and isinstance(exc, oom_cls):
                        return True
                except ImportError:
                    pass
                if isinstance(exc, MemoryError):
                    return True
                msg = str(exc.args[0]) if getattr(exc, "args", None) and exc.args else str(exc)
                return "unable to allocate" in msg.lower() or "out of memory" in msg.lower()
        
            def _step7_clean_duckdb_temp_dir() -> None:
                """Remove Step 7 DuckDB temp directory if it exists (PLAN Step 7: 清理暫存).
                Only deletes when path is DATA_DIR/duckdb_tmp or under DATA_DIR (R213 Review #1 whitelist).
                DEC-027: temp_dir from get_duckdb_memory_config('step7').
                """
                get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
                if get_cfg is not None:
                    temp_dir_raw = get_cfg("step7")[6] or str(DATA_DIR / "duckdb_tmp")
                else:
                    temp_dir_raw = getattr(_cfg, "STEP7_DUCKDB_TEMP_DIR", None) or str(DATA_DIR / "duckdb_tmp")
                if "'" in temp_dir_raw:
                    effective = DATA_DIR / "duckdb_tmp"
                else:
                    effective = Path(temp_dir_raw)
                data_dir_resolved = DATA_DIR.resolve()
                effective_resolved = effective.resolve()
                allowed_duckdb_tmp = (DATA_DIR / "duckdb_tmp").resolve()
                if effective_resolved != allowed_duckdb_tmp:
                    try:
                        effective_resolved.relative_to(data_dir_resolved)
                    except ValueError:
                        logger.warning(
                            "Step 7: refusing to remove DuckDB temp directory outside DATA_DIR: %s",
                            effective,
                        )
                        return
                if effective.exists() and effective.is_dir():
                    try:
                        shutil.rmtree(effective)
                        logger.info("Step 7: cleaned DuckDB temp directory %s", effective)
                    except OSError as _e:
                        logger.warning("Step 7: could not remove DuckDB temp directory %s: %s", effective, _e)
        
            def _duckdb_sort_and_split(
                chunk_paths: List[Path],
                train_frac: float,
                valid_frac: float,
            ) -> Tuple[Path, Path, Path]:
                """Sort chunk Parquets by payout_complete_dtm, canonical_id, bet_id and split into train/valid/test Parquet files.
                Uses DuckDB out-of-core; returns (train_path, valid_path, test_path).
                Creates step7_splits and DuckDB temp directory (or fallback DATA_DIR/duckdb_tmp when config path contains single quote).
                DuckDB may remove its temp directory on close; caller should not assume it exists after return.
                """
                if not chunk_paths:
                    raise ValueError("chunk_paths must be non-empty")
                if not (0 < train_frac and 0 < valid_frac and train_frac + valid_frac < 1.0):
                    raise ValueError(
                        "train_frac and valid_frac must be in (0, 1) and train_frac + valid_frac < 1"
                    )
                import duckdb
                path_list = [str(p) for p in chunk_paths]
                step7_dir = DATA_DIR / "step7_splits" / str(os.getpid())
                step7_dir.mkdir(parents=True, exist_ok=True)
                train_path = step7_dir / "split_train.parquet"
                valid_path = step7_dir / "split_valid.parquet"
                test_path = step7_dir / "split_test.parquet"
                get_cfg = getattr(_cfg, "get_duckdb_memory_config", None)
                temp_dir_raw = (get_cfg("step7")[6] if get_cfg else None) or str(DATA_DIR / "duckdb_tmp")
                if "'" in temp_dir_raw:
                    effective_temp_dir = str(DATA_DIR / "duckdb_tmp")
                else:
                    # DEC-027 Review #7: only use path under DATA_DIR or exactly DATA_DIR/duckdb_tmp
                    try:
                        effective_resolved = Path(temp_dir_raw).resolve()
                        data_dir_resolved = DATA_DIR.resolve()
                        allowed_duckdb_tmp = (DATA_DIR / "duckdb_tmp").resolve()
                        if effective_resolved != allowed_duckdb_tmp:
                            effective_resolved.relative_to(data_dir_resolved)
                    except (ValueError, OSError):
                        effective_temp_dir = str(DATA_DIR / "duckdb_tmp")
                        logger.warning(
                            "Step 7 DuckDB temp_directory outside DATA_DIR; using fallback %s",
                            effective_temp_dir,
                        )
                    else:
                        effective_temp_dir = temp_dir_raw
                Path(effective_temp_dir).mkdir(parents=True, exist_ok=True)
                con = duckdb.connect(":memory:")
                try:
                    budget = _compute_step7_duckdb_budget(_get_step7_available_ram_bytes())
                    _configure_step7_duckdb_runtime(con, budget_bytes=budget)
                    # Avoid prepared statement with list (Binder Error in some DuckDB builds).
                    paths_escaped = [p.replace("'", "''") for p in path_list]
                    paths_sql = ",".join(f"'{p}'" for p in paths_escaped)
                    con.execute(f"SELECT count(*) AS n FROM read_parquet([{paths_sql}])")
                    _row = con.fetchone()
                    if _row is None:
                        raise ValueError("No rows in chunk Parquets")
                    n_rows = _row[0]
                    if n_rows == 0:
                        raise ValueError("No rows in chunk Parquets")
                    train_end_idx = int(n_rows * train_frac)
                    valid_end_idx = int(n_rows * (train_frac + valid_frac))
                    col_rows = con.execute(
                        f"DESCRIBE SELECT * FROM read_parquet([{paths_sql}])"
                    ).fetchall()
                    available_cols = {str(r[0]) for r in col_rows}
                    order_cols: List[str] = ["payout_complete_dtm"]
                    if "canonical_id" in available_cols:
                        order_cols.append("canonical_id")
                    if "bet_id" in available_cols:
                        order_cols.append("bet_id")
                    order_sql = ", ".join(f"{c} NULLS LAST" for c in order_cols)
                    con.execute(
                        f"CREATE TEMP VIEW sorted_bets AS SELECT *, ROW_NUMBER() OVER (ORDER BY {order_sql}) - 1 AS _rn FROM read_parquet([{paths_sql}])"
                    )
                    _tp = str(train_path).replace("'", "''")
                    _vp = str(valid_path).replace("'", "''")
                    _sp = str(test_path).replace("'", "''")
                    try:
                        con.execute(
                            f"COPY (SELECT * EXCLUDE (_rn) FROM sorted_bets WHERE _rn >= 0 AND _rn < {train_end_idx}) TO '{_tp}' (FORMAT PARQUET)"
                        )
                        con.execute(
                            f"COPY (SELECT * EXCLUDE (_rn) FROM sorted_bets WHERE _rn >= {train_end_idx} AND _rn < {valid_end_idx}) TO '{_vp}' (FORMAT PARQUET)"
                        )
                        con.execute(
                            f"COPY (SELECT * EXCLUDE (_rn) FROM sorted_bets WHERE _rn >= {valid_end_idx}) TO '{_sp}' (FORMAT PARQUET)"
                        )
                    except Exception:
                        for p in (train_path, valid_path, test_path):
                            if p.exists():
                                p.unlink()
                        raise
                finally:
                    con.close()
                return (train_path, valid_path, test_path)
        
            def _step7_oom_failsafe_next_frac(current_frac: float) -> Tuple[float, bool]:
                """Compute next NEG_SAMPLE_FRAC after DuckDB OOM (halve); signal whether to retry.
                Returns (new_frac, should_retry). If already at NEG_SAMPLE_FRAC_MIN, raises
                with a clear message to reduce --days or add RAM. Orchestrator is responsible
                for re-running Step 6 with the returned new_frac and retrying _duckdb_sort_and_split.
                """
                if not (0.0 < current_frac <= 1.0):
                    raise ValueError(
                        "current_frac must be in (0, 1], got %s" % current_frac
                    )
                new_frac = max(NEG_SAMPLE_FRAC_MIN, current_frac / 2.0)
                if new_frac >= current_frac:
                    raise RuntimeError(
                        "Step 7 DuckDB OOM and NEG_SAMPLE_FRAC already at floor (%.2f). "
                        "Reduce training window (--days / --start --end) or add RAM."
                        % NEG_SAMPLE_FRAC_MIN
                    )
                return (new_frac, True)
        
            def _read_parquet_head(path: Path, n: int) -> pd.DataFrame:
                """Read first n rows from a Parquet file without loading full file (PLAN B+ Step 8 sample)."""
                if n <= 0:
                    return pd.DataFrame()
                import pyarrow as pa
                import pyarrow.parquet as pq
                pf = pq.ParquetFile(path)
                batches: List[Any] = []
                total = 0
                for batch in pf.iter_batches(batch_size=min(n, 100_000)):
                    batches.append(batch)
                    total += len(batch)
                    if total >= n:
                        break
                if not batches:
                    return pd.DataFrame()
                table = pa.Table.from_batches(batches)
                return table.slice(0, n).to_pandas()
        
            def _step7_metadata_from_paths(
                _train_path: Path, _valid_path: Path, _test_path: Path
            ) -> Tuple[int, int, int, int, Optional[Any]]:
                """(n_train, n_valid, n_test, label1_total, train_end_max) via DuckDB (PLAN B+)."""
                import duckdb
                con = duckdb.connect(":memory:")
                try:
                    def _q_count(p: Path) -> int:
                        s = str(p).replace("'", "''")
                        r = con.execute(f"SELECT count(*) FROM read_parquet('{s}')").fetchone()
                        return int(r[0]) if r else 0
        
                    def _q_label_sum(p: Path) -> int:
                        s = str(p).replace("'", "''")
                        r = con.execute(
                            f"SELECT coalesce(sum(cast(label AS INTEGER)), 0) FROM read_parquet('{s}')"
                        ).fetchone()
                        return int(r[0]) if r else 0
        
                    def _q_max_dtm(p: Path) -> Optional[Any]:
                        s = str(p).replace("'", "''")
                        r = con.execute(
                            f"SELECT max(payout_complete_dtm) FROM read_parquet('{s}')"
                        ).fetchone()
                        if r is None or r[0] is None:
                            return None
                        return pd.Timestamp(r[0])
        
                    n_train = _q_count(_train_path)
                    n_valid = _q_count(_valid_path)
                    n_test = _q_count(_test_path)
                    label1_total = _q_label_sum(_train_path) + _q_label_sum(_valid_path) + _q_label_sum(_test_path)
                    train_end_max = _q_max_dtm(_train_path)
                    return (n_train, n_valid, n_test, label1_total, train_end_max)
                finally:
                    con.close()
        
            def _step7_pandas_fallback(
                chunk_paths: List[Path],
                train_frac: float,
                valid_frac: float,
            ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Optional[Path], Optional[Path], Optional[Path]]:
                """Pandas in-memory concat + sort + row-level split (Layer 3 fallback).
                Returns (train_df, valid_df, test_df, None, None, None). Caller remains responsible for
                R700 log and MIN_VALID_TEST_ROWS warnings.
                Chunk Parquets must contain column payout_complete_dtm.
                """
                def _guard_step7_pandas_fallback_memory() -> None:
                    """Fail fast when pandas fallback is very likely to OOM on current RAM."""
                    _chunk_total_bytes_local = sum(Path(p).stat().st_size for p in chunk_paths)
                    if _chunk_total_bytes_local > STEP7_PANDAS_FALLBACK_MAX_BYTES:
                        raise RuntimeError(
                            "Step 7 pandas fallback blocked: chunk parquet total %.1f GB exceeds small-data "
                            "fallback limit %.1f GB. Pandas fallback is reserved for tiny test/dev datasets; "
                            "prefer STEP7_USE_DUCKDB=True, reduce --days / --start --end, or lower NEG_SAMPLE_FRAC."
                            % (
                                _chunk_total_bytes_local / (1024**3),
                                STEP7_PANDAS_FALLBACK_MAX_BYTES / (1024**3),
                            )
                        )
                    _estimated_peak_bytes = int(
                        _chunk_total_bytes_local
                        * CHUNK_CONCAT_RAM_FACTOR
                        * (1.0 + train_frac)
                    )
                    _available_bytes = _get_step7_available_ram_bytes()
                    _safe_budget_bytes = (
                        int(_available_bytes * NEG_SAMPLE_RAM_SAFETY)
                        if _available_bytes is not None and _available_bytes > 0
                        else None
                    )
                    if (
                        _safe_budget_bytes is not None
                        and _estimated_peak_bytes > _safe_budget_bytes
                    ):
                        raise RuntimeError(
                            "Step 7 pandas fallback blocked: estimated peak RAM %.1f GB exceeds safe available-RAM "
                            "budget %.1f GB. Prefer STEP7_USE_DUCKDB=True, reduce --days / --start --end, "
                            "or lower NEG_SAMPLE_FRAC."
                            % (
                                _estimated_peak_bytes / (1024**3),
                                _safe_budget_bytes / (1024**3),
                            )
                        )

                if not chunk_paths:
                    raise ValueError("chunk_paths must be non-empty")
                if not (
                    0 < train_frac and 0 < valid_frac and train_frac + valid_frac < 1.0
                ):
                    raise ValueError(
                        "train_frac and valid_frac must be in (0, 1) and "
                        "train_frac + valid_frac < 1.0"
                    )
                _guard_step7_pandas_fallback_memory()
                all_dfs = [pd.read_parquet(p) for p in chunk_paths]
                full_df = pd.concat(all_dfs, ignore_index=True)
                if "payout_complete_dtm" not in full_df.columns:
                    raise ValueError(
                        "chunk Parquets must contain column payout_complete_dtm"
                    )
                _payout_ts = pd.to_datetime(full_df["payout_complete_dtm"])
                if _payout_ts.dt.tz is not None:
                    _payout_ts = _payout_ts.dt.tz_localize(None)
                _sort_cols = ["_sort_ts_tmp"] + [
                    c for c in ("canonical_id", "bet_id") if c in full_df.columns
                ]
                full_df["_sort_ts_tmp"] = _payout_ts
                full_df.sort_values(_sort_cols, kind="stable", na_position="last", inplace=True)
                full_df.drop(columns=["_sort_ts_tmp"], inplace=True)
                full_df.reset_index(drop=True, inplace=True)
                n_rows = len(full_df)
                if n_rows == 0:
                    raise ValueError("chunk_paths produced no rows")
                _train_end_idx = int(n_rows * train_frac)
                _valid_end_idx = int(n_rows * (train_frac + valid_frac))
                _row_pos = np.arange(n_rows)
                full_df["_split"] = np.select(
                    [_row_pos < _train_end_idx, _row_pos < _valid_end_idx],
                    ["train", "valid"],
                    default="test",
                )
                _split_col = full_df["_split"]
                train_df = full_df[_split_col == "train"].reset_index(drop=True)
                valid_df = full_df[_split_col == "valid"].reset_index(drop=True)
                test_df = full_df[~(_split_col.isin(("train", "valid")))].reset_index(drop=True)
                del full_df, _split_col
                return (train_df, valid_df, test_df, None, None, None)
        
            def _step7_sort_and_split(
                chunk_paths: List[Path],
                train_frac: float,
                valid_frac: float,
                *,
                step6_runner: Optional[Callable[[float], List[Path]]] = None,
                current_neg_frac: Optional[float] = None,
            ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[Path], Optional[Path], Optional[Path]]:
                """Orchestrator: DuckDB sort+split (Layer 1), OOM retry (Layer 2), or pandas fallback (Layer 3).
                Returns (train_df, valid_df, test_df, train_path, valid_path, test_path). When STEP7_KEEP_TRAIN_ON_DISK
                and DuckDB succeed, train_df is None and paths are set (train not loaded). When STEP9_EXPORT_LIBSVM too,
                valid_df and test_df are not loaded (PLAN B+ 階段 6 第 2 步). Otherwise paths are None.
                When STEP7_KEEP_TRAIN_ON_DISK and DuckDB fails, raises (no pandas fallback) per PLAN B+.
                If DuckDB returns but read_parquet of the split files fails, falls back to pandas using chunk_paths.
                """
                if not STEP7_USE_DUCKDB:
                    if STEP7_KEEP_TRAIN_ON_DISK:
                        raise ValueError(
                            "STEP7_KEEP_TRAIN_ON_DISK=True requires STEP7_USE_DUCKDB=True. "
                            "Either set STEP7_USE_DUCKDB=True or set STEP7_KEEP_TRAIN_ON_DISK=False."
                        )
                    logger.warning(
                        "STEP7_USE_DUCKDB=False: using pandas fallback for Step 7 (high OOM risk). "
                        "Prefer STEP7_USE_DUCKDB=True or reduce --days / NEG_SAMPLE_FRAC. See doc/training_oom_and_runtime_audit.md A19."
                    )
                    return _step7_pandas_fallback(chunk_paths, train_frac, valid_frac)
                def _is_parquet_io_problem(err: Exception) -> bool:
                    msg = str(err).lower()
                    return (
                        "no files found that match the pattern" in msg
                        or "too small to be a parquet file" in msg
                        or "invalid parquet" in msg
                    )
                try:
                    train_path, valid_path, test_path = _duckdb_sort_and_split(
                        chunk_paths, train_frac, valid_frac
                    )
                    if STEP7_KEEP_TRAIN_ON_DISK:
                        if STEP9_EXPORT_LIBSVM:
                            _step7_clean_duckdb_temp_dir()
                            return (None, None, None, train_path, valid_path, test_path)
                        valid_df = pd.read_parquet(valid_path)
                        test_df = pd.read_parquet(test_path)
                        _step7_clean_duckdb_temp_dir()
                        return (None, valid_df, test_df, train_path, valid_path, test_path)
                    train_df = pd.read_parquet(train_path)
                    valid_df = pd.read_parquet(valid_path)
                    test_df = pd.read_parquet(test_path)
                    for p in (train_path, valid_path, test_path):
                        if p.exists():
                            p.unlink(missing_ok=True)
                    _step7_clean_duckdb_temp_dir()
                    return (train_df, valid_df, test_df, None, None, None)
                except Exception as exc:
                    if (
                        _is_duckdb_oom(exc)
                        and step6_runner is not None
                        and current_neg_frac is not None
                    ):
                        current = current_neg_frac
                        if not (0.0 < current_neg_frac <= 1.0):
                            if STEP7_KEEP_TRAIN_ON_DISK:
                                raise RuntimeError(
                                    "Step 7 STEP7_KEEP_TRAIN_ON_DISK is True and DuckDB failed; "
                                    "no pandas fallback. Reduce --days or add RAM."
                                )
                            logger.warning(
                                "Step 7 Layer 2 skipped: current_neg_frac=%.2f not in (0, 1]; falling back to pandas.",
                                current_neg_frac,
                            )
                            return _step7_pandas_fallback(chunk_paths, train_frac, valid_frac)
                        new_frac = None
                        retries_left = 3
                        while True:
                            step6_completed = False
                            train_path: Optional[Path] = None  # type: ignore[no-redef]
                            valid_path: Optional[Path] = None  # type: ignore[no-redef]
                            test_path: Optional[Path] = None  # type: ignore[no-redef]
                            try:
                                new_frac, _ = _step7_oom_failsafe_next_frac(current)
                                chunk_paths = step6_runner(new_frac)
                                if not chunk_paths:
                                    raise ValueError("step6_runner returned no chunk paths")
                                step6_completed = True
                                train_path, valid_path, test_path = _duckdb_sort_and_split(
                                    chunk_paths, train_frac, valid_frac
                                )
                                if STEP7_KEEP_TRAIN_ON_DISK:
                                    if STEP9_EXPORT_LIBSVM:
                                        _step7_clean_duckdb_temp_dir()
                                        return (None, None, None, train_path, valid_path, test_path)
                                    valid_df = pd.read_parquet(valid_path)
                                    test_df = pd.read_parquet(test_path)
                                    _step7_clean_duckdb_temp_dir()
                                    return (None, valid_df, test_df, train_path, valid_path, test_path)
                                train_df = pd.read_parquet(train_path)
                                valid_df = pd.read_parquet(valid_path)
                                test_df = pd.read_parquet(test_path)
                                for p in (train_path, valid_path, test_path):
                                    if p is not None and p.exists():
                                        p.unlink(missing_ok=True)
                                _step7_clean_duckdb_temp_dir()
                                return (train_df, valid_df, test_df, None, None, None)
                            except RuntimeError:
                                raise
                            except Exception as retry_exc:
                                for p in (train_path, valid_path, test_path):
                                    if p is not None and p.exists():
                                        p.unlink(missing_ok=True)
                                if (
                                    _is_duckdb_oom(retry_exc)
                                    and new_frac is not None
                                    and step6_completed
                                    and retries_left > 0
                                ):
                                    logger.warning(
                                        "Step 7 DuckDB OOM retry with NEG_SAMPLE_FRAC=%.4f; re-ran Step 6.",
                                        new_frac,
                                    )
                                    current = new_frac
                                    retries_left -= 1
                                    continue
                                if STEP7_KEEP_TRAIN_ON_DISK:
                                    if _is_parquet_io_problem(retry_exc):
                                        logger.warning(
                                            "Step 7 DuckDB parquet IO issue under keep-on-disk; "
                                            "falling back to pandas for test/dev robustness: %s",
                                            retry_exc,
                                        )
                                        return _step7_pandas_fallback(chunk_paths, train_frac, valid_frac)
                                    raise RuntimeError(
                                        "Step 7 STEP7_KEEP_TRAIN_ON_DISK: DuckDB failed after retries; "
                                        "no pandas fallback. Reduce --days or add RAM."
                                    ) from retry_exc
                                logger.warning(
                                    "Step 7 DuckDB failed (non-OOM) on retry; falling back to pandas: %s",
                                    retry_exc,
                                )
                                return _step7_pandas_fallback(chunk_paths, train_frac, valid_frac)
                    if STEP7_KEEP_TRAIN_ON_DISK:
                        if _is_parquet_io_problem(exc):
                            logger.warning(
                                "Step 7 DuckDB parquet IO issue under keep-on-disk; "
                                "falling back to pandas for test/dev robustness: %s",
                                exc,
                            )
                            return _step7_pandas_fallback(chunk_paths, train_frac, valid_frac)
                        raise RuntimeError(
                            "Step 7 STEP7_KEEP_TRAIN_ON_DISK is True and DuckDB failed; "
                            "no pandas fallback. Reduce --days or add RAM."
                        ) from exc
                    if _is_duckdb_oom(exc):
                        logger.warning(
                            "Step 7 DuckDB OOM; falling back to pandas in-memory sort+split: %s",
                            exc,
                        )
                    else:
                        logger.warning(
                            "Step 7 DuckDB failed (non-OOM); falling back to pandas: %s",
                            exc,
                        )
                    return _step7_pandas_fallback(chunk_paths, train_frac, valid_frac)
        
            # 5. Load all chunks, sort, row-level train/valid/test split (PLAN Step 7 Out-of-Core).
            #    Orchestrator: DuckDB first (Layer 1), on failure pandas fallback (Layer 3).
            # T12.2: checkpoint memory sampling across Step 7-9.
            # Peak := max(start, end) to keep sampling overhead low while satisfying
            # "start/peak/end are present" logging contracts.
            try:
                import psutil as _psutil  # optional dependency (best-effort)

                _step7_process = _psutil.Process()
                step7_rss_start_gb = _step7_process.memory_info().rss / (1024**3)
                _vm_start = _psutil.virtual_memory()
                _step7_sys_available_start_gb = _vm_start.available / (1024**3)
                _step7_sys_used_percent_start = float(_vm_start.percent)

                # MLflow tag naming contract (constants must be present in source).
                log_tags_safe(
                    {
                        "memory_sampling": "checkpoint_peak",
                        "memory_sampling_scope": "step7_9",
                    }
                )
            except Exception as _e:
                # If psutil is unavailable, still tag so MLflow run can be diagnosed.
                log_tags_safe({"memory_sampling": "disabled_no_psutil"})

            print("[Step 7/10] Load all chunks, concat, row-level train/valid/test split…", flush=True)
            t0 = time.perf_counter()
            _chunk_total_bytes = sum(Path(p).stat().st_size for p in chunk_paths)
            _est_ram_gb = (_chunk_total_bytes * CHUNK_CONCAT_RAM_FACTOR) / (1024**3)
            step7_chunk_parquet_total_bytes = _chunk_total_bytes
            step7_chunk_parquet_est_ram_gb = _est_ram_gb
            if _chunk_total_bytes >= CHUNK_CONCAT_MEMORY_WARN_BYTES:
                logger.warning(
                    "Chunk Parquets total %.2f GB on disk -> estimated %.1f GB RAM for concat + train/valid split. "
                    "Reduce training window (--days / --start --end) or ensure sufficient RAM to avoid OOM.",
                    _chunk_total_bytes / (1024**3),
                    _est_ram_gb,
                )
            # R803: validate fractions at runtime so misconfiguration is caught early (-O safe).
            if not (TRAIN_SPLIT_FRAC + VALID_SPLIT_FRAC < 1.0):
                raise ValueError(
                    f"TRAIN_SPLIT_FRAC ({TRAIN_SPLIT_FRAC}) + VALID_SPLIT_FRAC ({VALID_SPLIT_FRAC}) "
                    "must be < 1.0 to leave room for the test set"
                )
        
            def _run_step6(neg_frac: float) -> List[Path]:
                """Re-run Step 6 with given neg_sample_frac and force_recompute=True (Layer 2 OOM retry)."""
                paths: List[Path] = []
                for _i, _chunk in enumerate(chunks):
                    _path = process_chunk(
                        _chunk,
                        canonical_map,
                        dummy_player_ids=dummy_player_ids,
                        use_local_parquet=use_local,
                        force_recompute=True,
                        profile_df=profile_df,
                        feature_spec=feature_spec,
                        feature_spec_hash=feature_spec_hash,
                        neg_sample_frac=neg_frac,
                        chunk_cache_stats=chunk_cache_stats,
                    )
                    if _path is not None:
                        paths.append(_path)
                    gc.collect()
                return paths
        
            _step7_result = _step7_sort_and_split(
                chunk_paths,
                TRAIN_SPLIT_FRAC,
                VALID_SPLIT_FRAC,
                step6_runner=_run_step6,
                current_neg_frac=_effective_neg_sample_frac,
            )
            train_df, valid_df, test_df, step7_train_path, step7_valid_path, step7_test_path = _step7_result
            _train_libsvm: Optional[Path] = None
            _valid_libsvm: Optional[Path] = None
            _test_libsvm: Optional[Path] = None
            if step7_train_path is not None:
                # R202 Review #3: guard so _step7_metadata_from_paths never receives None (B+ path contract).
                if step7_valid_path is None or step7_test_path is None:
                    raise ValueError(
                        "step7_valid_path and step7_test_path must be set when step7_train_path is set (B+ path)."
                    )
                # PLAN B+ Stage 1–2: train not loaded; get metadata and sample for Step 8 from file.
                _n_train, _n_valid, _n_test, _label1_total, _train_end_max = _step7_metadata_from_paths(
                    step7_train_path, step7_valid_path, step7_test_path
                )
                _total_rows = _n_train + _n_valid + _n_test
                _label1 = _label1_total
                _actual_train_end = _train_end_max
                _sample_n_disk = (
                    int(STEP8_SCREEN_SAMPLE_ROWS)
                    if (STEP8_SCREEN_SAMPLE_ROWS is not None and STEP8_SCREEN_SAMPLE_ROWS >= 1)
                    else 2_000_000
                )
                _train_for_screen = _read_parquet_head(step7_train_path, _sample_n_disk)
            else:
                assert train_df is not None  # step7_train_path is None implies train was loaded in Step 7
                assert valid_df is not None and test_df is not None  # pandas path always has both
                _train_for_screen = None
                _n_valid = len(valid_df)
                _n_test = len(test_df)
                _total_rows = len(train_df) + _n_valid + _n_test
                _label1 = int(train_df["label"].sum()) + int(valid_df["label"].sum()) + int(test_df["label"].sum())
                _actual_train_end = train_df["payout_complete_dtm"].max() if not train_df.empty else None
            if step7_train_path is not None:
                assert step7_valid_path is not None and step7_test_path is not None
                _split_row_meta = split_row_metadata_from_parquet_paths(
                    step7_train_path,
                    step7_valid_path,
                    step7_test_path,
                )
            else:
                _split_row_meta = split_row_metadata_from_dataframes(
                    cast(pd.DataFrame, train_df),
                    cast(pd.DataFrame, valid_df),
                    cast(pd.DataFrame, test_df),
                )
            _train_cols = (
                train_df.columns
                if train_df is not None
                else (_train_for_screen.columns if _train_for_screen is not None else pd.Index([]))
            )
            n_rows = _total_rows  # for downstream summary (artifact, logs)
            _n_train_print = _n_train if step7_train_path is not None else (len(train_df) if train_df is not None else 0)
            logger.info("Total rows: %d  (label=1: %d)", _total_rows, _label1)
        
            # R700: compare row-level _actual_train_end against chunk-level train_end.
            # The canonical mapping cutoff (B1/R25 guard) always uses chunk-level train_end;
            # this log makes any semantic drift between the two boundaries observable.
            # R701 (known limitation): same run rows may be assigned to different split sets
            # at row-level boundaries — group-aware split is a long-term improvement.
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
            _n_valid_print = _n_valid if valid_df is None else len(valid_df)
            _n_test_print = _n_test if test_df is None else len(test_df)
            _el = time.perf_counter() - t0
            step7_duration_sec = _el
            print("[Step 7/10] Load all chunks, concat, row-level split done in %.1fs (train=%d valid=%d test=%d)" % (_el, _n_train_print, _n_valid_print, _n_test_print), flush=True)
            logger.info(
                "Row-level split (%.0f/%.0f/%.0f) — train: %d  valid: %d  test: %d  (load+sort+split: %.1fs)",
                TRAIN_SPLIT_FRAC * 100,
                VALID_SPLIT_FRAC * 100,
                (1.0 - TRAIN_SPLIT_FRAC - VALID_SPLIT_FRAC) * 100,
                _n_train_print, _n_valid_print, _n_test_print,
                _el,
            )
            if _n_valid_print < MIN_VALID_TEST_ROWS:
                logger.warning(
                    "Validation set has only %d rows (MIN_VALID_TEST_ROWS=%d); "
                    "AP and Optuna results will be unreliable. "
                    "Consider adding more --recent-chunks.",
                    _n_valid_print, MIN_VALID_TEST_ROWS,
                )
            if _n_test_print < MIN_VALID_TEST_ROWS:
                logger.warning(
                    "Test set has only %d rows (MIN_VALID_TEST_ROWS=%d); "
                    "backtester metrics will be unreliable.",
                    _n_test_print, MIN_VALID_TEST_ROWS,
                )
        
            active_feature_cols = get_all_candidate_feature_ids(feature_spec, screening_only=True)
        
            # 5b. Full-feature screening (DEC-020).
            # Runs on the TRAINING SET ONLY to comply with TRN-09 anti-leakage rules.
            #
            # Candidate set = active_feature_cols (Track Human + Legacy + Profile) PLUS
            # Track LLM candidate columns declared in feature spec and present in train_df.
            if feature_spec is not None:
                _track_llm_cols = [
                    cand.get("feature_id")
                    for cand in (feature_spec.get("track_llm", {}) or {}).get("candidates", [])
                    if cand.get("feature_id") in _train_cols
                ]
                if _track_llm_cols:
                    logger.info(
                        "screen_features: loaded %d Track LLM candidate columns from feature spec",
                        len(_track_llm_cols),
                    )
                _all_candidate_cols: List[str] = list(dict.fromkeys(active_feature_cols + _track_llm_cols))
            else:
                _all_candidate_cols = active_feature_cols
        
            # Only screen columns that actually exist in train (or train sample when B+ on disk).
            _present_candidate_cols = [c for c in _all_candidate_cols if c in _train_cols]
            if not _present_candidate_cols:
                logger.warning(
                    "screen_features: no candidate columns found in train_df — skipping screening"
                )
                # R1004: restrict active_feature_cols to columns actually present in train.
                active_feature_cols = [c for c in active_feature_cols if c in _train_cols]
                print("[Step 8/10] Feature screening skipped (no candidates)", flush=True)
            else:
                # PLAN 方案 B 策略 A / B+ Stage 2: use sample from memory or from file (_train_for_screen from _read_parquet_head when on disk).
                # Step 8 DuckDB std (PLAN): pass train_path or train_df so zv is computed on full data via DuckDB; keep _matrix_for_screen as sample to avoid OOM in corr/MI/LGBM.
                _cap = (
                    int(STEP8_SCREEN_SAMPLE_ROWS)
                    if (STEP8_SCREEN_SAMPLE_ROWS is not None and STEP8_SCREEN_SAMPLE_ROWS >= 1)
                    else 2_000_000
                )
                _screen_train_df: Optional[pd.DataFrame] = None
                if train_df is not None:
                    _sample_n = STEP8_SCREEN_SAMPLE_ROWS if (STEP8_SCREEN_SAMPLE_ROWS is not None and STEP8_SCREEN_SAMPLE_ROWS >= 1) else None
                    if _sample_n is not None:
                        _sample_n = int(_sample_n)  # Round 184 Review P2: coerce float to int before head()
                        _matrix_for_screen = train_df.head(_sample_n)
                        _screen_train_df = _matrix_for_screen
                        if len(_matrix_for_screen) < _sample_n:
                            logger.info(
                                "Step 8 screening: using first %d rows (train smaller than cap STEP8_SCREEN_SAMPLE_ROWS=%d); full train has %d rows",
                                len(_matrix_for_screen),
                                _sample_n,
                                len(train_df),
                            )
                        else:
                            logger.info(
                                "Step 8 screening: using first %d rows (cap STEP8_SCREEN_SAMPLE_ROWS); full train has %d rows",
                                len(_matrix_for_screen),
                                len(train_df),
                            )
                    else:
                        _matrix_for_screen = train_df.head(_cap)
                        _screen_train_df = _matrix_for_screen
                        logger.info(
                            "Step 8 screening: using first %d rows from in-memory train (full train has %d rows); "
                            "screening no longer re-reads full train for DuckDB std/corr",
                            len(_matrix_for_screen),
                            len(train_df),
                        )
                else:
                    _matrix_for_screen = _train_for_screen
                    logger.info(
                        "Step 8 screening: using first %d rows from train file (STEP7_KEEP_TRAIN_ON_DISK); full train has %d rows",
                        len(_matrix_for_screen),
                        _n_train_print,
                    )
                step8_screening_source = (
                    "in_memory_head" if train_df is not None else "train_file_head"
                )
                step8_screening_stats_source = (
                    "screening_sample_df" if train_df is not None else "train_path"
                )
                step8_screening_sample_rows = len(_matrix_for_screen)
                step8_screening_full_train_rows = (
                    len(train_df) if train_df is not None else _n_train_print
                )
                step8_screening_candidate_cols = len(_present_candidate_cols)
                step8_screened_feature_count = None
                print("[Step 8/10] Feature screening…", flush=True)
                t0 = time.perf_counter()
                screened_cols = screen_features(
                    feature_matrix=_matrix_for_screen,
                    labels=_matrix_for_screen["label"],
                    feature_names=_present_candidate_cols,
                    screen_method=SCREEN_FEATURES_METHOD,
                    train_path=step7_train_path if step7_train_path is not None else None,
                    train_df=_screen_train_df,
                )
                _el = time.perf_counter() - t0
                step8_duration_sec = _el
                step8_screened_feature_count = len(screened_cols)
                print("[Step 8/10] Feature screening done in %.1fs (%d -> %d features)" % (_el, len(_present_candidate_cols), len(screened_cols)), flush=True)
                logger.info(
                    "screen_features: %d -> %d features retained  (%.1fs)",
                    len(_present_candidate_cols), len(screened_cols), _el,
                )
                # R1001: post-screening sanity — ensure at least one Track Human feature survives.
                # Use YAML feature_spec (SSOT) instead of hardcoded list (feat-consolidation R123-2).
                _screened_set = set(screened_cols)
                _yaml_track_human = (
                    set(get_candidate_feature_ids(feature_spec, "track_human", screening_only=True))
                    if feature_spec is not None
                    else set()
                )
                if _yaml_track_human and not _screened_set.intersection(_yaml_track_human):
                    _missing_track_human = [c for c in _yaml_track_human if c in _train_cols]
                    if _missing_track_human:
                        logger.warning(
                            "screen_features: no track_human features survived screening — "
                            "re-appending %d track_human features as fallback (R1001)",
                            len(_missing_track_human),
                        )
                        screened_cols = screened_cols + [
                            c for c in _missing_track_human if c not in _screened_set
                        ]
                active_feature_cols = screened_cols
        
            # PLAN B+ Stage 2: load train from file after screening so export/Step 9 have train_df.
            if step7_train_path is not None:
                if _train_for_screen is not None:
                    _train_for_screen = None
                    if "_matrix_for_screen" in locals():
                        del _matrix_for_screen
                    gc.collect()
                if STEP9_EXPORT_LIBSVM and active_feature_cols:
                    assert step7_valid_path is not None and step7_test_path is not None  # R202 guard
                    _train_libsvm, _valid_libsvm, _test_libsvm = _export_parquet_to_libsvm(
                        step7_train_path,
                        step7_valid_path,
                        active_feature_cols,
                        DATA_DIR / "export",
                        test_path=step7_test_path,
                    )
                train_df = pd.read_parquet(step7_train_path)
                if step7_train_path.exists():
                    step7_train_path.unlink(missing_ok=True)
                logger.info(
                    "Step 7 B+: loaded train from file after screening (%d rows)%s",
                    len(train_df),
                    "; valid/test left on disk (B+ 階段 6 第 2 步)" if (valid_df is None and test_df is None) else "",
                )
        
            if not active_feature_cols:
                # R1613: explicit guardrail message for zero-feature situations.  In
                # integration / debug contexts (e.g. heavily mocked tests) we still
                # want the pipeline to run so that wiring between stages can be
                # exercised, so we fall back to a single constant "bias" feature
                # instead of terminating the process.
                msg = (
                    "screen_features + Track Human fallback both returned empty feature list. "
                    "Cannot train any model. Check data quality and feature definitions."
                )
                logger.warning(msg)
                print(msg, flush=True)
                _placeholder_col = "bias"  # constant feature for integration/debug runs (R1605: named via explicit variable)
                if train_df is not None and _placeholder_col not in train_df.columns:
                    train_df[_placeholder_col] = 0.0
                if valid_df is not None and not valid_df.empty and _placeholder_col not in valid_df.columns:
                    valid_df[_placeholder_col] = 0.0
                if test_df is not None and not test_df.empty and _placeholder_col not in test_df.columns:
                    test_df[_placeholder_col] = 0.0
                active_feature_cols = [_placeholder_col]
        
            # Plan B: export train/valid to CSV when training from file (PLAN 方案 B §3).
            # Skip when B+ LibSVM path (valid_df not loaded) — validation uses LibSVM from file.
            if STEP9_TRAIN_FROM_FILE and train_df is not None and valid_df is not None:
                _export_dir = DATA_DIR / "export"
                _train_csv, _valid_csv = _export_train_valid_to_csv(
                    train_df,
                    valid_df,
                    active_feature_cols,
                    _export_dir,
                    ranking_recipe=pipeline_ranking_recipe,
                )
                print(
                    "[Plan B] Exported train/valid to %s and %s"
                    % (_train_csv, _valid_csv),
                    flush=True,
                )
        
            # 6. Train dual model (Optuna + run-level sample_weight, DEC-013)
            #    test_df is passed so test-set metrics and feature importance are
            #    computed immediately after training and included in the artifact.
            print("[Step 9/10] Train rated GBM family (LGBM/CatBoost/XGBoost compare) + test-set eval…", flush=True)
            t0 = time.perf_counter()
            model_version = get_model_version()
            _libsvm_paths = (_train_libsvm, _valid_libsvm) if (_train_libsvm is not None and _valid_libsvm is not None) else None
            rated_art, _, combined_metrics = train_single_rated_model(
                train_df,
                valid_df,
                active_feature_cols,
                run_optuna=not skip_optuna,
                test_df=test_df,
                train_from_file=STEP9_TRAIN_FROM_FILE,
                train_libsvm_paths=_libsvm_paths,
                test_libsvm_path=_test_libsvm,
                ranking_recipe=pipeline_ranking_recipe,
                gbm_bakeoff=pipeline_gbm_bakeoff,
                valid_split_parquet_path=step7_valid_path,
                test_split_parquet_path=step7_test_path,
            )
            _el = time.perf_counter() - t0
            step9_duration_sec = _el
            print("[Step 9/10] Train rated GBM family + test-set eval done in %.1fs" % _el, flush=True)
            logger.info("train_single_rated_model + A3 family compare + test eval: %.1fs", _el)

            # T12.2: capture RSS/sys RAM snapshot at Step 9 end (checkpoint scope Step 7-9).
            # Peak := max(start, end) to avoid heavy sampling/polling overhead.
            if step7_rss_start_gb is not None:
                try:
                    import psutil as _psutil

                    _proc_end = _psutil.Process()
                    step7_rss_end_gb = _proc_end.memory_info().rss / (1024**3)
                    if step7_rss_end_gb is not None:
                        step7_rss_peak_gb = max(step7_rss_start_gb, step7_rss_end_gb)

                    _vm_end = _psutil.virtual_memory()
                    if _step7_sys_available_start_gb is not None:
                        _vm_end_avail_gb = _vm_end.available / (1024**3)
                        step7_sys_available_min_gb = min(_step7_sys_available_start_gb, _vm_end_avail_gb)
                    if _step7_sys_used_percent_start is not None:
                        _vm_end_used_percent = float(_vm_end.percent)
                        step7_sys_used_percent_peak = max(_step7_sys_used_percent_start, _vm_end_used_percent)
                except Exception:
                    # If memory sampling fails, just keep metrics unset; never impact training.
                    pass

            # Step 9 no longer needs the in-memory split frames after training returns.
            # Release them before artifact / MLflow phases so large train/valid/test
            # DataFrames do not stay resident through the rest of the pipeline.
            train_df = None
            valid_df = None
            test_df = None
            gc.collect()
        
            # 7. Save artifacts (versioned subdir under MODEL_DIR; see Priority 1 investigation plan).
            print("[Step 10/10] Save artifact bundle…", flush=True)
            t0 = time.perf_counter()
            _versions_root = MODEL_DIR
            _bundle_dir = safe_version_subdirectory(_versions_root, model_version)
            if _bundle_dir.exists() and (_bundle_dir / "model.pkl").exists():
                raise FileExistsError(
                    f"Refusing to overwrite existing model bundle: {_bundle_dir}. "
                    "Remove the directory or wait for a new model_version timestamp."
                )
            _bundle_dir.mkdir(parents=True, exist_ok=True)
            _baseline_align = _make_baseline_training_alignment_payload(
                effective_start,
                effective_end,
                float(TRAIN_SPLIT_FRAC),
                float(VALID_SPLIT_FRAC),
            )
            _split_mlflow_meta = split_row_metadata_to_mlflow_string_params(_split_row_meta)
            _model_meta_doc = build_model_metadata_document(
                model_version=model_version,
                effective_start=effective_start,
                effective_end=effective_end,
                splits=_split_row_meta,
                use_local_parquet=use_local,
                recent_chunks=getattr(args, "recent_chunks", None),
                sample_rated_n=sample_rated_n,
                skip_optuna=skip_optuna,
                neg_sample_frac_effective=_effective_neg_sample_frac,
                bundle_dir=_bundle_dir,
                combined_metrics=combined_metrics,
            )
            save_artifact_bundle(
                rated_art, active_feature_cols, combined_metrics, model_version,
                sample_rated_n=sample_rated_n,
                feature_spec_path=FEATURE_SPEC_PATH,
                neg_sample_frac=_effective_neg_sample_frac,
                bundle_dir=_bundle_dir,
                baseline_training_alignment=_baseline_align,
                model_metadata=_model_meta_doc,
            )
            try:
                write_latest_model_manifest(_versions_root, model_version, _bundle_dir)
            except Exception as _man_exc:
                logger.warning(
                    "Failed to write latest model manifest (artifacts saved): %s",
                    _man_exc,
                )
            _el = time.perf_counter() - t0
            step10_duration_sec = _el
            print("[Step 10/10] Save artifact bundle done in %.1fs" % _el, flush=True)
            logger.info("save_artifact_bundle: %.1fs", _el)

            # T13: Warm up MLflow (e.g. Cloud Run) before first log to reduce 503 on cold start.
            if has_active_run():
                warm_up_mlflow_run_safe()

            # Phase 2 T2: Log provenance to MLflow (no-op when URI unset/unreachable).
            try:
                _log_training_provenance_to_mlflow(
                    model_version=model_version,
                    artifact_dir=str(_bundle_dir),
                    training_window_start=effective_start,
                    training_window_end=effective_end,
                    feature_spec_path=str(FEATURE_SPEC_PATH),
                    training_metrics_path=str(_bundle_dir / "training_metrics.json"),
                    pipeline_diagnostics_path=str(_bundle_dir / "pipeline_diagnostics.json"),
                    pipeline_diagnostics_rel_path=f"{_bundle_dir.name}/pipeline_diagnostics.json",
                    model_metadata_path=str(_bundle_dir / "model_metadata.json"),
                    model_metadata_rel_path=f"{_bundle_dir.name}/model_metadata.json",
                    split_boundary_params=_split_mlflow_meta,
                )
            except Exception as e:
                logger.warning("MLflow provenance logging failed (training still succeeded): %s", e)
        
            # Remove stale dual-model and legacy pickles so operators do not assume
            # they are loadable (DEC-040: only model.pkl is read).
            for _stale in ["nonrated_model.pkl", "rated_model.pkl", "walkaway_model.pkl"]:
                _stale_path = _versions_root / _stale
                if _stale_path.exists():
                    _stale_path.unlink()
                    logger.info("Removed stale artifact: %s", _stale)
        
            total_sec = time.perf_counter() - pipeline_start
            _pipeline_finished_at_iso = datetime.now(timezone.utc).isoformat()
            if (
                oom_precheck_est_peak_ram_gb is not None
                and oom_precheck_est_peak_ram_gb > 0
                and step7_rss_peak_gb is not None
            ):
                oom_precheck_step7_rss_error_ratio = (
                    step7_rss_peak_gb / oom_precheck_est_peak_ram_gb
                )
            try:
                _write_pipeline_diagnostics_json(
                    model_version=model_version,
                    pipeline_started_at=pipeline_started_at_iso,
                    pipeline_finished_at=_pipeline_finished_at_iso,
                    total_duration_sec=total_sec,
                    step1_duration_sec=step1_duration_sec,
                    step2_duration_sec=step2_duration_sec,
                    step3_duration_sec=step3_duration_sec,
                    step4_duration_sec=step4_duration_sec,
                    step5_duration_sec=step5_duration_sec,
                    step6_duration_sec=step6_duration_sec,
                    step7_duration_sec=step7_duration_sec,
                    step8_duration_sec=step8_duration_sec,
                    step9_duration_sec=step9_duration_sec,
                    step10_duration_sec=step10_duration_sec,
                    oom_precheck_est_peak_ram_gb=oom_precheck_est_peak_ram_gb,
                    oom_precheck_step7_rss_error_ratio=oom_precheck_step7_rss_error_ratio,
                    step7_rss_start_gb=step7_rss_start_gb,
                    step7_rss_peak_gb=step7_rss_peak_gb,
                    step7_rss_end_gb=step7_rss_end_gb,
                    step7_sys_available_min_gb=step7_sys_available_min_gb,
                    step7_sys_used_percent_peak=step7_sys_used_percent_peak,
                    step7_chunk_parquet_total_bytes=step7_chunk_parquet_total_bytes,
                    step7_chunk_parquet_est_ram_gb=step7_chunk_parquet_est_ram_gb,
                    step8_screening_source=step8_screening_source,
                    step8_screening_stats_source=step8_screening_stats_source,
                    step8_screening_sample_rows=step8_screening_sample_rows,
                    step8_screening_full_train_rows=step8_screening_full_train_rows,
                    step8_screening_candidate_cols=step8_screening_candidate_cols,
                    step8_screened_feature_count=step8_screened_feature_count,
                    chunk_cache_stats=chunk_cache_stats,
                    output_dir=_bundle_dir,
                )
            except Exception as _diag_exc:
                logger.warning(
                    "pipeline_diagnostics.json write failed (training still succeeded): %s",
                    _diag_exc,
                )

            # Phase 2 / pipeline plan: small-file artifacts for MLflow UI (best-effort; no active run → no-op).
            # P1.5: full bundle under model_bundle/ (log_artifacts_safe) + SHA-256 params; keeps bundle/ copies below.
            if has_active_run():
                _checksum_params: Dict[str, str] = {}
                _mpath = _bundle_dir / "model.pkl"
                if _mpath.is_file():
                    try:
                        _checksum_params["model_pkl_sha256"] = _sha256_file_hex(_mpath)
                    except Exception as _h_exc:
                        logger.warning("model.pkl checksum failed (MLflow param skipped): %s", _h_exc)
                if FEATURE_SPEC_PATH.is_file():
                    try:
                        _checksum_params["feature_spec_sha256"] = _sha256_file_hex(FEATURE_SPEC_PATH)
                    except Exception as _h_exc:
                        logger.warning("feature_spec checksum failed (MLflow param skipped): %s", _h_exc)
                if _checksum_params:
                    try:
                        log_params_safe(_checksum_params)
                    except Exception as _p_exc:
                        logger.warning("MLflow checksum params failed (training still succeeded): %s", _p_exc)
                # P1.5: full bundle (includes model.pkl); transient retries in helper.
                log_artifacts_safe(_bundle_dir, artifact_path="model_bundle")
                # Legacy UI path: small files under bundle/ (contract tests + existing dashboards).
                _bundle_artifact_path = "bundle"
                for _fname in (
                    "training_metrics.json",
                    "pipeline_diagnostics.json",
                    "model_metadata.json",
                    "feature_spec.yaml",
                    "model_version",
                ):
                    _ap = _bundle_dir / _fname
                    if _ap.is_file():
                        log_artifact_safe(_ap, artifact_path=_bundle_artifact_path)

            print("All steps completed. Pipeline total: %.1fs (%.1f min)" % (total_sec, total_sec / 60.0), flush=True)
            logger.info("Pipeline total: %.1fs (%.1f min)", total_sec, total_sec / 60.0)

            # T12.2: Log training success metrics + per-step durations + Step 7–9 memory/OOM diagnostics to MLflow.
            try:
                oom_params = {
                    "oom_precheck_est_peak_ram_gb": oom_precheck_est_peak_ram_gb,
                    "oom_precheck_step7_rss_error_ratio": oom_precheck_step7_rss_error_ratio,
                }
                # Avoid logging None values (MLflow params do not accept nulls well).
                oom_params_clean = {k: v for k, v in oom_params.items() if v is not None}
                if oom_params_clean:
                    log_params_safe(oom_params_clean)

                log_params_safe(
                    {
                        "lightgbm_device_requested": _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS,
                        "lightgbm_device_effective": _EFFECTIVE_LIGHTGBM_DEVICE,
                        "lightgbm_device_fallback": str(bool(_LIGHTGBM_GPU_FALLBACK_USED)),
                    }
                )

                # Training metrics from artifact, then pipeline timing + memory/OOM last so
                # combined_metrics["rated"] cannot overwrite reserved keys (vs pipeline_diagnostics.json).
                mlflow_metrics: dict[str, Any] = {}
                _rated = (combined_metrics or {}).get("rated", {})
                if isinstance(_rated, dict):
                    mlflow_metrics.update(_rated)

                mlflow_metrics.update(
                    {
                        "total_duration_sec": total_sec,
                        "step1_duration_sec": step1_duration_sec,
                        "step2_duration_sec": step2_duration_sec,
                        "step3_duration_sec": step3_duration_sec,
                        "step4_duration_sec": step4_duration_sec,
                        "step5_duration_sec": step5_duration_sec,
                        "step6_duration_sec": step6_duration_sec,
                        "step7_duration_sec": step7_duration_sec,
                        "step8_duration_sec": step8_duration_sec,
                        "step9_duration_sec": step9_duration_sec,
                        "step10_duration_sec": step10_duration_sec,
                        # Step 7-9 checkpoint memory metrics (names align with plan).
                        "step7_rss_start_gb": step7_rss_start_gb,
                        "step7_rss_peak_gb": step7_rss_peak_gb,
                        "step7_rss_end_gb": step7_rss_end_gb,
                        "step7_sys_available_min_gb": step7_sys_available_min_gb,
                        "step7_sys_used_percent_peak": step7_sys_used_percent_peak,
                        # Keep this also as a metric for easier plotting.
                        "oom_precheck_step7_rss_error_ratio": oom_precheck_step7_rss_error_ratio,
                    }
                )

                log_metrics_safe(mlflow_metrics)
            except Exception as _mlflow_exc:
                logger.warning("MLflow success diagnostics logging failed: %s", _mlflow_exc)
        
            summary = {
                "model_version": model_version,
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "total_rows": n_rows,
                "metrics": combined_metrics,
            }
            print(json.dumps(summary, indent=2, default=str))
        except Exception as e:
            log_tags_safe({"status": "FAILED", "error": str(e)[:500]})
            # T12 failure diagnostics (optional follow-on): log best-effort params for post-mortem.
            # Never allow this diagnostics step to change failure behavior.
            try:
                def _iso_or_str(x: Any) -> Optional[str]:
                    # Code Review §6: keep MLflow params bounded to avoid oversized strings.
                    _MAX_CHARS = 200
                    if x is None:
                        return None
                    if hasattr(x, "isoformat"):
                        s = x.isoformat()  # datetime-like
                    else:
                        s = str(x)
                    if len(s) > _MAX_CHARS:
                        return s[:_MAX_CHARS]
                    return s

                chunk_count = len(chunks) if chunks else None
                failure_params = {
                    "training_window_start": _iso_or_str(effective_start),
                    "training_window_end": _iso_or_str(effective_end),
                    "recent_chunks": recent_chunks,
                    "neg_sample_frac": _effective_neg_sample_frac,
                    "chunk_count": chunk_count,
                    "use_local_parquet": bool(use_local),
                    "oom_precheck_est_peak_ram_gb": oom_precheck_est_peak_ram_gb,
                }
                # MLflow expects non-null scalar params; drop None.
                failure_params_clean = {k: v for k, v in failure_params.items() if v is not None}
                if failure_params_clean:
                    log_params_safe(failure_params_clean)
            except Exception:
                pass
            raise



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    from trainer.training.trainer_argparse import build_trainer_argparser

    args = build_trainer_argparser().parse_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()
