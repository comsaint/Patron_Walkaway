"""trainer/trainer.py — Phase 1 Refactor
=========================================
Patron Walkaway Prediction — Training Pipeline

Compatibility facade: new code should import paths/time from
``trainer.training.common_runtime``, identity DuckDB from
``trainer.training.identity_runtime``, metric/predict helpers from
``trainer.training.model_eval_runtime``, GBM HPO defaults from
``trainer.training.hpo_runtime``, Step 7 sort/split from
``trainer.training.step7_split_runtime``, artifact writers from
``trainer.core.training_artifact_bundle``, and the full pipeline body from
``trainer.training.pipeline_run_core.run_pipeline_core`` (orchestrator:
``trainer.training.pipeline_orchestrator.run_pipeline_impl``). The public
decorated entry remains ``run_pipeline`` here.

Pipeline (SSOT §4.3 / §9)
--------------------------
1. time_fold.get_single_window_chunk(start, end)  -> one training window (single-path; no monthly partition)
2. Per window: load bets + sessions -> DQ -> identity -> labels -> Track Human features
   - Data source: ClickHouse (production) OR local Parquet (dev iteration)
   - Labels use C1 extended pull; bets in (window_end, extended_end] are
     used only for label computation, NOT added to training rows.
3. Write processed window rows to .data/chunks/ as Parquet (legacy compat path).
4. Concatenate window part(s); split train / valid / test at ROW level (time-ordered
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
  feature_list.json         [{name, track}]  track ∈ canonical layer+method keys
                              (``bet_duckdb_window``, ``run_state_machine``, ``player_run_asset``);
                              legacy ``track_*`` strings remain readable for old bundles.
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
from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple, Union, cast

import joblib
import lightgbm as lgb
import numpy as np
import optuna
from optuna.trial import FrozenTrial
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.model_selection import train_test_split
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
    RANKING_RECIPE_COMBINED,
    RANKING_RECIPE_HNM,
    apply_ranking_recipe_pre_optuna_weights,
    build_final_ranking_weights_from_libsvm_proxy,
    build_final_ranking_weights_in_memory,
    invalidate_lgb_binary_cache_for_libsvm,
    read_libsvm_weight_file,
    refine_weights_hnm_shallow_lgbm,
    write_libsvm_weight_file,
    resolve_ranking_recipe,
)
from trainer.training.split_file_bundle import (
    merge_libsvm_files,
    merge_train_valid_weight_files,
    trainer_file_backed_strict_enabled,
    validate_libsvm_paths_exist,
    write_split_manifest,
)
from trainer.training.pipeline_step_context import (
    ensure_pipeline_step_log_filter_installed,
    get_pipeline_step_label,
    message_already_has_pipeline_step_prefix,
    pipeline_step_set,
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

_PIPELINE_ECHO_PREFIX = "[Pipeline]"


def pipeline_echo(msg: str) -> None:
    """Single-line stdout milestone for operators (keep detailed logs on ``logger``).

    Prefer messages prefixed with ``Step N/11 —`` (``N`` in 0–10; optional ``7b``; 11 steps total) so
    terminal progress aligns with pipeline diagnostics step keys.

    When :func:`pipeline_step_set` has bound a label and *msg* does not already start with
    a canonical ``Step N/11`` marker, the label is prepended so every line matches logger output.
    """
    label = get_pipeline_step_label()
    out = msg
    if label and not message_already_has_pipeline_step_prefix(msg):
        out = f"{label} — {msg}"
    print(f"{_PIPELINE_ECHO_PREFIX} {out}", flush=True)


def _run_pipeline_with_step_cleanup(fn: Callable[..., None]) -> Callable[..., None]:
    """Install step log filter once and clear the step label after ``run_pipeline`` exits."""

    from functools import wraps

    @wraps(fn)
    def _wrapped(args: Any) -> None:
        ensure_pipeline_step_log_filter_installed()
        try:
            fn(args)
        finally:
            pipeline_step_set(None)

    return _wrapped

# MLflow namespace: keep this project isolated on shared tracking server.
# Override via credential/mlflow.env (MLFLOW_EXPERIMENT_TRAIN).
MLFLOW_EXPERIMENT_TRAIN = (
    (os.environ.get("MLFLOW_EXPERIMENT_TRAIN") or "").strip()
    or "patron/patron_walkaway/prod/train"
)
# Full bundle (model.pkl, metrics, spec, …) is logged under this artifact prefix.
MLFLOW_FULL_MODEL_BUNDLE_ARTIFACT_PATH = "model_bundle"

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
    TRAIN_SPLIT_FRAC: float = getattr(_cfg, "TRAIN_SPLIT_FRAC", 0.65)
    VALID_SPLIT_FRAC: float = getattr(_cfg, "VALID_SPLIT_FRAC", 0.20)
    MIN_VALID_TEST_ROWS: int = getattr(_cfg, "MIN_VALID_TEST_ROWS", 50)
    THRESHOLD_MIN_ALERT_COUNT: int = getattr(_cfg, "THRESHOLD_MIN_ALERT_COUNT", 5)
    THRESHOLD_MIN_RECALL: Optional[float] = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)
    THRESHOLD_FBETA: float = getattr(_cfg, "THRESHOLD_FBETA", 0.5)
    NEG_SAMPLE_FRAC: float = getattr(_cfg, "NEG_SAMPLE_FRAC", 1.0)
    NEG_SAMPLE_FRAC_AUTO: bool = getattr(_cfg, "NEG_SAMPLE_FRAC_AUTO", False)
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
    STEP8_SCREEN_SAMPLE_STRATEGY: str = str(
        getattr(_cfg, "STEP8_SCREEN_SAMPLE_STRATEGY", "head") or "head"
    )
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
    TRAIN_SPLIT_FRAC = getattr(_cfg, "TRAIN_SPLIT_FRAC", 0.65)
    VALID_SPLIT_FRAC = getattr(_cfg, "VALID_SPLIT_FRAC", 0.20)
    MIN_VALID_TEST_ROWS = getattr(_cfg, "MIN_VALID_TEST_ROWS", 50)
    THRESHOLD_MIN_ALERT_COUNT = getattr(_cfg, "THRESHOLD_MIN_ALERT_COUNT", 5)
    THRESHOLD_MIN_RECALL = getattr(_cfg, "THRESHOLD_MIN_RECALL", 0.01)
    THRESHOLD_FBETA = getattr(_cfg, "THRESHOLD_FBETA", 0.5)
    NEG_SAMPLE_FRAC = getattr(_cfg, "NEG_SAMPLE_FRAC", 1.0)
    NEG_SAMPLE_FRAC_AUTO = getattr(_cfg, "NEG_SAMPLE_FRAC_AUTO", False)
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
    STEP8_SCREEN_SAMPLE_STRATEGY = str(
        getattr(_cfg, "STEP8_SCREEN_SAMPLE_STRATEGY", "head") or "head"
    )
    CASINO_PLAYER_ID_CLEAN_SQL = getattr(_cfg, "CASINO_PLAYER_ID_CLEAN_SQL", "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END")

from trainer.core.training_artifact_bundle import (
    _log_training_provenance_to_mlflow,
    _make_baseline_training_alignment_payload,
    _sha256_file_hex,
    _write_pipeline_diagnostics_json,
    save_artifact_bundle,
)
from trainer.training.metrics_eval import (
    _precision_prod_adjusted,
    _warn_if_invalid_production_neg_pos_ratio,
)
from trainer.training.model_eval_runtime import (
    _batched_booster_predict_scores,
    _batched_model_positive_class_scores,
    _compute_feature_importance,
    _compute_test_metrics,
    _compute_test_metrics_from_scores,
    _compute_train_metrics,
    _compute_valid_metrics_from_scores,
    _dataframe_for_lgb_predict,
    _field_test_hpo_min_alerts_per_hour_for_reports,
    _histogram_average_precision_streaming,
    _lgb_booster_feature_name_list,
    _split_alert_density_prefixed_dict,
    _train_metrics_dict_from_y_scores,
    _tp_fp_fn_at_threshold_streaming,
)
from trainer.training.hpo_runtime import (
    _apply_backend_imbalance_params,
    _backend_hpo_defaults,
    _balanced_binary_class_ratio,
    _catboost_gpu_supports_rsm,
    _sanitize_catboost_params_for_runtime,
)
from trainer.training.step7_split_runtime import (
    DuckdbStep7Runtime,
    is_duckdb_oom,
    read_parquet_head,
    step7_clean_duckdb_temp_dir,
    step7_metadata_from_paths,
    _step7_sort_and_split,
)


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

_TRAINER_DEVICE_MODE_RAW = str(
    getattr(
        _cfg,
        "TRAINER_DEVICE_MODE",
        getattr(_core_trainer_config, "TRAINER_DEVICE_MODE", "auto"),
    )
).strip().lower()
if _TRAINER_DEVICE_MODE_RAW not in ("auto", "cpu", "gpu"):
    logger.warning(
        "TRAINER_DEVICE_MODE=%r invalid (use auto, cpu, or gpu); using auto",
        _TRAINER_DEVICE_MODE_RAW,
    )
    TRAINER_DEVICE_MODE: str = "auto"
else:
    TRAINER_DEVICE_MODE = _TRAINER_DEVICE_MODE_RAW

# Effective device for this process: updated by configure_lightgbm_device_for_run() in run_pipeline.
_EFFECTIVE_LIGHTGBM_DEVICE: str = LIGHTGBM_DEVICE_TYPE
_LIGHTGBM_GPU_FALLBACK_USED: bool = False
_REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS: str = LIGHTGBM_DEVICE_TYPE
_CLI_LIGHTGBM_DEVICE_OVERRIDE: Optional[str] = None
_REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS: str = TRAINER_DEVICE_MODE
_LAST_GBM_BACKEND_EFFECTIVE_DEVICE: str = "cpu"
_GBM_BACKEND_GPU_FALLBACK_USED: bool = False

try:
    _threshold_selection_mod = _import_module_threshold_selection(
        "trainer.training.threshold_selection"
    )
except ModuleNotFoundError:
    _threshold_selection_mod = _import_module_threshold_selection("training.threshold_selection")
pick_threshold_dec026 = _threshold_selection_mod.pick_threshold_dec026
Dec026ThresholdPick = _threshold_selection_mod.Dec026ThresholdPick

# Module-level pipeline imports: try = run from trainer dir with modules on path (e.g. dev);
# except = run as package (python -m trainer.trainer). Only the except path uses relative db_conn.
try:
    from time_fold import (  # type: ignore[import]
        get_single_window_chunk,
        partition_windows_for_train_end_cutoff,
    )
    from identity import (  # type: ignore[import]
        build_canonical_mapping_from_df,
        build_canonical_mapping,
        build_canonical_mapping_from_links,
        get_dummy_player_ids,
        get_dummy_player_ids_from_df,
    )
    from labels import compute_labels  # type: ignore[import]
    from features import (  # type: ignore[import]
        add_wave2_personalized_baselines,
        compute_consecutive_non_win_streak,
        compute_loss_streak,
        compute_run_boundary,
        compute_table_hc,
        compute_bet_duckdb_window_features,
        load_feature_spec,
        join_player_profile,
        screen_features,
        coerce_feature_dtypes,
        PROFILE_FEATURE_COLS,
        get_all_candidate_feature_ids,
        get_candidate_feature_ids,
        get_cross_layer_compose_contract,
        resolve_spec_track_section,
    )
    # Phase B PR-B3: layered (bet/run/trip/player) entrypoints.
    from layered import (  # type: ignore[import]
        compute_bet_duckdb_window_features,
        compute_bet_layer_features,
        compute_player_layer_features,
        compute_trip_layer_features,
        evaluate_pit_admission,
        SKIP_REASON_IDENTITY_UNMATCHED,
        SKIP_REASON_PIT_UNAVAILABLE_SOURCE,
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
    from trainer.time_fold import (  # type: ignore[import]
        get_single_window_chunk,
        partition_windows_for_train_end_cutoff,
    )
    from trainer.identity import (  # type: ignore[import]
        build_canonical_mapping_from_df,
        build_canonical_mapping,
        build_canonical_mapping_from_links,
        get_dummy_player_ids,
        get_dummy_player_ids_from_df,
    )
    from trainer.labels import compute_labels  # type: ignore[import]
    from trainer.features import (  # type: ignore[import]
        add_wave2_personalized_baselines,
        compute_consecutive_non_win_streak,
        compute_loss_streak,
        compute_run_boundary,
        compute_table_hc,
        compute_bet_duckdb_window_features,
        load_feature_spec,
        join_player_profile,
        screen_features,
        coerce_feature_dtypes,
        PROFILE_FEATURE_COLS,
        get_all_candidate_feature_ids,
        get_candidate_feature_ids,
        get_cross_layer_compose_contract,
        resolve_spec_track_section,
    )
    # Phase B PR-B3: layered entrypoints (bet/player thin wrappers over legacy).
    from trainer.features.layered import (  # type: ignore[import]
        compute_bet_duckdb_window_features,
        compute_bet_layer_features,
        compute_player_layer_features,
        compute_trip_layer_features,
        evaluate_pit_admission,
        SKIP_REASON_IDENTITY_UNMATCHED,
        SKIP_REASON_PIT_UNAVAILABLE_SOURCE,
    )
    from trainer.db_conn import get_clickhouse_client  # type: ignore[import]
    from trainer.etl_player_profile import (  # type: ignore[import]
        compute_profile_schema_hash,
        LOCAL_PROFILE_SCHEMA_HASH,
        backfill as _etl_backfill,
    )
    from trainer.config import SCREEN_FEATURES_METHOD  # type: ignore[import]
    from trainer.schema_io import normalize_bets_sessions  # type: ignore[import]

# Issue #33: paths + column lists + time helpers live in ``common_runtime``; identity DuckDB in ``identity_runtime``.
from trainer.training.common_runtime import (  # noqa: E402
    _BET_SELECT_COLS,
    _CANONICAL_MAP_SESSION_COLS,
    _OPTIONAL_BET_LDA_RUN_TRIP_COLS,
    _REQUIRED_BET_PARQUET_COLS,
    _SESSION_SELECT_COLS,
    BASE_DIR,
    CANONICAL_MAPPING_CUTOFF_JSON,
    CANONICAL_MAPPING_PARQUET,
    CHUNK_DIR,
    DATA_DIR,
    FEATURE_CANDIDATES_PATH,
    FEATURE_SPEC_PATH,
    HK_TZ,
    HISTORY_BUFFER_DAYS,
    LOCAL_PARQUET_DIR,
    MODEL_DIR,
    OUT_DIR,
    PROJECT_ROOT,
    TRAINER_DAYS,
    default_training_window,
    get_model_version,
    local_parquet_session_path_for_trainer,
    parse_window,
    trainer_local_parquet_bridge_manifest_path,
    _to_hk,
)
from trainer.training.identity_runtime import (  # noqa: E402
    _apply_cutoff_window_identity_fallback,
    _compute_canonical_map_duckdb_budget,
    attach_pit_identity_chunk_duckdb,
    attach_pit_identity_chunk_duckdb_legacy,
    build_canonical_links_and_dummy_from_duckdb,
    build_canonical_links_and_dummy_from_duckdb_legacy,
    build_pit_session_links_from_duckdb,
    build_pit_session_links_from_duckdb_legacy,
)

# Feature column lists are now from Feature Spec YAML (get_all_candidate_feature_ids /
# get_candidate_feature_ids). See feat-consolidation Step 3; no TRACK_B_FEATURE_COLS,
# LEGACY_FEATURE_COLS, or ALL_FEATURE_COLS here.
# HISTORY_BUFFER_DAYS is read from config (DEC-027) in the config block above.

# ---------------------------------------------------------------------------
# ClickHouse data loading (production path)
# ---------------------------------------------------------------------------

# Data ingress (ClickHouse + local Parquet) lives in trainer.training.data_sources
# (Issue #12 PR-12.2). Re-exported below to keep historical call sites working.
from trainer.training.data_sources import (  # noqa: E402
    load_clickhouse_data,
    load_local_parquet,
)


# ---------------------------------------------------------------------------
# player_profile loading (PLAN Step 4 / DEC-011)
# ---------------------------------------------------------------------------


def load_player_layer_asset_parquet(asset_path: Path) -> pd.DataFrame:
    """Load a pre-built player-layer Parquet for PIT join (WS5 asset path).

    Contract: must include ``snapshot_dtm`` (same as ``load_player_profile``).
    """
    if not asset_path.is_file():
        raise FileNotFoundError(f"TRAINER_PLAYER_LAYER_ASSET_PATH: not a file: {asset_path}")
    df = pd.read_parquet(asset_path)
    if "snapshot_dtm" not in df.columns:
        raise ValueError(
            f"TRAINER_PLAYER_LAYER_ASSET_PATH: missing required column 'snapshot_dtm' ({asset_path})"
        )
    return df


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


# Local Parquet metadata helpers extracted to data_sources (Issue #12 PR-12.2).
from trainer.training.data_sources import (  # noqa: E402
    _detect_local_data_end,
    _parquet_date_range,
    _parse_obj_to_date,
)


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
    session_path = local_parquet_session_path_for_trainer()
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
    requested_window_end = window_end.date()
    required_start = latest_month_end_on_or_before(window_start.date())
    required_end = requested_window_end

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
        required_end = min(required_end, session_rng[1], requested_window_end)

    if required_end > requested_window_end:
        logger.warning(
            "Profile required_end exceeded training window (%s > %s); clamping to avoid "
            "building unused future player_profile snapshots.",
            required_end,
            requested_window_end,
        )
        required_end = requested_window_end

    if required_start > required_end:
        logger.warning(
            "Profile auto-build skipped: effective required range is empty (%s > %s)",
            required_start,
            required_end,
        )
        return

    profile_rng = _parquet_date_range(profile_path, ["snapshot_date", "snapshot_dtm"])
    logger.info(
        "player_profile required coverage: window=%s->%s required=%s->%s "
        "session=%s profile=%s",
        window_start.date(),
        requested_window_end,
        required_start,
        required_end,
        session_rng,
        profile_rng,
    )
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
        miss_start = max(miss_start, required_start)
        miss_end = min(miss_end, required_end)
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

# Issue #12 PR-12.3: feature-pipeline coordination (DQ + Track Human attach)
# now lives in trainer.training.feature_pipeline. Re-exported below so the
# names ``apply_dq`` / ``add_run_state_machine_features`` keep resolving on
# trainer.training.trainer for all historic call sites.
from trainer.training.feature_pipeline import (  # noqa: E402
    add_run_state_machine_features,
    apply_dq,
)
from trainer.training.label_asset_cache import (  # noqa: E402
    build_label_disk_cache_components,
    label_asset_cache_disabled,
    label_disk_cache_fingerprint,
    label_intermediate_parquet_path,
    label_intermediate_sidecar_path,
    try_load_label_intermediate_cache,
    write_label_intermediate_cache,
)
from trainer.training.l2_bundle_materialize import read_bridge_source_snapshot_id  # noqa: E402


# ---------------------------------------------------------------------------
# Chunk processing
# ---------------------------------------------------------------------------

def _chunk_parquet_path(chunk: dict) -> Path:
    ws = chunk["window_start"].strftime("%Y%m%d")
    we = chunk["window_end"].strftime("%Y%m%d")
    return CHUNK_DIR / f"chunk_{ws}_{we}.parquet"


def apply_train_only_negative_downsampling(
    train_df: pd.DataFrame,
    *,
    neg_sample_frac: float,
    random_state: int,
) -> pd.DataFrame:
    """Downsample label=0 rows on the train split only (GitHub #19).

    Valid/test splits must remain full populations; chunk-level downsampling was removed.
    """
    if neg_sample_frac >= 1.0 - 1e-12 or train_df.empty or "label" not in train_df.columns:
        return train_df
    _pos_mask = train_df["label"] == 1
    _n_neg_before = int((~_pos_mask).sum())
    if _n_neg_before == 0:
        return train_df
    _neg_keep = train_df.loc[~_pos_mask].sample(
        frac=neg_sample_frac, random_state=random_state
    )
    out = pd.concat([train_df.loc[_pos_mask], _neg_keep], ignore_index=True)
    logger.info(
        "Step 7b train-only neg downsample: frac=%.4f rows %d->%d (neg %d->%d)",
        neg_sample_frac,
        len(train_df),
        len(out),
        _n_neg_before,
        len(_neg_keep),
    )
    if int((out["label"] == 0).sum()) == 0:
        logger.error(
            "train-only neg_sample_frac=%.4f removed ALL train negatives — "
            "increase NEG_SAMPLE_FRAC or NEG_SAMPLE_FRAC_MIN.",
            neg_sample_frac,
        )
    return out


def _apply_train_neg_downsample_to_parquet(
    train_path: Path,
    *,
    neg_sample_frac: float,
    random_state: int,
) -> None:
    """Rewrite on-disk train split Parquet after train-only negative downsampling."""
    df = pd.read_parquet(train_path)
    out = apply_train_only_negative_downsampling(
        df, neg_sample_frac=neg_sample_frac, random_state=random_state
    )
    tmp = train_path.with_suffix(".parquet.tmp")
    out.to_parquet(tmp, index=False)
    tmp.replace(train_path)


def _chunk_prefeatures_parquet_path(chunk: dict) -> Path:
    """Task 7 R6: Parquet of bets after run_state_machine, before bet_duckdb_window."""
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
_CHUNK_FINAL_SCHEMA_VERSION = "llmmerge2"


def _prefeatures_cache_components(components: dict) -> dict:
    """Task 7 R6: key material for post-Track-Human bets (LLM spec + neg-sample excluded)."""
    return {
        **components,
        "feature_spec_hash": _CHUNK_PREFEATURES_SPEC_PLACEHOLDER,
        "neg_sample_frac": 1.0,
    }


def _cross_layer_compose_closure_hash(
    feature_spec: Optional[dict],
    *,
    data_hash: str,
    profile_hash: str,
    cfg_hash: str,
) -> str:
    """Build closure hash for cross-layer compose nodes.

    Uses compose contract from the spec plus upstream layer hashes so changes in
    either dependencies or their source fingerprints invalidate chunk cache.
    """
    contract = get_cross_layer_compose_contract(feature_spec)
    if not contract:
        return "none"
    payload = {
        "contract": contract,
        "upstream": {
            "bet": str(data_hash),
            "run": hashlib.md5(f"{data_hash}|{cfg_hash}".encode()).hexdigest()[:8],
            "player": str(profile_hash),
            "trip": "none",
        },
    }
    return hashlib.md5(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:8]


def _validate_cross_layer_compose_inputs(
    df: pd.DataFrame,
    feature_spec: Optional[dict],
) -> None:
    """Fail-closed guard: all compose input columns must exist before compose."""
    contract = get_cross_layer_compose_contract(feature_spec)
    if not contract:
        return
    for fid, meta in contract.items():
        required = [str(c) for c in (meta.get("input_columns") or []) if str(c)]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(
                f"cross-layer compose '{fid}' missing input columns: {sorted(missing)}"
            )


def _validate_cross_layer_compose_outputs(
    df: pd.DataFrame,
    feature_spec: Optional[dict],
) -> None:
    """Fail-closed guard: compose outputs declared in spec must be materialized."""
    contract = get_cross_layer_compose_contract(feature_spec)
    if not contract:
        return
    for fid, meta in contract.items():
        outputs = [str(c) for c in (meta.get("output_columns") or []) if str(c)]
        targets = outputs or [fid]
        missing = [c for c in targets if c not in df.columns]
        if missing:
            raise ValueError(
                f"cross-layer compose '{fid}' missing output columns: {sorted(missing)}"
            )


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
    1. Skip if NEG_SAMPLE_FRAC_AUTO is False (default; GitHub #10 owns chunk-path OOM policy).
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
        logger.info("OOM-check: peak %.1f GB <= budget %.1f GB -- no adjustment", estimated_peak_ram / (1024**3), ram_budget / (1024**3))
        return current_frac

    # OOM is likely.
    if current_frac < 1.0:
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
    logger.warning(
        "OOM-check: auto-adjusting NEG_SAMPLE_FRAC 1.0 -> %.2f  (peak %.1f GB > budget %.1f GB)%s",
        auto_frac,
        estimated_peak_ram / (1024**3),
        ram_budget / (1024**3),
        (
            f"; floor hit at NEG_SAMPLE_FRAC_MIN={NEG_SAMPLE_FRAC_MIN} — consider fewer --days or a narrower --start/--end window"
            if _warn_floor
            else ""
        ),
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

    logger.info(
        "OOM-check (chunk 1 size): %d chunks x %.0f MB -> est. peak %.1f GB  budget %.1f GB",
        n_chunks, per_chunk_bytes / (1024**2), estimated_peak_ram / (1024**3), ram_budget / (1024**3),
    )

    if estimated_peak_ram <= ram_budget:
        logger.info("OOM-check (chunk 1 size): peak <= budget — no adjustment")
        return current_frac

    if current_frac < 1.0:
        logger.warning(
            "OOM-check (chunk 1 size): est. peak > budget but NEG_SAMPLE_FRAC=%.2f is user-set — not overriding",
            current_frac,
        )
        return current_frac

    needed_factor = ram_budget / estimated_peak_ram
    raw_frac = (needed_factor - p) / (1.0 - p)
    auto_frac = max(NEG_SAMPLE_FRAC_MIN, min(1.0, raw_frac))
    _warn_floor = raw_frac < NEG_SAMPLE_FRAC_MIN
    logger.warning(
        "OOM-check (chunk 1 size): auto-adjusting NEG_SAMPLE_FRAC 1.0 -> %.2f  (peak %.1f GB > budget %.1f GB)%s",
        auto_frac,
        estimated_peak_ram / (1024**3),
        ram_budget / (1024**3),
        "; floor hit — consider fewer chunks" if _warn_floor else "",
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
    identity_mapping_mode: str = "cutoff_window",
    pit_identity_engine: str = "cutoff_window_map",
    t_game_visible_time_column: str = "none",
    cross_layer_compose_hash: str = "none",
    feature_spec_for_cross_layer: Optional[dict] = None,
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
        "ALERT_HORIZON_MIN": int(ALERT_HORIZON_MIN),
        "LABEL_LOOKAHEAD_MIN": int(LABEL_LOOKAHEAD_MIN),
        "SESSION_AVAIL_DELAY_MIN": SESSION_AVAIL_DELAY_MIN,
        "BET_AVAIL_DELAY_MIN": BET_AVAIL_DELAY_MIN,
        "TABLE_HC_WINDOW_MIN": int(getattr(_cfg, "TABLE_HC_WINDOW_MIN", 30)),
        "HISTORY_BUFFER_DAYS": HISTORY_BUFFER_DAYS,
        "TRACK_HUMAN_LOOKBACK_HOURS": _effective_lookback,
        "RUN_STATE_MACHINE_LOOKBACK_HOURS": _effective_lookback,
        "identity_mapping_mode": str(identity_mapping_mode),
        "pit_identity_engine": str(pit_identity_engine),
        "t_game_features_enabled": bool(getattr(_cfg, "T_GAME_FEATURES_ENABLED", False)),
        "t_game_visible_time_column": str(t_game_visible_time_column),
    }, sort_keys=True)
    cfg_hash = hashlib.md5(cfg_str.encode()).hexdigest()[:6]
    feature_spec_cache_hash = str(feature_spec_hash)
    if feature_spec_cache_hash != _CHUNK_PREFEATURES_SPEC_PLACEHOLDER:
        feature_spec_cache_hash = f"{feature_spec_cache_hash}:{_CHUNK_FINAL_SCHEMA_VERSION}"
    _xlayer = str(cross_layer_compose_hash or "none").strip()
    if _xlayer == "auto":
        _xlayer = _cross_layer_compose_closure_hash(
            feature_spec_for_cross_layer,
            data_hash=str(data_hash),
            profile_hash=str(profile_hash),
            cfg_hash=str(cfg_hash),
        )
    if _xlayer and _xlayer != "none" and feature_spec_cache_hash != _CHUNK_PREFEATURES_SPEC_PLACEHOLDER:
        feature_spec_cache_hash = f"{feature_spec_cache_hash}:xcl{_xlayer}"
    return {
        "window_start": ws,
        "window_end": we,
        "data_hash": data_hash,
        "cfg_hash": cfg_hash,
        "profile_hash": profile_hash,
        "feature_spec_hash": feature_spec_cache_hash,
        "neg_sample_frac": float(neg_sample_frac),
    }


# Parquet fingerprint helpers extracted to data_sources (Issue #12 PR-12.2).
from trainer.training.data_sources import (  # noqa: E402
    _local_parquet_source_data_hash,
    _parquet_stable_rowgroups_schema_digest,
)


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
    *,
    identity_mapping_mode: str = "cutoff_window",
) -> Optional[Path]:
    """Materialize one training-window slice; return path to written Parquet or None if empty.

    ``canonical_map`` is the legacy cutoff-window map (``identity_mapping_mode=cutoff_window``).
    When ``identity_mapping_mode=pit_asof`` and local parquet is enabled,
    chunk-scoped DuckDB ASOF join attaches ``canonical_id`` per bet time (B3).
    dummy_player_ids: FND-12 dummy/fake-account player_ids to drop from training (TRN-04).
    profile_df: player_profile snapshot table for PIT join (PLAN Step 4/DEC-011).
        Pass None to skip; profile feature columns will be 0 for all rows.
    feature_spec: parsed feature spec (bet_duckdb_window) loaded by run_pipeline.
    feature_spec_hash: short hash of the feature spec used to compute bet-layer DuckDB window
        columns; included in the chunk cache key so spec changes bust cache.
    neg_sample_frac: ignored for chunk output (GitHub #19). Chunk Parquets are full;
        train-only negative downsampling runs after Step 7. Kept on the signature for
        OOM-probe call compatibility.

    Task 7 R6: pre-LLM Parquet cache (``*.prefeatures.parquet``) is **on by default**
    (``trainer.core.config.CHUNK_TWO_STAGE_CACHE_DEFAULT``) so run_state_machine can be skipped
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
    # (apply_dq, compute_labels, add_run_state_machine_features, label filter) receive
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
    _pit_engine = (
        "duckdb_chunk_asof"
        if str(identity_mapping_mode or "").strip().lower() == "pit_asof" and use_local_parquet
        else "cutoff_window_map"
    )
    _t_game_visible_col = (
        "__etl_insert_Dtm" if bool(getattr(_cfg, "T_GAME_FEATURES_ENABLED", False)) else "none"
    )

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
            neg_sample_frac=1.0,
            data_hash=_dh_local,
            identity_mapping_mode=identity_mapping_mode,
            pit_identity_engine=_pit_engine,
            t_game_visible_time_column=_t_game_visible_col,
            cross_layer_compose_hash="auto",
            feature_spec_for_cross_layer=feature_spec,
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
        _dh_clickhouse = _order_insensitive_bets_hash(bets_raw)
        _cache_components = _chunk_cache_components(
            chunk,
            None,
            profile_hash=_profile_hash,
            feature_spec_hash=feature_spec_hash,
            neg_sample_frac=1.0,
            data_hash=_dh_clickhouse,
            identity_mapping_mode=identity_mapping_mode,
            pit_identity_engine=_pit_engine,
            t_game_visible_time_column=_t_game_visible_col,
            cross_layer_compose_hash="auto",
            feature_spec_for_cross_layer=feature_spec,
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

    # --- Identity: attach canonical_id (cutoff-window vs PIT as-of, B3) ---
    _mode = str(identity_mapping_mode or "cutoff_window").strip().lower()
    _use_pit = (_mode == "pit_asof" and use_local_parquet)
    _pit_strict = (os.environ.get("TRAINER_PIT_IDENTITY_STRICT") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )
    if _use_pit:
        _sess_path = local_parquet_session_path_for_trainer()
        try:
            bets = attach_pit_identity_chunk_duckdb(
                bets_df=bets,
                session_parquet_path=_sess_path,
                observation_end=extended_end,
            )
            if "_pit_rated" not in bets.columns:
                _msg = (
                    f"Chunk {window_start.date()}–{window_end.date()}: PIT DuckDB merge missing _pit_rated"
                )
                if _pit_strict:
                    raise RuntimeError(
                        f"{_msg}; TRAINER_PIT_IDENTITY_STRICT=1 forbids cutoff_window fallback."
                    )
                logger.warning("%s; fallback to cutoff_window", _msg)
                _use_pit = False
        except Exception as _pit_exc:
            if _pit_strict:
                raise RuntimeError(
                    f"Chunk {window_start.date()}–{window_end.date()}: PIT DuckDB identity failed "
                    f"({_pit_exc!r}); TRAINER_PIT_IDENTITY_STRICT=1 forbids cutoff_window fallback."
                ) from _pit_exc
            logger.warning(
                "Chunk %s–%s: PIT DuckDB identity failed (%s); fallback to cutoff_window",
                window_start.date(),
                window_end.date(),
                _pit_exc,
            )
            _use_pit = False
    # Phase C PR-C2: unify identity / PIT prune behind evaluate_pit_admission.
    # Both pit_asof and cutoff_window paths now produce the same skip_reason_code
    # set (PIT_UNAVAILABLE_SOURCE / IDENTITY_UNMATCHED) instead of scattered
    # log lines, so trainer skip stats are directly comparable to scorer/backtester.
    if not _use_pit:
        bets = _apply_cutoff_window_identity_fallback(bets, canonical_map)
        _rated_for_prune: set = (
            set(canonical_map["canonical_id"].astype(str).unique())
            if not canonical_map.empty and "canonical_id" in canonical_map.columns
            else set()
        )
        if not _rated_for_prune:
            logger.warning(
                "Chunk %s–%s: canonical_map empty -> no rated rows; skip heavy FE",
                window_start.date(),
                window_end.date(),
            )
            return None
        bets["canonical_id"] = bets["canonical_id"].astype(str)
        _adm = evaluate_pit_admission(
            bets,
            rated_canonical_ids=_rated_for_prune,
        )
    else:
        bets["canonical_id"] = bets["canonical_id"].astype(str)
        _adm = evaluate_pit_admission(bets)
    bets = _adm.admitted
    _n_pit = int(_adm.skip_counts.get(SKIP_REASON_PIT_UNAVAILABLE_SOURCE, 0))
    _n_id = int(_adm.skip_counts.get(SKIP_REASON_IDENTITY_UNMATCHED, 0))
    if _adm.skipped_rows > 0:
        logger.info(
            "Chunk %s–%s: prediction_skip (mode=%s) total=%d "
            "PIT_UNAVAILABLE_SOURCE=%d IDENTITY_UNMATCHED=%d (admitted=%d)",
            window_start.date(),
            window_end.date(),
            "pit_asof" if _use_pit else "cutoff_window",
            _adm.skipped_rows,
            _n_pit,
            _n_id,
            _adm.admitted_rows,
        )
    if bets.empty:
        logger.warning(
            "Chunk %s–%s: empty after admission (mode=%s)",
            window_start.date(),
            window_end.date(),
            "pit_asof" if _use_pit else "cutoff_window",
        )
        return None

    # Global rated canonical_id universe (for is_rated flag; same cutoff map as Step 3).
    rated_ids: set = (
        set(canonical_map["canonical_id"].astype(str).unique())
        if not canonical_map.empty and "canonical_id" in canonical_map.columns
        else set()
    )

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
    _skip_run_state_machine = False
    if _two_stage and not force_recompute and _pref_path.exists():
        stored_pref = _pref_key_path.read_text(encoding="utf-8") if _pref_key_path.exists() else ""
        sk, sc = _read_chunk_cache_sidecar(stored_pref)
        if sk == _pref_key:
            logger.info(
                "Chunk %s–%s: prefeatures cache hit (key=%s), skipping run_state_machine",
                window_start.date(), window_end.date(), _pref_key,
            )
            bets = pd.read_parquet(_pref_path)
            _skip_run_state_machine = True
            _bump_chunk_cache_stat(chunk_cache_stats, "step6_chunk_cache_prefeatures_hit_total")
        else:
            miss_reasons = _chunk_cache_miss_reasons(sk, sc, _pref_comps)
            logger.info(
                "Chunk %s–%s: prefeatures cache stale (miss_reason=%s), recomputing run_state_machine",
                window_start.date(), window_end.date(), miss_reasons,
            )

    if not _skip_run_state_machine:
        bets = add_run_state_machine_features(bets, canonical_map, window_end, lookback_hours=_lookback_hours)
        if _two_stage:
            _bump_chunk_cache_stat(
                chunk_cache_stats, "step6_chunk_cache_prefeatures_run_state_machine_recompute_total",
            )
            _bump_chunk_cache_stat(
                chunk_cache_stats, "step6_chunk_cache_prefeatures_run_state_machine_recompute_total",
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
        # Phase B PR-B3: route through layered bet entrypoint (thin wrapper over
        # compute_bet_duckdb_window_features). Output identical.
        _bets_llm_result = compute_bet_duckdb_window_features(
            bets,
            feature_spec=feature_spec,
            cutoff_time=window_end,
        )
        _llm_cand_ids = [
            c.get("feature_id")
            for c in resolve_spec_track_section(feature_spec, "bet_duckdb_window").get("candidates", [])
        ]
        _bets_llm_feature_cols = [
            fid for fid in _llm_cand_ids
            if fid and fid in _bets_llm_result.columns and fid not in bets.columns
        ]
        if _bets_llm_feature_cols and "bet_id" in _bets_llm_result.columns:
            bets = bets.merge(
                _bets_llm_result[["bet_id"] + _bets_llm_feature_cols].drop_duplicates("bet_id"),
                on="bet_id",
                how="left",
            )
        logger.info(
            "Chunk %s–%s: bet_duckdb_window features computed (%.1fs)",
            window_start.date(),
            window_end.date(),
            time.perf_counter() - _t0_llm,
        )

    # B2 ``t_game`` join removed from training path (issue #34): game-level features
    # are dropped from the dev catalog; production timeliness is unreliable.

    # Trip layer (v0): optional ``lda_*`` passthrough contract; fail-closed when configured.
    _trip_fail_closed = bool(getattr(_cfg, "TRIP_LAYER_FAIL_CLOSED", False))
    bets = compute_trip_layer_features(bets, fail_closed=_trip_fail_closed)

    # --- Labels (C1 extended pull) + optional label_intermediate disk cache (L2-aligned) ---
    _label_disk_components = build_label_disk_cache_components(
        window_start_iso=window_start.isoformat(),
        window_end_iso=window_end.isoformat(),
        extended_end_iso=extended_end.isoformat(),
        data_hash=str(_cache_components["data_hash"]),
        walkaway_gap_min=int(WALKAWAY_GAP_MIN),
        alert_horizon_min=int(ALERT_HORIZON_MIN),
        label_lookahead_min=int(LABEL_LOOKAHEAD_MIN),
        identity_mapping_mode=str(identity_mapping_mode),
        pit_identity_engine=str(_pit_engine),
        source_snapshot_id=str(read_bridge_source_snapshot_id() or "unknown"),
    )
    _label_disk_components["bets_label_input_hash"] = _order_insensitive_bets_hash(bets)
    _label_disk_fp = label_disk_cache_fingerprint(_label_disk_components)
    _label_pq = label_intermediate_parquet_path(chunk, CHUNK_DIR)
    _label_key = label_intermediate_sidecar_path(chunk, CHUNK_DIR)
    labeled: Optional[pd.DataFrame] = None
    if not label_asset_cache_disabled() and not force_recompute:
        labeled = try_load_label_intermediate_cache(
            parquet_path=_label_pq,
            sidecar_path=_label_key,
            expected_fingerprint=_label_disk_fp,
            expected_components=_label_disk_components,
            expected_n_rows=len(bets),
        )
    if labeled is not None:
        logger.info(
            "Chunk %s–%s: label_intermediate cache hit (fp=%s)",
            window_start.date(),
            window_end.date(),
            _label_disk_fp,
        )
        _bump_chunk_cache_stat(chunk_cache_stats, "step6_label_asset_cache_hit_total")
        _bump_chunk_cache_stat(chunk_cache_stats, "step6_label_intermediate_cache_hit_total")
    else:
        _bump_chunk_cache_stat(chunk_cache_stats, "step6_label_asset_cache_miss_total")
        labeled = compute_labels(
            bets_df=bets,
            window_end=window_end,
            extended_end=extended_end,
        )
        if not label_asset_cache_disabled():
            write_label_intermediate_cache(
                labeled=labeled,
                parquet_path=_label_pq,
                sidecar_path=_label_key,
                components=_label_disk_components,
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
    # Phase B PR-B3: route through layered player entrypoint (thin wrapper).
    labeled = compute_player_layer_features(
        labeled,
        profile_df,
        feature_spec=feature_spec,
        use_local_parquet=use_local_parquet,
    )
    _validate_cross_layer_compose_inputs(labeled, feature_spec)
    labeled = add_wave2_personalized_baselines(labeled)
    _validate_cross_layer_compose_outputs(labeled, feature_spec)

    # Ensure all non-profile feature columns exist with numeric defaults.
    # R74: profile columns are intentionally left as NaN when a player has no
    # prior snapshot — LightGBM routes them to the trained default-child.
    # Blanket fillna(0) across all candidate cols would erase that signal.
    # R127-2: derive profile set from feature_spec/YAML (SSOT). When feature_spec is None
    # (e.g. YAML path missing), fallback to PROFILE_FEATURE_COLS from features.py; long-term
    # PLAN Step 3 may move this to load default YAML or reject run (Round 141 Review P1).
    _all_candidate_cols = get_all_candidate_feature_ids(feature_spec, screening_only=True) if feature_spec else list(PROFILE_FEATURE_COLS)
    _yaml_profile_set = (
        set(get_candidate_feature_ids(feature_spec, "player_run_asset", screening_only=False))
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

    # GitHub #19: negative downsampling is applied only to the train split after Step 7,
    # not here (chunk Parquets stay full valid/test populations).

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
    try:
        from trainer.training.layer_asset_store import write_chunk_layer_asset_manifest

        write_chunk_layer_asset_manifest(
            chunk_parquet_path=chunk_path,
            chunk=chunk,
            labeled_columns=labeled.columns,
            feature_spec=feature_spec,
            source_snapshot_id=read_bridge_source_snapshot_id(),
            pit_policy_id=str(identity_mapping_mode),
            pit_identity_engine=str(_pit_engine),
            row_count=len(labeled),
        )
    except Exception as _lam_exc:
        logger.warning(
            "Chunk %s–%s: layer asset manifest write failed (non-fatal): %s",
            window_start.date(),
            window_end.date(),
            _lam_exc,
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
# Legacy Plan B CSV cleanup (retired — LibSVM-only training)
# ---------------------------------------------------------------------------


def remove_legacy_plan_b_csv_exports(export_dir: Path) -> None:
    """Delete retired Plan B CSV artifacts so they cannot shadow LibSVM training."""
    export_dir = Path(export_dir)
    for name in ("train_for_lgb.csv", "valid_for_lgb.csv"):
        p = export_dir / name
        if p.is_file():
            try:
                p.unlink()
                logger.info("Removed legacy Plan B CSV: %s", p)
            except OSError as exc:
                logger.warning("Could not remove legacy CSV %s: %s", p, exc)


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


def _libsvm_normalize_parquet_sources(src: Union[Path, Sequence[Path]]) -> List[Path]:
    """Normalize LibSVM export sources to a non-empty path list."""
    if isinstance(src, Path):
        return [src]
    out = [Path(p) for p in src]
    if not out:
        raise ValueError("LibSVM export: parquet path list is empty")
    return out


def _libsvm_duckdb_read_parquet_expr(paths: Sequence[Path]) -> str:
    """Build DuckDB ``read_parquet`` expression for one or many files."""

    def _esc_path(s: str) -> str:
        return s.replace("'", "''")

    esc = [_esc_path(str(p.resolve())) for p in paths]
    if len(esc) == 1:
        return f"read_parquet('{esc[0]}')"
    inner = ", ".join(f"'{e}'" for e in esc)
    return f"read_parquet([{inner}])"


def _export_parquet_to_libsvm(
    train_path: Union[Path, Sequence[Path]],
    valid_path: Union[Path, Sequence[Path]],
    feature_cols: List[str],
    export_dir: Path,
    test_path: Optional[Union[Path, Sequence[Path]]] = None,
) -> Tuple[Path, Path, Optional[Path]]:
    """Stream export from split Parquets to LibSVM + .weight (PLAN B+ §4.3, 階段 6 第 3 步).

    Train: rated rows only; weight = 1/N_run (same as compute_sample_weights).
    Valid: rated rows only; no weight file.
    Test (optional): rated rows only; no weight file. When test_path is provided, writes test_for_lgb.libsvm.
    Does not load full train/valid/test into memory.
    *train_path* / *valid_path* / *test_path* may each be a single ``Path`` or a non-empty sequence
    of Parquet files (unioned by DuckDB) for day-sharded L2 bundles.
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
    train_paths = _libsvm_normalize_parquet_sources(train_path)
    valid_paths = _libsvm_normalize_parquet_sources(valid_path)
    for p in train_paths:
        if not p.exists():
            raise FileNotFoundError(f"Train Parquet not found: {p}")
    for p in valid_paths:
        if not p.exists():
            raise FileNotFoundError(f"Valid Parquet not found: {p}")

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
        _resolve_runtime = getattr(_cfg, "resolve_duckdb_runtime_policy", None)
        _apply_runtime = getattr(_cfg, "apply_duckdb_runtime", None)
        _available_bytes: Optional[int] = None
        try:
            import psutil as _psutil

            _available_bytes = int(_psutil.virtual_memory().available)
        except Exception:
            _available_bytes = None
        _input_bytes = int(sum(p.stat().st_size for p in train_paths) + sum(p.stat().st_size for p in valid_paths))
        if callable(_resolve_runtime) and callable(_apply_runtime):
            _policy = _resolve_runtime(
                "libsvm_export",
                _available_bytes,
                input_bytes=_input_bytes,
            )
            _apply_runtime(con, _policy)
        train_from = _libsvm_duckdb_read_parquet_expr(train_paths)
        valid_from = _libsvm_duckdb_read_parquet_expr(valid_paths)
        cols = ", ".join(_esc_col(c) for c in feature_cols)
        # Rated only; weight = 1/N_run per (canonical_id, run_id)
        train_sql = (
            f"SELECT label, {cols}, "
            "1.0 / COUNT(*) OVER (PARTITION BY canonical_id, run_id) AS _w "
            f"FROM {train_from} WHERE COALESCE(is_rated, false) = true"
        )
        valid_sql = (
            f"SELECT label, {cols} "
            f"FROM {valid_from} WHERE COALESCE(is_rated, false) = true"
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
                    # Keep LibSVM dimensionality stable only when tail feature is effectively zero.
                    _last = vals[-1] if nf > 0 else None
                    try:
                        _last_num = float(_last) if _last is not None else 0.0
                    except (TypeError, ValueError):
                        _last_num = 0.0
                    if nf > 0 and ((not math.isfinite(_last_num)) or _last_num == 0.0):
                        parts.append(f"{nf - 1}:0")
                    f_lib.write(" ".join(parts) + "\n")
                    f_w.write(f"{w}\n")
                    n_train += 1
        if n_train == 0:
            for _p in (train_libsvm_tmp, train_weight_tmp):
                try:
                    Path(_p).unlink(missing_ok=True)
                except OSError:
                    pass
            raise RuntimeError(
                "LibSVM export produced 0 rated train rows (check is_rated / Parquet split)."
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
                    # Keep LibSVM dimensionality stable only when tail feature is effectively zero.
                    _last = vals[-1] if nf > 0 else None
                    try:
                        _last_num = float(_last) if _last is not None else 0.0
                    except (TypeError, ValueError):
                        _last_num = 0.0
                    if nf > 0 and ((not math.isfinite(_last_num)) or _last_num == 0.0):
                        parts.append(f"{nf - 1}:0")
                    f_lib.write(" ".join(parts) + "\n")
                    n_valid += 1
        os.replace(valid_libsvm_tmp, valid_libsvm)

        n_test_rows = 0
        test_paths_list: Optional[List[Path]] = None
        if test_path is not None:
            test_paths_list = [p for p in _libsvm_normalize_parquet_sources(test_path) if p.is_file()]
        if test_paths_list:
            test_libsvm = export_dir / "test_for_lgb.libsvm"
            test_libsvm_tmp = export_dir / "test_for_lgb.libsvm.tmp"
            test_from = _libsvm_duckdb_read_parquet_expr(test_paths_list)
            test_sql = (
                f"SELECT label, {cols} "
                f"FROM {test_from} WHERE COALESCE(is_rated, false) = true"
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
                        # Keep LibSVM dimensionality stable only when tail feature is effectively zero.
                        _last = vals[-1] if nf > 0 else None
                        try:
                            _last_num = float(_last) if _last is not None else 0.0
                        except (TypeError, ValueError):
                            _last_num = 0.0
                        if nf > 0 and ((not math.isfinite(_last_num)) or _last_num == 0.0):
                            parts.append(f"{nf - 1}:0")
                        f_lib.write(" ".join(parts) + "\n")
                        n_test_rows += 1
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
        logger.debug(
            "Post-export train LibSVM scan failed for %s: %s",
            train_libsvm,
            _scan_e,
            exc_info=True,
        )

    if test_libsvm is not None:
        logger.info(
            "Exported LibSVM for Plan B+: train %s (%d rows + weight), valid %s (%d rows), test %s (%d rows)",
            train_libsvm, n_train, valid_libsvm, n_valid, test_libsvm, n_test_rows,
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
    try:
        write_split_manifest(
            export_dir,
            train_libsvm=train_libsvm,
            valid_libsvm=valid_libsvm,
            test_libsvm=test_libsvm,
            feature_columns=list(feature_cols),
            train_row_count=int(n_train),
            valid_row_count=int(n_valid),
            test_row_count=int(n_test_rows) if test_libsvm is not None else None,
        )
    except OSError as _manifest_exc:
        logger.warning(
            "LibSVM export: could not write split manifest (%s); continuing without manifest.",
            _manifest_exc,
        )
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
    """Resolve ``TRAINER_DEVICE_MODE``, optional ``--lgbm-device`` override, and GPU probe."""
    global _EFFECTIVE_LIGHTGBM_DEVICE, _LIGHTGBM_GPU_FALLBACK_USED
    global _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS, _CLI_LIGHTGBM_DEVICE_OVERRIDE
    global _REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS
    global _LAST_GBM_BACKEND_EFFECTIVE_DEVICE, _GBM_BACKEND_GPU_FALLBACK_USED

    unified_req = str(TRAINER_DEVICE_MODE).strip().lower()
    if unified_req not in ("auto", "cpu", "gpu"):
        unified_req = "auto"
    _REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS = unified_req
    _LAST_GBM_BACKEND_EFFECTIVE_DEVICE = "cpu"
    _GBM_BACKEND_GPU_FALLBACK_USED = False

    raw = getattr(args, "lgbm_device", None)
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        _CLI_LIGHTGBM_DEVICE_OVERRIDE = None
    else:
        s = str(raw).strip().lower()
        if s not in ("cpu", "gpu"):
            raise SystemExit("Invalid --lgbm-device %r; use cpu or gpu." % (raw,))
        _CLI_LIGHTGBM_DEVICE_OVERRIDE = s
        logger.warning(
            "--lgbm-device is deprecated and overrides LightGBM only for this run. "
            "Prefer TRAINER_DEVICE_MODE=auto|cpu|gpu (unifies LightGBM + A3 CatBoost/XGBoost)."
        )

    if _CLI_LIGHTGBM_DEVICE_OVERRIDE is not None:
        lgb_intent = _CLI_LIGHTGBM_DEVICE_OVERRIDE
    elif unified_req == "cpu":
        lgb_intent = "cpu"
    elif unified_req == "gpu":
        lgb_intent = "gpu"
    else:
        lgb_intent = "gpu"

    if _CLI_LIGHTGBM_DEVICE_OVERRIDE is not None:
        _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS = _CLI_LIGHTGBM_DEVICE_OVERRIDE
    elif unified_req == "auto":
        _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS = "auto"
    elif unified_req == "cpu":
        _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS = "cpu"
    else:
        _REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS = "gpu"

    _LIGHTGBM_GPU_FALLBACK_USED = False

    if lgb_intent == "cpu":
        _EFFECTIVE_LIGHTGBM_DEVICE = "cpu"
        logger.info(
            "Trainer device mode: requested=%s; LightGBM effective=cpu (CPU-only path).",
            unified_req,
        )
        return

    if _lightgbm_gpu_probe_ok():
        _EFFECTIVE_LIGHTGBM_DEVICE = "gpu"
        logger.info(
            "Trainer device mode: requested=%s; LightGBM effective=gpu (OpenCL probe ok).",
            unified_req,
        )
        return

    _EFFECTIVE_LIGHTGBM_DEVICE = "cpu"
    if unified_req == "gpu" or _CLI_LIGHTGBM_DEVICE_OVERRIDE == "gpu":
        _LIGHTGBM_GPU_FALLBACK_USED = True
        logger.warning(
            "LightGBM: GPU requested but probe failed; using cpu for this run "
            "(fix OpenCL/driver or set TRAINER_DEVICE_MODE=auto|cpu, or pass --lgbm-device cpu)."
        )
    else:
        logger.info(
            "Trainer device mode: requested=%s; LightGBM effective=cpu (GPU probe failed or unavailable).",
            unified_req,
        )


def _lgb_dataset_params_for_pipeline() -> dict[str, Any]:
    """Params for ``lgb.Dataset(..., params=...)`` that LightGBM locks at Dataset construction.

    If these differ from values passed later to ``lgb.train``, LightGBM raises e.g.
    "Cannot change feature_pre_filter after constructed Dataset handle."
    """
    return {"feature_pre_filter": False}


def _lgb_params_for_pipeline() -> dict:
    """LightGBM params shared by Optuna, final fit, and lgb.train (device-aware)."""
    dev = _EFFECTIVE_LIGHTGBM_DEVICE
    out: dict[str, Any] = {
        "objective": "binary",
        "class_weight": "balanced",
        "verbose": -1,
        "random_state": 42,
        "device_type": dev,
        # Optuna may pick min_child_samples below Dataset construction defaults; with
        # feature_pre_filter=true, lgb.train then raises (LibSVM / .bin reload path).
        **_lgb_dataset_params_for_pipeline(),
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
    global _LAST_GBM_BACKEND_EFFECTIVE_DEVICE, _GBM_BACKEND_GPU_FALLBACK_USED

    visible_gpu_ids = discover_visible_gpu_ids()
    requested_mode = str(TRAINER_DEVICE_MODE).strip().lower()
    if requested_mode not in ("auto", "cpu", "gpu"):
        requested_mode = "auto"

    effective_mode = requested_mode
    if requested_mode == "auto":
        effective_mode = "gpu" if visible_gpu_ids else "cpu"
    elif requested_mode == "gpu" and not visible_gpu_ids:
        logger.warning(
            "TRAINER_DEVICE_MODE=gpu but no CUDA-visible GPUs were found for CatBoost/XGBoost; using cpu."
        )
        effective_mode = "cpu"

    _GBM_BACKEND_GPU_FALLBACK_USED = bool(requested_mode == "gpu" and effective_mode == "cpu")
    _LAST_GBM_BACKEND_EFFECTIVE_DEVICE = str(effective_mode)

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
        "trainer_device_mode_requested": requested_mode,
        "requested_backend_device_mode": requested_mode,
        "effective_backend_device_mode": effective_mode,
        "gbm_backend_gpu_fallback_used": bool(_GBM_BACKEND_GPU_FALLBACK_USED),
        "visible_gpu_ids": list(visible_gpu_ids),
        "gpu_assignments": dict(gpu_assignments),
        "backend_runtime_by_name": backend_runtime_by_name,
        "parallel_backend_workers": int(parallel_backend_workers),
        "parallel_backend_execution": bool(parallel_backend_execution),
    }


def _base_lgb_params() -> dict:
    """Backward-compat alias for :func:`_lgb_params_for_pipeline`."""
    return _lgb_params_for_pipeline()


def _split_window_hours_from_payout_df(df: Optional[pd.DataFrame]) -> Optional[float]:
    """Time span in hours from ``payout_complete_dtm`` min/max on a split (train/val/test).

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


def _val_window_hours_from_payout_df(df: Optional[pd.DataFrame]) -> Optional[float]:
    """Backward-compatible alias for :func:`_split_window_hours_from_payout_df`."""
    return _split_window_hours_from_payout_df(df)


def _split_window_hours_from_parquet_payout(path: Path) -> Optional[float]:
    """Min/max ``payout_complete_dtm`` span in hours via DuckDB (no full-parquet load)."""
    if not path.exists():
        return None
    import duckdb

    p = str(path.resolve()).replace("'", "''")
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            f"SELECT min(payout_complete_dtm) AS mn, max(payout_complete_dtm) AS mx "
            f"FROM read_parquet('{p}')"
        ).fetchone()
    except Exception:
        return None
    finally:
        con.close()
    if row is None or row[0] is None or row[1] is None:
        return None
    mn = pd.to_datetime(row[0], errors="coerce")
    mx = pd.to_datetime(row[1], errors="coerce")
    if pd.isna(mn) or pd.isna(mx):
        return None
    span_sec = float((mx - mn).total_seconds())
    if not math.isfinite(span_sec) or span_sec <= 0.0:
        return None
    return span_sec / 3600.0




def pick_dec026_threshold_from_binary_scores(
    y_true: Union[np.ndarray, pd.Series],
    y_score: np.ndarray,
    *,
    recall_floor: Optional[float],
    min_alert_count: int,
    min_alerts_per_hour: Optional[float],
    window_hours: Optional[float],
    fbeta_beta: float,
) -> Dec026ThresholdPick:
    """Run :func:`pick_threshold_dec026` on parallel binary labels and scores (any score surface).

    Used for A4 fused-score calibration so the same DEC-026 / density guards apply as
    for stage-1 validation picks.
    """
    y_arr = np.asarray(y_true, dtype=float).reshape(-1)
    s_arr = np.asarray(y_score, dtype=np.float64).reshape(-1)
    return pick_threshold_dec026(
        y_arr,
        s_arr,
        recall_floor=recall_floor,
        min_alert_count=min_alert_count,
        min_alerts_per_hour=min_alerts_per_hour,
        window_hours=window_hours,
        fbeta_beta=fbeta_beta,
    )


def _snapshot_stage1_datasets_for_v2(metrics: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy train/val/test split fragments for ``training_metrics.v2`` stage1_datasets."""
    train: Dict[str, Any] = {}
    val: Dict[str, Any] = {}
    test: Dict[str, Any] = {}
    for k, v in metrics.items():
        if not isinstance(k, str):
            continue
        if k.startswith("train_"):
            train[k[len("train_") :]] = v
        elif k.startswith("val_"):
            val[k[len("val_") :]] = v
        elif k.startswith("test_"):
            test[k[len("test_") :]] = v
    for noisy in ("field_test_primary_score", "field_test_primary_score_mode"):
        val.pop(noisy, None)
    out: Dict[str, Any] = {}
    if train:
        out["train"] = train
    if val:
        out["val"] = val
    if test:
        out["test"] = test
    return out


def _update_val_field_test_primary_keys_from_val_labels(
    metrics: MutableMapping[str, Any],
    y_val: Union[pd.Series, np.ndarray],
) -> None:
    """Set val_field_test_primary_score* from current val_precision and validation labels."""
    val_precision = metrics.get("val_precision")
    val_np_ratio = _neg_pos_ratio_from_binary_labels(y_val)
    val_primary_adj = _precision_prod_adjusted(
        float(val_precision) if val_precision is not None else None,
        production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
        test_neg_pos_ratio=val_np_ratio,
    )
    metrics["val_neg_pos_ratio"] = val_np_ratio
    metrics["val_field_test_primary_score"] = (
        float(val_primary_adj) if val_primary_adj is not None else float(val_precision or 0.0)
    )
    metrics["val_field_test_primary_score_mode"] = (
        "precision_prod_adjusted" if val_primary_adj is not None else "precision_raw"
    )




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
    wh = _split_window_hours_from_payout_df(val_df)
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


def _write_skipped_optuna_manifest_for_libsvm(
    sink: Optional[list[dict[str, Any]]],
    *,
    run_optuna: bool,
    skipped_reason: Optional[str] = None,
) -> None:
    """Record effective Optuna status when LibSVM path does not run in-memory HPO."""
    runtime_manifest = _backend_runtime_manifest("lightgbm")
    if not run_optuna:
        _reason = "run_optuna_false"
        _objective_mode = "disabled_by_run_flag"
    else:
        _reason = skipped_reason or "libsvm_path_uses_default_hyperparams"
        if _reason == "libsvm_no_validation_for_hpo":
            _objective_mode = "skipped_libsvm_no_validation_for_hpo"
        elif _reason == "libsvm_no_positives_in_validation":
            _objective_mode = "skipped_libsvm_no_positives_in_validation"
        elif _reason == "libsvm_optuna_gate_blocked":
            _objective_mode = "skipped_libsvm_optuna_gate_blocked"
        elif _reason == "libsvm_no_train_rows_for_hpo":
            _objective_mode = "skipped_libsvm_no_train_rows_for_hpo"
        else:
            _objective_mode = "skipped_libsvm_default_params"
    _write_optuna_hpo_manifest(
        sink,
        {
            "optuna_hpo_backend": "lightgbm",
            "optuna_hpo_enabled": False,
            "optuna_hpo_effective_enabled": False,
            "optuna_hpo_backend_device_mode": runtime_manifest["backend_device_mode"],
            "optuna_hpo_backend_gpu_id": runtime_manifest["backend_gpu_id"],
            "optuna_hpo_n_trials_requested": (
                int(OPTUNA_N_TRIALS) if isinstance(OPTUNA_N_TRIALS, int) else None
            ),
            "optuna_hpo_timeout_seconds": (
                int(OPTUNA_TIMEOUT_SECONDS)
                if isinstance(OPTUNA_TIMEOUT_SECONDS, int) and OPTUNA_TIMEOUT_SECONDS > 0
                else None
            ),
            "optuna_hpo_early_stop_patience": (
                int(OPTUNA_EARLY_STOP_PATIENCE)
                if isinstance(OPTUNA_EARLY_STOP_PATIENCE, int)
                and OPTUNA_EARLY_STOP_PATIENCE > 0
                else None
            ),
            "optuna_hpo_study_best_trial_value": None,
            "optuna_hpo_objective_mode": _objective_mode,
            "optuna_hpo_skipped_reason": _reason,
        },
    )




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


def _fit_lightgbm_hpo_scores_from_libsvm(
    params: dict[str, Any],
    *,
    train_libsvm: Path,
    valid_libsvm: Path,
    train_row_count: int,
    feature_names: Sequence[str],
) -> np.ndarray:
    """One Optuna trial: train LightGBM from LibSVM paths only (no dense train matrix)."""
    from trainer.training.gbm_bakeoff_disk import ensure_train_weight_f32_memmap

    w_mm, _n = ensure_train_weight_f32_memmap(
        Path(train_libsvm), expected_rows=int(train_row_count)
    )
    ds_params = _lgb_dataset_params_for_pipeline()
    hp_lgb: dict[str, Any] = {**_lgb_params_for_pipeline()}
    for k, v in params.items():
        if k == "n_estimators":
            continue
        hp_lgb[k] = v
    num_boost = max(1, int(params.get("n_estimators", 400)))
    fn = [str(x) for x in feature_names]
    dtrain = lgb.Dataset(
        str(train_libsvm),
        weight=w_mm,
        feature_name=fn,
        params=ds_params,
    )
    dvalid = lgb.Dataset(
        str(valid_libsvm),
        reference=dtrain,
        feature_name=fn,
        params=ds_params,
    )
    booster = lgb.train(
        hp_lgb,
        dtrain,
        num_boost_round=num_boost,
        valid_sets=[dvalid],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(-1)],
    )
    pred = booster.predict(dvalid)
    return np.asarray(pred, dtype=np.float64).reshape(-1)


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
        fit_params = _sanitize_catboost_params_for_runtime(fit_params)
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
    libsvm_disk_hpo: Optional[Tuple[Path, Path, int, Tuple[str, ...]]] = None,
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

    _libsvm_disk_hpo = libsvm_disk_hpo is not None
    _disk_tr_p: Optional[Path] = None
    _disk_va_p: Optional[Path] = None
    _disk_tr_rows = 0
    _disk_feats: Tuple[str, ...] = ()
    if _libsvm_disk_hpo:
        _dt0, _dv0, _dn0, _df0 = libsvm_disk_hpo
        _disk_tr_p = Path(_dt0)
        _disk_va_p = Path(_dv0)
        _disk_tr_rows = int(_dn0)
        _disk_feats = tuple(str(x) for x in _df0)
        if backend_n not in ("lightgbm", "catboost", "xgboost"):
            raise ValueError(
                f"libsvm_disk_hpo is only supported for lightgbm/catboost/xgboost, not {backend_n!r}"
            )

    # R705: guard against empty validation input — return empty dict (base params)
    # rather than crashing inside LightGBM or average_precision_score.
    # LibSVM disk HPO: labels come from the valid LibSVM file (X_val may be empty).
    _y_val_ref = (
        _labels_from_libsvm(_disk_va_p)
        if _libsvm_disk_hpo and _disk_va_p is not None
        else y_val
    )
    _val_empty = (not _libsvm_disk_hpo and (X_val.empty or len(y_val) == 0)) or (
        _libsvm_disk_hpo and len(_y_val_ref) == 0
    )
    if _val_empty:
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
    if (
        not _libsvm_disk_hpo
        and _sample_rows is not None
        and len(X_train) > _sample_rows
    ):
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

    if _libsvm_disk_hpo:
        y_vl = _y_val_ref
        logger.info(
            "Optuna HPO[%s]: disk LibSVM validation rows=%d (no in-memory HPO subsampling).",
            backend_n,
            len(y_vl),
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
        if _libsvm_disk_hpo and _disk_tr_p is not None and _disk_va_p is not None:
            if backend_n == "lightgbm":
                scores = _fit_lightgbm_hpo_scores_from_libsvm(
                    params,
                    train_libsvm=_disk_tr_p,
                    valid_libsvm=_disk_va_p,
                    train_row_count=_disk_tr_rows,
                    feature_names=_disk_feats,
                )
            else:
                from trainer.training.gbm_bakeoff_disk import (
                    hpo_trial_val_scores_catboost_from_libsvm,
                    hpo_trial_val_scores_xgboost_from_libsvm,
                    libsvm_bundle_for_a3_hpo,
                )

                _hpo_b = libsvm_bundle_for_a3_hpo(
                    _disk_tr_p,
                    _disk_va_p,
                    train_row_count=int(_disk_tr_rows),
                    feature_names=_disk_feats,
                )
                if backend_n == "xgboost":
                    scores = hpo_trial_val_scores_xgboost_from_libsvm(
                        params,
                        _hpo_b,
                        y_val,
                        backend_runtime_params=backend_runtime_params,
                        use_external_memory=bool(
                            getattr(_cfg, "GBM_BAKEOFF_XGBOOST_EXTERNAL_MEMORY", False)
                        ),
                    )
                elif backend_n == "catboost":
                    scores = hpo_trial_val_scores_catboost_from_libsvm(
                        params,
                        _hpo_b,
                        y_val,
                        backend_runtime_params=backend_runtime_params,
                        quantize_first=bool(getattr(_cfg, "GBM_BAKEOFF_CATBOOST_QUANTIZE", False)),
                    )
                else:
                    raise ValueError(f"Unexpected backend for libsvm_disk_hpo: {backend_n!r}")
        else:
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
        else _tqdm_bar(total=n_trials_eff, desc=f"Step 9/11 Optuna {backend_n}", unit="trial")
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
                "[Step 9/11] Optuna (%s[%s]) trial %d/%d  %s=%s  elapsed %.0fs (%.1f min)",
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
                "[Step 9/11] Optuna early stop (%s[%s]): no improvement for %d trials (stopped at trial %d/%d)",
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
    best = _backend_hpo_defaults(backend_n)
    try:
        best.update(dict(study.best_params))
    except ValueError:
        logger.warning(
            "Optuna (%s[%s]) completed no successful trials; returning backend defaults.",
            label or "model",
            backend_n,
        )
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
    logger.info(
        "[Step 9/11] A3 investigate: Optuna HPO finished backend=%s label=%s; "
        "returning hyperparams to caller for final model fit.",
        backend_n,
        label or "rated",
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
    if _libsvm_disk_hpo and _disk_tr_p is not None and _disk_va_p is not None:
        _manifest_pay["optuna_hpo_data_source"] = "libsvm_disk"
        _manifest_pay["optuna_hpo_train_libsvm"] = str(_disk_tr_p.resolve())
        _manifest_pay["optuna_hpo_valid_libsvm"] = str(_disk_va_p.resolve())
        _manifest_pay["optuna_hpo_train_row_count"] = int(_disk_tr_rows)
    else:
        _manifest_pay["optuna_hpo_data_source"] = "in_memory_dense"
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
    libsvm_disk_hpo: Optional[Tuple[Path, Path, int, Tuple[str, ...]]] = None,
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
        libsvm_disk_hpo=libsvm_disk_hpo,
    )


# ---------------------------------------------------------------------------
# Dual-model training
# ---------------------------------------------------------------------------

_ENV_DISABLE_FINAL_REFIT = "TRAINER_DISABLE_FINAL_REFIT_TRAIN_VALID"


def _lgb_refit_tree_count(model: lgb.LGBMClassifier) -> int:
    """Return boosting rounds to use when refitting on train+valid (post early stopping)."""
    bi = getattr(model, "best_iteration_", None)
    if bi is not None and int(bi) > 0:
        return int(bi)
    booster = getattr(model, "booster_", None)
    if booster is not None:
        try:
            nt = int(booster.num_trees())
            if nt > 0:
                return nt
        except Exception:
            pass
    return max(1, int(model.get_params().get("n_estimators", 100)))


def _final_refit_lgbm_sklearn_on_train_valid(
    model: lgb.LGBMClassifier,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    sw_train: pd.Series,
    X_val: pd.DataFrame,
    y_val: pd.Series,
    hyperparams: dict,
    *,
    label: str = "",
) -> lgb.LGBMClassifier:
    """Refit LightGBM on train ∪ valid using the same tree budget as *model* (pipeline §12).

    Threshold / val metrics remain from the train/valid selection phase; test is
    evaluated only afterward. Skipped when validation is empty, when disabled via
    ``TRAINER_DISABLE_FINAL_REFIT_TRAIN_VALID``, or when *model* is not an
    ``LGBMClassifier`` (caller should skip for wrappers / other backends).
    """
    if os.environ.get(_ENV_DISABLE_FINAL_REFIT, "").strip().lower() in ("1", "true", "yes"):
        return model
    if X_val.empty or len(y_val) == 0:
        return model
    n_trees = _lgb_refit_tree_count(model)
    merged_hp = {**dict(hyperparams), "n_estimators": n_trees}
    params = {**_lgb_params_for_pipeline(), **merged_hp}
    final = lgb.LGBMClassifier(**params)
    X_tv = pd.concat([X_train, X_val], axis=0, ignore_index=True)
    y_tv = pd.concat([y_train, y_val], axis=0, ignore_index=True)
    sw_tr = sw_train.astype(float).reset_index(drop=True)
    sw_v = pd.Series(np.ones(len(X_val), dtype=np.float64))
    sw_tv = pd.concat([sw_tr, sw_v], axis=0, ignore_index=True)
    final.fit(X_tv, y_tv, sample_weight=sw_tv)
    if label:
        logger.info(
            "%s: final refit on train+valid rows=%d n_estimators=%d (pipeline §12)",
            label,
            int(len(X_tv)),
            n_trees,
        )
    return final


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

        _vw_h = _split_window_hours_from_payout_df(vl_df) if name == "rated" else None
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
        if not X_vl.empty and len(y_vl) > 0:
            model = _final_refit_lgbm_sklearn_on_train_valid(
                model,
                X_tr,
                y_tr,
                sw,
                X_vl,
                y_vl,
                hp,
                label=name,
            )
            _fr_off = os.environ.get(_ENV_DISABLE_FINAL_REFIT, "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            metrics["final_refit_train_valid"] = not _fr_off
        else:
            metrics["final_refit_train_valid"] = False

        if _optuna_hpo_manifest_loop:
            metrics.update(_optuna_hpo_manifest_loop[0])

        _train_wh_dual = _split_window_hours_from_payout_df(tr_df)
        _test_wh_dual = _split_window_hours_from_payout_df(te_df) if not te_df.empty else None

        # Training set performance (for overfit / fit quality reporting).
        metrics.update(
            _compute_train_metrics(
                model,
                metrics["threshold"],
                X_tr,
                y_tr,
                label=name,
                train_window_hours=_train_wh_dual,
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
                    test_window_hours=_test_wh_dual,
                )
            )

        _obj_dual = _field_test_hpo_min_alerts_per_hour_for_reports()
        metrics["field_test_min_alerts_per_hour_objective"] = float(_obj_dual)
        _vwh_dual = _split_window_hours_from_payout_df(vl_df)
        _vs_val_dual: Optional[np.ndarray] = None
        if not vl_df.empty:
            _miss_vd = [c for c in avail_cols if c not in vl_df.columns]
            if not _miss_vd:
                try:
                    _x_vl_dual = _dataframe_for_lgb_predict(model, vl_df, avail_cols)
                    _vs_val_dual = _batched_model_positive_class_scores(
                        model,
                        _x_vl_dual,
                        TRAIN_METRICS_PREDICT_BATCH_ROWS,
                    )
                except Exception:
                    _vs_val_dual = None
        metrics.update(
            _split_alert_density_prefixed_dict(
                "val",
                scores=_vs_val_dual,
                threshold=float(metrics["threshold"]),
                window_hours=_vwh_dual,
                objective_min=_obj_dual,
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


def train_issue8_high_roller_segmented_bundle(
    *,
    step7_train_path: Path,
    step7_valid_path: Path,
    step7_test_path: Optional[Path],
    active_feature_cols: List[str],
    export_base: Path,
    run_optuna: bool,
    ranking_recipe: Optional[str],
    gbm_bakeoff: bool,
) -> Optional[Tuple[dict, dict]]:
    """Issue #8: train separate rated models for high/low theo segments (train-only).

    Each segment uses **LightGBM only** (``gbm_bakeoff=False``); pipeline
    ``gbm_bakeoff`` is ignored for the two segment fits to save time and RAM.
    Min-rows fallback still respects the caller's ``gbm_bakeoff``.

    Returns ``(rated_art, combined_metrics)`` when segmentation completes.

    Returns ``None`` only when Issue #8 is disabled in config (caller uses legacy
    single-path ``train_single_rated_model``). When enabled, any prerequisite or
    segment train failure raises ``RuntimeError`` (no silent fallback).
    """
    from trainer.training.high_roller_segmentation import (
        compute_high_roller_cutoff_from_train_parquet,
        count_rated_rows_parquet,
        materialize_segment_parquet_splits,
        parquet_has_column,
        routed_test_metrics_payload,
        validate_high_roller_theo_nonempty_on_rated_train,
    )

    if not bool(getattr(_core_trainer_config, "HIGH_ROLLER_SEGMENT_ENABLE", False)):
        return None

    theo_col = str(getattr(_core_trainer_config, "HIGH_ROLLER_THEO_FEATURE", "theo_win_sum_30d"))
    q = float(getattr(_core_trainer_config, "HIGH_ROLLER_QUANTILE", 0.90))
    min_h = int(getattr(_core_trainer_config, "HIGH_ROLLER_MIN_ROWS_HIGH", 500))
    min_l = int(getattr(_core_trainer_config, "HIGH_ROLLER_MIN_ROWS_LOW", 500))
    primary_seg = str(
        getattr(_core_trainer_config, "HIGH_ROLLER_PRIMARY_SEGMENT_FOR_SERVING", "low")
    ).strip().lower()
    if primary_seg not in ("low", "high"):
        primary_seg = "low"

    if not parquet_has_column(step7_train_path, theo_col):
        raise RuntimeError(
            f"Issue #8 high-roller segmentation requires column {theo_col!r} on "
            f"{step7_train_path} (missing or unreadable)."
        )
    validate_high_roller_theo_nonempty_on_rated_train(step7_train_path, theo_col)

    cutoff, cut_meta = compute_high_roller_cutoff_from_train_parquet(
        step7_train_path, theo_col, q
    )
    n_rated_train = int(cut_meta.get("high_roller_rated_train_row_count", 0))
    n_low_prev = int(cut_meta.get("high_roller_segment_train_rated_rows_low", 0))
    n_high_prev = int(cut_meta.get("high_roller_segment_train_rated_rows_high", 0))
    if n_low_prev == 0 or n_high_prev == 0:
        raise RuntimeError(
            "Issue #8 segmentation is degenerate at the configured quantile: "
            f"rated train splits as high={n_high_prev}, low={n_low_prev} "
            f"(cutoff={cutoff!r} on COALESCE({theo_col}, 0.0) at quantile={q}). "
            "This usually means the theo feature is constant on rated rows, or so "
            "left-skewed that the quantile equals the minimum so the low segment is "
            "empty. Fix or replace HIGH_ROLLER_THEO_FEATURE data, lower "
            "HIGH_ROLLER_QUANTILE, or set HIGH_ROLLER_SEGMENT_ENABLE=False in "
            "trainer/core/_config_high_roller_segmentation.py for legacy single-model "
            "training."
        )
    if n_low_prev < min_l or n_high_prev < min_h:
        raise RuntimeError(
            "Issue #8 segmentation cannot proceed: rated train rows per segment "
            "below minimum **before** writing segment Parquets "
            f"(high={n_high_prev}, required>={min_h}; low={n_low_prev}, required>={min_l}; "
            f"rated_train_total={n_rated_train}). Adjust splits, quantile, "
            "HIGH_ROLLER_MIN_ROWS_*, or disable HIGH_ROLLER_SEGMENT_ENABLE."
        )

    hr_root = Path(export_base) / "hr_segment"
    if hr_root.exists():
        shutil.rmtree(hr_root, ignore_errors=True)
    hr_root.mkdir(parents=True, exist_ok=True)

    high_parq_dir = hr_root / "parquet_high"
    low_parq_dir = hr_root / "parquet_low"
    materialize_segment_parquet_splits(
        step7_train_path,
        step7_valid_path,
        step7_test_path,
        theo_col,
        cutoff,
        "high",
        high_parq_dir,
    )
    materialize_segment_parquet_splits(
        step7_train_path,
        step7_valid_path,
        step7_test_path,
        theo_col,
        cutoff,
        "low",
        low_parq_dir,
    )

    high_train_p, high_valid_p, high_test_p = (
        high_parq_dir / "train_segment.parquet",
        high_parq_dir / "valid_segment.parquet",
        high_parq_dir / "test_segment.parquet",
    )
    low_train_p, low_valid_p, low_test_p = (
        low_parq_dir / "train_segment.parquet",
        low_parq_dir / "valid_segment.parquet",
        low_parq_dir / "test_segment.parquet",
    )

    n_h = count_rated_rows_parquet(high_train_p)
    n_l = count_rated_rows_parquet(low_train_p)
    seg_audit: Dict[str, Any] = {
        **cut_meta,
        "high_roller_segment_enable": True,
        "high_roller_train_rated_rows_high": n_h,
        "high_roller_train_rated_rows_low": n_l,
        "high_roller_min_rows_high": min_h,
        "high_roller_min_rows_low": min_l,
    }

    if n_h < min_h or n_l < min_l:
        raise RuntimeError(
            "Issue #8 segmentation cannot proceed: rated train rows per segment below "
            f"minimum (high={n_h}, required>={min_h}; low={n_l}, required>={min_l}). "
            "Adjust splits, quantile, or HIGH_ROLLER_MIN_ROWS_* in config."
        )

    if gbm_bakeoff:
        logger.info(
            "Issue #8 segmented training: disabling A3 gbm_bakeoff for high/low segments "
            "(LightGBM only); pipeline had gbm_bakeoff=%s",
            gbm_bakeoff,
        )

    high_export = hr_root / "libsvm_high"
    low_export = hr_root / "libsvm_low"
    high_export.mkdir(parents=True, exist_ok=True)
    low_export.mkdir(parents=True, exist_ok=True)

    _htrain, _hvalid, _htest = _export_parquet_to_libsvm(
        high_train_p,
        high_valid_p,
        active_feature_cols,
        high_export,
        test_path=high_test_p if high_test_p.is_file() else None,
    )
    _ltrain, _lvalid, _ltest = _export_parquet_to_libsvm(
        low_train_p,
        low_valid_p,
        active_feature_cols,
        low_export,
        test_path=low_test_p if low_test_p.is_file() else None,
    )

    train_high_df = pd.read_parquet(high_train_p)
    train_low_df = pd.read_parquet(low_train_p)

    high_art, _, cm_high = train_single_rated_model(
        train_high_df,
        None,
        active_feature_cols,
        run_optuna=run_optuna,
        test_df=None,
        train_libsvm_paths=(_htrain, _hvalid),
        test_libsvm_path=_htest,
        ranking_recipe=ranking_recipe,
        gbm_bakeoff=False,
        valid_split_parquet_path=high_valid_p,
        test_split_parquet_path=high_test_p if high_test_p.is_file() else None,
        train_split_parquet_path=high_train_p,
    )
    low_art, _, cm_low = train_single_rated_model(
        train_low_df,
        None,
        active_feature_cols,
        run_optuna=run_optuna,
        test_df=None,
        train_libsvm_paths=(_ltrain, _lvalid),
        test_libsvm_path=_ltest,
        ranking_recipe=ranking_recipe,
        gbm_bakeoff=False,
        valid_split_parquet_path=low_valid_p,
        test_split_parquet_path=low_test_p if low_test_p.is_file() else None,
        train_split_parquet_path=low_train_p,
    )

    if high_art is None or low_art is None:
        raise RuntimeError(
            "Issue #8 segmentation cannot proceed: train_single_rated_model returned empty "
            f"artifact (high_art is None={high_art is None}, low_art is None={low_art is None})."
        )

    m_h = cm_high.get("rated") if isinstance(cm_high, dict) else {}
    m_l = cm_low.get("rated") if isinstance(cm_low, dict) else {}
    if not isinstance(m_h, dict):
        m_h = {}
    if not isinstance(m_l, dict):
        m_l = {}

    routed: Dict[str, Any] = {}
    if step7_test_path is not None and Path(step7_test_path).is_file():
        try:
            import pyarrow.parquet as pq

            _cols_use = [c for c in active_feature_cols if c not in ("label",)]
            _read_cols = ["label", "is_rated", theo_col] + [c for c in _cols_use if c != theo_col]
            _read_cols = list(dict.fromkeys(_read_cols))
            _avail = set(pq.read_schema(step7_test_path).names)
            _cols_ok = [c for c in _read_cols if c in _avail]
            if "label" not in _cols_ok or "is_rated" not in _cols_ok:
                raise ValueError("test parquet missing label or is_rated")
            _test_mini = pd.read_parquet(step7_test_path, columns=_cols_ok)
            routed = routed_test_metrics_payload(
                test_df=_test_mini,
                theo_col=theo_col,
                cutoff=cutoff,
                feature_cols=active_feature_cols,
                model_high=high_art["model"],
                model_low=low_art["model"],
                thr_high=float(high_art["threshold"]),
                thr_low=float(low_art["threshold"]),
            )
        except Exception as _rte:
            raise RuntimeError(
                f"Issue #8 segmentation cannot proceed: routed test metrics failed "
                f"({step7_test_path}): {_rte}"
            ) from _rte

    primary_art, secondary_art = (low_art, high_art) if primary_seg == "low" else (high_art, low_art)
    primary_metrics = primary_art["metrics"]
    secondary_metrics = secondary_art["metrics"]

    rated_art = {
        "model": primary_art["model"],
        "threshold": primary_art["threshold"],
        "features": primary_art["features"],
        "metrics": primary_metrics,
        "model_kind": primary_art.get("model_kind", primary_metrics.get("model_kind")),
        "reason_codes_enabled": bool(primary_art.get("reason_codes_enabled", True)),
        "component_backends": list(primary_art.get("component_backends") or []),
        "a4_enabled": bool(primary_metrics.get("a4_enabled", False)),
        "a4_fusion_mode": primary_art.get("a4_fusion_mode", A4_FUSION_MODE_PRODUCT),
        "a4_candidate_cutoff": primary_art.get("a4_candidate_cutoff"),
        "a4_stage1_threshold_before_final_calibration": primary_art.get(
            "a4_stage1_threshold_before_final_calibration"
        ),
        "stage2_model": primary_art.get("stage2_model"),
        "stage2_features": list(primary_art.get("stage2_features") or primary_art.get("features") or []),
        "high_roller_segmentation": {
            "schema_version": "issue8_v1",
            **seg_audit,
            "primary_segment": primary_seg,
            "secondary_segment": "high" if primary_seg == "low" else "low",
            "high_model_threshold": float(high_art["threshold"]),
            "low_model_threshold": float(low_art["threshold"]),
            "high_segment_metrics": m_h,
            "low_segment_metrics": m_l,
            "secondary_artifact": {
                "model": secondary_art["model"],
                "threshold": secondary_art["threshold"],
                "features": secondary_art["features"],
                "metrics": secondary_metrics,
            },
            "overall_weighted": routed,
        },
    }

    combined_metrics: Dict[str, Any] = {
        "rated": primary_metrics,
        "segment_high": m_h,
        "segment_low": m_l,
        "high_roller_segmentation": rated_art["high_roller_segmentation"],
    }
    return rated_art, combined_metrics


def train_single_rated_model(
    train_df: pd.DataFrame,
    valid_df: Optional[pd.DataFrame],
    feature_cols: List[str],
    run_optuna: bool = True,
    test_df: Optional[pd.DataFrame] = None,
    train_libsvm_paths: Optional[Tuple[Path, Path]] = None,
    test_libsvm_path: Optional[Path] = None,
    ranking_recipe: Optional[str] = None,
    gbm_bakeoff: bool = False,
    valid_split_parquet_path: Optional[Path] = None,
    test_split_parquet_path: Optional[Path] = None,
    train_split_parquet_path: Optional[Path] = None,
) -> Tuple[Optional[dict], Optional[dict], dict]:
    """Train one rated artifact entry and return ``(rated_art, None, metrics)``.

    Only rows where is_rated==True are used for training, validation, and test
    evaluation.  Non-rated observations are intentionally excluded (DEC-009/010).

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
    LibSVM disk training remains valid for LightGBM, but no longer suppress A3.

    *train_split_parquet_path* (Plan B+): optional Step-7 train split parquet used only
    for ``payout_complete_dtm`` span when computing train alert-density without loading
    the full train frame.
    """
    _ft_pre_doc: Optional[Dict[str, Any]] = None
    _ft_pre_path_raw = ""
    _optuna_hpo_manifest: list[dict[str, Any]] = []
    if valid_df is None:
        valid_df = pd.DataFrame()
    if test_df is None:
        test_df = pd.DataFrame()
    # B+: valid/test may be on disk only; load minimal rated validation early so
    # DEC-026 val_window_hours, Optuna, and _rated_field_test_val_pick_per_hour_kwargs
    # match the in-memory / A3 parquet path.
    if (
        valid_df.empty
        and valid_split_parquet_path is not None
        and valid_split_parquet_path.exists()
        and feature_cols
    ):
        try:
            valid_df = _load_rated_eval_split_from_parquet(
                valid_split_parquet_path, list(feature_cols)
            )
            logger.info(
                "Plan B+: loaded rated validation from parquet for metrics/HPO (%d rows): %s",
                len(valid_df),
                valid_split_parquet_path,
            )
        except Exception as exc_val_parq:
            logger.warning(
                "Plan B+: could not load validation parquet (non-fatal): %s",
                exc_val_parq,
            )
    use_from_libsvm = False
    if train_libsvm_paths is not None:
        _t, _v = train_libsvm_paths
        if not _t.is_file() or not _v.is_file():
            raise FileNotFoundError(
                "LibSVM-only: train or valid LibSVM missing "
                f"(train={_t}, valid={_v})."
            )
        use_from_libsvm = True
        train_libsvm_p, valid_libsvm_p = train_libsvm_paths  # type: ignore[misc]
        with open(train_libsvm_p, encoding="utf-8") as _f:
            _n_lines = sum(1 for _ in _f)
        if _n_lines < 1:
            raise RuntimeError(
                f"LibSVM-only: train LibSVM has 0 data lines (path={train_libsvm_p})."
            )
        with open(train_libsvm_p, encoding="utf-8") as _f:
            _labels = [line.split(None, 1)[0] for line in _f if line.strip()]
        if len(set(_labels)) < 2:
            raise RuntimeError(
                f"LibSVM-only: train LibSVM has only one class (path={train_libsvm_p})."
            )

    if use_from_libsvm and trainer_file_backed_strict_enabled():
        if train_libsvm_paths is None:
            raise RuntimeError("internal: use_from_libsvm without train_libsvm_paths")
        _st_chk, _sv_chk = train_libsvm_paths
        validate_libsvm_paths_exist(
            Path(_st_chk),
            Path(_sv_chk),
            test_libsvm=Path(test_libsvm_path) if test_libsvm_path else None,
        )
        if test_df is not None and not test_df.empty:
            if test_libsvm_path is None or not Path(test_libsvm_path).is_file():
                raise FileNotFoundError(
                    "TRAINER_FILE_BACKED_STRICT: test_df is non-empty but test_libsvm_path "
                    "is missing or not a file; export test LibSVM or omit test_df."
                )

    libsvm_optuna_search_ran = False

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
        if not tr.empty:
            coerce_feature_dtypes(tr, feature_names)
        if not vr.empty:
            coerce_feature_dtypes(vr, feature_names)
        if tr.empty:
            # L2 bundle + LibSVM: train rows stay on disk (empty placeholder frame) but
            # A3/bakeoff still need X_tr/y_tr schema aligned with screened feature names.
            X_tr = pd.DataFrame(columns=list(feature_names))
            y_tr = pd.Series(dtype=float)
        else:
            X_tr = tr[feature_names]
            y_tr = tr["label"]
        if not vr.empty:
            _missing_v = [c for c in feature_names if c not in vr.columns]
            if _missing_v:
                raise KeyError(
                    "validation frame missing feature columns for train views: "
                    f"missing={_missing_v[:20]}{'...' if len(_missing_v) > 20 else ''}"
                )
            X_vl = vr[feature_names]
        else:
            X_vl = pd.DataFrame(columns=list(feature_names))
        if not isinstance(y_vl, np.ndarray):
            y_vl = vr["label"] if not vr.empty else y_tr.head(0)
        _train_views_ready = True

    if not use_from_libsvm and _get_train_rated().empty:
        logger.warning("rated model: no training rows, skipping")
        return None, None, {"rated": None}

    recipe_use = resolve_ranking_recipe(ranking_recipe)
    _snap_val_scores_holder: list[Optional[np.ndarray]] = [None]
    _valid_cols = valid_df.columns if not valid_df.empty else pd.Index([])
    if use_from_libsvm:
        # LibSVM was built for ``feature_cols``; LightGBM ``feature_name`` / num_feature
        # must match sparse indices even when in-memory ``valid_df`` omits columns.
        avail_cols = list(feature_cols)
    else:
        avail_cols = [c for c in feature_cols if c in _get_train_rated().columns]
        if len(_valid_cols) > 0:
            avail_cols = [c for c in avail_cols if c in _valid_cols]

    _skip_views_for_strict_disk_hpo = (
        trainer_file_backed_strict_enabled()
        and use_from_libsvm
        and run_optuna
        and _get_train_rated().empty
    )
    if (
        (not use_from_libsvm)
        or gbm_bakeoff
        or A4_TWO_STAGE_ENABLE_TRAINING
        or (use_from_libsvm and run_optuna and not _skip_views_for_strict_disk_hpo)
    ):
        _ensure_inmemory_train_views(avail_cols)

    _train_rated_for_weights = _get_train_rated()
    sw_base = (
        compute_sample_weights(_train_rated_for_weights)
        if not _train_rated_for_weights.empty
        else pd.Series(dtype=float)
    )
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
    # B+ LibSVM: full train stays on disk for lgb.Dataset, but Optuna may run on an in-memory
    # sample (see run_optuna_search hpo_sample_rows) when run_optuna is True.
    _default_hp = {
        "n_estimators": 400,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": 8,
        "min_child_samples": 20,
    }
    if use_from_libsvm:
        _vl_hpo = _get_val_rated()
        _strict_libsvm_optuna = trainer_file_backed_strict_enabled() and run_optuna
        _tls_o, _vls_o = train_libsvm_paths  # type: ignore[misc]
        _libsvm_disk_hpo_arg: Optional[Tuple[Path, Path, int, Tuple[str, ...]]] = None
        _yvl_pos = 0.0
        if _strict_libsvm_optuna:
            _y_file = np.asarray(_labels_from_libsvm(Path(_vls_o)), dtype=float)
            _yvl_pos = float(np.sum(_y_file == 1.0))
            _neg_ct = int(np.sum(_y_file == 0.0))
            _pos_ct = int(np.sum(_y_file == 1.0))
            _can_run_optuna_here = (
                len(_y_file) >= int(MIN_VALID_TEST_ROWS)
                and _pos_ct >= 1
                and _neg_ct >= 1
            )
            if _can_run_optuna_here:
                _libsvm_disk_hpo_arg = (
                    Path(_tls_o),
                    Path(_vls_o),
                    int(_n_lines),
                    tuple(str(c) for c in avail_cols),
                )
        else:
            if not _vl_hpo.empty and "label" in _vl_hpo.columns:
                _yvl_pos = float(
                    pd.to_numeric(_vl_hpo["label"], errors="coerce").fillna(0).sum()
                )
            _can_run_optuna_here = (
                run_optuna
                and not _get_train_rated().empty
                and not _vl_hpo.empty
                and _yvl_pos > 0
            )

        if run_optuna and _can_run_optuna_here:
            _val_wh = _val_window_hours_from_payout_df(_vl_hpo)
            _ft_hpo_active = _ft_allowed and _val_wh is not None
            log_optuna_precondition_context(
                _ft_pre_doc, uses_field_test_hpo_objective=_ft_hpo_active
            )
            try:
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
                    libsvm_disk_hpo=_libsvm_disk_hpo_arg,
                )
                libsvm_optuna_search_ran = True
            except RuntimeError as _opt_exc:
                logger.warning(
                    "rated LibSVM path: Optuna aborted (%s); using default hyperparameters.",
                    _opt_exc,
                )
                _write_skipped_optuna_manifest_for_libsvm(
                    _optuna_hpo_manifest,
                    run_optuna=True,
                    skipped_reason="libsvm_optuna_gate_blocked",
                )
                hp = dict(_default_hp)
            if not hp:
                hp = dict(_default_hp)
        else:
            _skip_sr: Optional[str] = None
            if run_optuna:
                if _strict_libsvm_optuna:
                    _yf = np.asarray(_labels_from_libsvm(Path(_vls_o)), dtype=float)
                    if len(_yf) < int(MIN_VALID_TEST_ROWS):
                        _skip_sr = "libsvm_strict_valid_too_small"
                    elif int(np.sum(_yf == 1.0)) < 1:
                        _skip_sr = "libsvm_strict_valid_no_positives"
                    elif int(np.sum(_yf == 0.0)) < 1:
                        _skip_sr = "libsvm_strict_valid_no_negatives"
                elif _get_train_rated().empty:
                    _skip_sr = "libsvm_no_train_rows_for_hpo"
                elif _vl_hpo.empty:
                    _skip_sr = "libsvm_no_validation_for_hpo"
                elif _yvl_pos <= 0:
                    _skip_sr = "libsvm_no_positives_in_validation"
            _write_skipped_optuna_manifest_for_libsvm(
                _optuna_hpo_manifest,
                run_optuna=bool(run_optuna),
                skipped_reason=_skip_sr,
            )
            hp = dict(_default_hp)
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
        _libsvm_temp_to_remove: Optional[Path] = None
        _lgb_ds_params = _lgb_dataset_params_for_pipeline()
        _lgb_train_bin_cache_hit = bool(_bin_path.is_file())
        if _bin_path.is_file():
            dtrain = lgb.Dataset(str(_bin_path), params=_lgb_ds_params)
            dvalid = lgb.Dataset(
                str(valid_libsvm_p),
                reference=dtrain,
                feature_name=list(avail_cols),
                params=_lgb_ds_params,
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
                params=_lgb_ds_params,
            )
            dvalid = lgb.Dataset(
                str(valid_libsvm_p),
                reference=dtrain,
                feature_name=list(avail_cols),
                params=_lgb_ds_params,
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
                    dtrain.save_binary(str(_bin_path))
                    logger.info("Plan B+: saved train Dataset to %s", _bin_path)
                except OSError as _e:
                    logger.warning(
                        "Plan B+: failed to save train Dataset to %s (%s); continuing without .bin.",
                        _bin_path,
                        _e,
                    )
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
        avail_cols = _lgb_booster_feature_name_list(booster)
        _did_strict_libsvm_refit = False
        _strict_tv_paths: list[Path] = []
        if (
            trainer_file_backed_strict_enabled()
            and _has_val_from_file
            and os.environ.get(_ENV_DISABLE_FINAL_REFIT, "").strip().lower()
            not in ("1", "true", "yes")
        ):
            from trainer.training.gbm_bakeoff_disk import ensure_train_weight_f32_memmap as _ensure_tv_w

            _tv_dir = train_libsvm_p.parent
            _fd_m, _p_m = tempfile.mkstemp(prefix=".tv_merge_", suffix=".libsvm", dir=str(_tv_dir))
            os.close(_fd_m)
            _merged_tv = Path(_p_m)
            _merged_tv_w = Path(str(_merged_tv) + ".weight")
            try:
                _n_merged = merge_libsvm_files(_merged_tv, [Path(train_libsvm_p), Path(valid_libsvm_p)])
                merge_train_valid_weight_files(
                    _merged_tv_w,
                    train_weight_txt=Path(str(train_libsvm_p) + ".weight"),
                    valid_libsvm=Path(valid_libsvm_p),
                )
                _w_tv, _ = _ensure_tv_w(_merged_tv, expected_rows=int(_n_merged))
                _strict_tv_paths.extend(
                    [
                        _merged_tv,
                        _merged_tv_w,
                        Path(str(_merged_tv) + ".weight.f32"),
                    ]
                )
                _n_round_tv = max(1, int(booster.best_iteration))
                _d_tv = lgb.Dataset(
                    str(_merged_tv),
                    weight=_w_tv,
                    feature_name=list(avail_cols),
                    params=_lgb_ds_params,
                )
                booster = lgb.train(hp_lgb, _d_tv, num_boost_round=_n_round_tv)
                _did_strict_libsvm_refit = True
                avail_cols = _lgb_booster_feature_name_list(booster)
                logger.info(
                    "rated Plan B+ LibSVM strict: file-backed final refit train∪valid rows=%d num_boost_round=%d",
                    int(_n_merged),
                    int(_n_round_tv),
                )
            except Exception as _tv_exc:
                raise RuntimeError(
                    "TRAINER_FILE_BACKED_STRICT: LibSVM train∪valid file-backed final refit failed "
                    f"({_tv_exc})"
                ) from _tv_exc
            finally:
                for _p in _strict_tv_paths:
                    try:
                        _p.unlink(missing_ok=True)
                    except OSError:
                        pass
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
                if valid_df is None or valid_df.empty:
                    _snap_val_scores_holder[0] = np.asarray(
                        val_scores, dtype=np.float64
                    ).reshape(-1).copy()
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
            "diagnostic_libsvm_optuna_search_ran": bool(libsvm_optuna_search_ran),
            "final_refit_train_valid": (
                bool(_did_strict_libsvm_refit)
                if trainer_file_backed_strict_enabled()
                else "skipped_libsvm_on_disk"
            ),
            "training_data_contract": (
                "file_backed_libsvm_strict"
                if trainer_file_backed_strict_enabled()
                else "file_backed_libsvm"
            ),
            "lgb_train_dataset_bin_cache_hit": bool(_lgb_train_bin_cache_hit),
        }
        if _optuna_hpo_manifest:
            _h0 = _optuna_hpo_manifest[0]
            for _hk in (
                "optuna_hpo_data_source",
                "optuna_hpo_train_libsvm",
                "optuna_hpo_valid_libsvm",
            ):
                if _hk in _h0:
                    metrics[_hk] = _h0[_hk]
        if _ft_thr_wh is not None and _ft_thr_mah is not None:
            metrics["val_dec026_pick_window_hours"] = float(_ft_thr_wh)
            metrics["val_dec026_pick_min_alerts_per_hour"] = float(_ft_thr_mah)
        model = _BoosterWrapper(booster)
        if _libsvm_temp_to_remove is not None and _libsvm_temp_to_remove.exists():
            _libsvm_temp_to_remove.unlink()

    if not use_from_libsvm:
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
        if not X_vl.empty and len(y_vl) > 0:
            model = _final_refit_lgbm_sklearn_on_train_valid(
                model,
                X_tr,
                y_tr,
                sw_rated,
                X_vl,
                y_vl,
                hp,
                label="rated",
            )
            _fr_off_r = os.environ.get(_ENV_DISABLE_FINAL_REFIT, "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            metrics["final_refit_train_valid"] = not _fr_off_r
        else:
            metrics["final_refit_train_valid"] = False

    train_thr = cast(float, metrics["threshold"])
    _train_wh_rate = _split_window_hours_from_payout_df(_get_train_rated())
    if _train_wh_rate is None and train_split_parquet_path is not None and train_split_parquet_path.exists():
        _train_wh_rate = _split_window_hours_from_parquet_payout(train_split_parquet_path)
    _test_df_for_wh = _get_test_rated()
    _test_wh_rate: Optional[float] = None
    if _test_df_for_wh is not None and not _test_df_for_wh.empty:
        _test_wh_rate = _split_window_hours_from_payout_df(_test_df_for_wh)
    if _test_wh_rate is None and test_split_parquet_path is not None and test_split_parquet_path.exists():
        _test_wh_rate = _split_window_hours_from_parquet_payout(test_split_parquet_path)
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
                        train_window_hours=_train_wh_rate,
                    )
                    used_libsvm_train_metrics = True
                except Exception as exc:
                    if trainer_file_backed_strict_enabled():
                        raise RuntimeError(
                            "TRAINER_FILE_BACKED_STRICT: train metrics via LibSVM file failed "
                            f"({exc})"
                        ) from exc
                    logger.warning(
                        "Plan B+: train metrics via LibSVM file failed (%s); "
                        "falling back to batched in-memory predict.",
                        exc,
                    )
    if not used_libsvm_train_metrics:
        if trainer_file_backed_strict_enabled() and use_from_libsvm:
            raise RuntimeError(
                "TRAINER_FILE_BACKED_STRICT: train metrics require LibSVM file predict "
                "(train path under DATA_DIR and non-empty labels)."
            )
        _ensure_inmemory_train_views(avail_cols)
        X_tr_pred = _dataframe_for_lgb_predict(model, _get_train_rated(), avail_cols)
        train_m = _compute_train_metrics(
            model,
            train_thr,
            X_tr_pred,
            y_tr,
            label="rated",
            log_results=False,
            train_window_hours=_train_wh_rate,
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
                test_window_hours=_test_wh_rate,
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
                        test_window_hours=_test_wh_rate,
                    )
                    metrics.update(test_m)
    else:
        test_m = {}

    _obj_rate = _field_test_hpo_min_alerts_per_hour_for_reports()
    metrics["field_test_min_alerts_per_hour_objective"] = float(_obj_rate)
    _vdf_rate = _get_val_rated()
    _vwh_rate = _split_window_hours_from_payout_df(_vdf_rate)
    if _vwh_rate is None and valid_split_parquet_path is not None and valid_split_parquet_path.exists():
        _vwh_rate = _split_window_hours_from_parquet_payout(valid_split_parquet_path)
    _vs_for_val_rate = _snap_val_scores_holder[0]
    if _vs_for_val_rate is None and not _vdf_rate.empty:
        _miss_vc = [c for c in avail_cols if c not in _vdf_rate.columns]
        if not _miss_vc:
            try:
                _xv_rate = _dataframe_for_lgb_predict(model, _vdf_rate, avail_cols)
                _vs_for_val_rate = _batched_model_positive_class_scores(
                    model,
                    _xv_rate,
                    TRAIN_METRICS_PREDICT_BATCH_ROWS,
                )
            except Exception as _vscr_exc:
                logger.warning(
                    "rated: validation alert-density scores failed (non-fatal): %s",
                    _vscr_exc,
                )
                _vs_for_val_rate = None
    metrics.update(
        _split_alert_density_prefixed_dict(
            "val",
            scores=_vs_for_val_rate,
            threshold=float(metrics["threshold"]),
            window_hours=_vwh_rate,
            objective_min=_obj_rate,
        )
    )

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
            _a3_eval_valid_source = "memory"
            if (
                (_compare_valid is None or _compare_valid.empty)
                and valid_split_parquet_path is not None
                and valid_split_parquet_path.exists()
            ):
                _compare_valid = _load_rated_eval_split_from_parquet(
                    valid_split_parquet_path,
                    avail_cols,
                )
                _a3_eval_valid_source = "parquet"
            _compare_test = test_rated
            _a3_eval_test_source = "memory"
            if (
                (_compare_test is None or _compare_test.empty)
                and test_split_parquet_path is not None
                and test_split_parquet_path.exists()
            ):
                _compare_test = _load_rated_eval_split_from_parquet(
                    test_split_parquet_path,
                    avail_cols,
                )
                _a3_eval_test_source = "parquet"

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

            _compare_valid_for_span = (
                _compare_valid
                if _compare_valid is not None
                else pd.DataFrame()
            )
            _bake_val_wh, _bake_val_mah = _rated_field_test_val_pick_per_hour_kwargs(
                label="rated",
                field_test_constrained_optuna_objective_allowed=_ft_allowed,
                val_df=_compare_valid_for_span,
            )

            _bake_libsvm_bundle = None
            if (
                use_from_libsvm
                and train_libsvm_paths is not None
                and bool(getattr(_cfg, "GBM_BAKEOFF_FROM_FILE", True))
            ):
                try:
                    from trainer.training.gbm_bakeoff_disk import (
                        GbmBakeoffLibSvmBundle,
                        bakeoff_cache_dir,
                    )

                    _tp, _vp = train_libsvm_paths
                    _test_p = (
                        test_libsvm_path
                        if test_libsvm_path is not None and test_libsvm_path.exists()
                        else None
                    )
                    _tmp_bundle = GbmBakeoffLibSvmBundle(
                        train_libsvm=Path(_tp),
                        valid_libsvm=Path(_vp),
                        test_libsvm=Path(_test_p) if _test_p is not None else None,
                        feature_names=tuple(str(c) for c in avail_cols),
                        train_row_count=int(_n_lines),
                        cache_dir=Path(_tp).parent,
                    )
                    _bake_libsvm_bundle = GbmBakeoffLibSvmBundle(
                        train_libsvm=_tmp_bundle.train_libsvm,
                        valid_libsvm=_tmp_bundle.valid_libsvm,
                        test_libsvm=_tmp_bundle.test_libsvm,
                        feature_names=_tmp_bundle.feature_names,
                        train_row_count=_tmp_bundle.train_row_count,
                        cache_dir=bakeoff_cache_dir(Path(_tp).parent, _tmp_bundle),
                    )
                except Exception as _bundle_exc:
                    if trainer_file_backed_strict_enabled():
                        raise RuntimeError(
                            "TRAINER_FILE_BACKED_STRICT: A3 LibSVM-disk bundle build failed "
                            f"({_bundle_exc})"
                        ) from _bundle_exc
                    logger.warning(
                        "A3 LibSVM-disk bundle not used (falling back to in-memory optional backends): %s",
                        _bundle_exc,
                    )
                    _bake_libsvm_bundle = None

            logger.info(
                "[Step 9/11] A3 investigate: calling train_and_select_rated_gbm_family "
                "(train_rated_rows=%d n_features=%d libsvm_bundle=%s)",
                len(_get_train_rated()),
                len(avail_cols),
                _bake_libsvm_bundle is not None,
            )
            _winner_backend, _winner_art, _bake_report = train_and_select_rated_gbm_family(
                X_tr,
                y_tr,
                _x_vl_cmp,
                _y_vl_cmp,
                sw_rated,
                hp,
                rated_train_df=_get_train_rated(),
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
                val_dec026_window_hours=_bake_val_wh,
                val_dec026_min_alerts_per_hour=_bake_val_mah,
                libsvm_bundle=_bake_libsvm_bundle,
            )
            logger.info(
                "[Step 9/11] A3 investigate: train_and_select_rated_gbm_family returned "
                "winner_backend=%s (merging metrics, then A4 if enabled).",
                _winner_backend,
            )
            model = _winner_art["model"]
            metrics = dict(_winner_art["metrics"])
            metrics["a3_eval_valid_source"] = _a3_eval_valid_source
            metrics["a3_eval_test_source"] = _a3_eval_test_source
            metrics["gbm_bakeoff"] = _bake_report
            metrics["trainer_file_backed_strict"] = bool(trainer_file_backed_strict_enabled())
            metrics["a3_libsvm_disk_bundle_used"] = bool(_bake_libsvm_bundle is not None)
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

    logger.info(
        "[Step 9/11] A3 investigate: post A3/bakeoff path checkpoint "
        "selected_backend=%s selected_backend_source=%s; entering A4 / rest of Step 9.",
        metrics.get("selected_backend"),
        metrics.get("selected_backend_source"),
    )
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
                    _val_rated_eval = _get_val_rated()
                    metrics["a4_valid_eval_source"] = "memory"
                    if (
                        (_val_rated_eval is None or _val_rated_eval.empty)
                        and valid_split_parquet_path is not None
                        and valid_split_parquet_path.exists()
                    ):
                        try:
                            _val_rated_eval = _load_rated_eval_split_from_parquet(
                                valid_split_parquet_path,
                                avail_cols,
                            )
                            metrics["a4_valid_eval_source"] = "parquet"
                        except Exception as _a4_v_exc:
                            logger.warning(
                                "A4: could not load validation parquet for eval: %s",
                                _a4_v_exc,
                            )
                            metrics["a4_valid_eval_source"] = "parquet_failed"
                    _fused_vl: Optional[np.ndarray] = None
                    if _val_rated_eval is not None and not _val_rated_eval.empty:
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
                        metrics["a4_candidate_rows_valid"] = int(np.sum(_cand_mask_vl))
                    else:
                        metrics["a4_candidate_rows_valid"] = 0
                    _test_rated_a4 = test_rated
                    metrics["a4_test_eval_source"] = "memory"
                    if (
                        (_test_rated_a4 is None or _test_rated_a4.empty)
                        and test_split_parquet_path is not None
                        and test_split_parquet_path.exists()
                    ):
                        try:
                            _test_rated_a4 = _load_rated_eval_split_from_parquet(
                                test_split_parquet_path,
                                avail_cols,
                            )
                            metrics["a4_test_eval_source"] = "parquet"
                        except Exception as _a4_te_exc:
                            logger.warning(
                                "A4: could not load test parquet for eval: %s",
                                _a4_te_exc,
                            )
                            metrics["a4_test_eval_source"] = "parquet_failed"
                    _fused_te: Optional[np.ndarray] = None
                    _a4_test_wh: Optional[float] = None
                    if _test_rated_a4 is not None and not _test_rated_a4.empty:
                        _x_te_s1 = _dataframe_for_lgb_predict(model, _test_rated_a4, avail_cols)
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
                        _a4_test_wh = _split_window_hours_from_payout_df(_test_rated_a4)
                        if _a4_test_wh is None and (
                            test_split_parquet_path is not None and test_split_parquet_path.exists()
                        ):
                            _a4_test_wh = _split_window_hours_from_parquet_payout(test_split_parquet_path)
                        metrics["a4_candidate_rows_test"] = int(np.sum(_cand_mask_te))
                    else:
                        metrics["a4_candidate_rows_test"] = 0

                    metrics["stage1_datasets"] = _snapshot_stage1_datasets_for_v2(metrics)
                    _field_test_mode = str(SELECTION_MODE or "").strip().lower() == "field_test"
                    _a4_thr_calib = "validation_fused_scores"
                    if _fused_vl is None:
                        if _field_test_mode:
                            raise ValueError(
                                "rated A4: field_test mode requires non-empty validation rows "
                                "with fused scores to calibrate a deployed threshold."
                            )
                        _a4_final_t = float(_stage1_threshold)
                        _a4_thr_calib = "validation_missing_retained_stage1_threshold"
                    else:
                        _pick_a4 = pick_dec026_threshold_from_binary_scores(
                            _val_rated_eval["label"].to_numpy(dtype=float),
                            _fused_vl,
                            recall_floor=THRESHOLD_MIN_RECALL,
                            min_alert_count=THRESHOLD_MIN_ALERT_COUNT,
                            min_alerts_per_hour=_ft_thr_mah,
                            window_hours=_ft_thr_wh,
                            fbeta_beta=THRESHOLD_FBETA,
                        )
                        if _field_test_mode and _pick_a4.is_fallback:
                            raise ValueError(
                                "rated A4: field_test mode requires a feasible DEC-026 pick on "
                                "fused validation scores (recall / min_alerts / alerts-per-hour); "
                                "pick returned fallback."
                            )
                        if _pick_a4.is_fallback:
                            _a4_final_t = float(_stage1_threshold)
                            _a4_thr_calib = "fused_pick_fallback_retained_stage1_threshold"
                        else:
                            _a4_final_t = float(_pick_a4.threshold)

                    metrics["a4_stage1_threshold_before_final_calibration"] = float(_stage1_threshold)
                    metrics["final_score_surface"] = "a4_product"
                    metrics["a4_threshold_calibrated_on"] = _a4_thr_calib
                    metrics["deployed_threshold"] = float(_a4_final_t)
                    metrics["threshold"] = float(_a4_final_t)

                    _a4_train = _train_metrics_dict_from_y_scores(
                        y_tr,
                        _fused_tr,
                        _a4_final_t,
                        label="rated_a4_fused_train",
                        log_results=False,
                        train_window_hours=_train_wh_rate,
                    )
                    metrics.update({f"a4_{k}": v for k, v in _a4_train.items()})
                    metrics.update(_a4_train)

                    if _fused_vl is not None and _val_rated_eval is not None:
                        _a4_valid = _compute_valid_metrics_from_scores(
                            _val_rated_eval["label"].to_numpy(dtype=float),
                            _fused_vl,
                            _a4_final_t,
                        )
                        metrics.update({f"a4_{k}": v for k, v in _a4_valid.items()})
                        metrics.update(_a4_valid)
                        _update_val_field_test_primary_keys_from_val_labels(
                            metrics, _val_rated_eval["label"]
                        )

                    if _fused_te is not None and _test_rated_a4 is not None and not _test_rated_a4.empty:
                        _a4_test = _compute_test_metrics_from_scores(
                            _test_rated_a4["label"].to_numpy(dtype=float),
                            _fused_te,
                            _a4_final_t,
                            label="rated_a4_fused_test",
                            _uncalibrated=bool(metrics.get("_uncalibrated", False)),
                            log_results=False,
                            production_neg_pos_ratio=PRODUCTION_NEG_POS_RATIO,
                            test_window_hours=_a4_test_wh,
                        )
                        metrics.update({f"a4_{k}": v for k, v in _a4_test.items()})
                        metrics.update(_a4_test)

                    metrics.update(
                        _split_alert_density_prefixed_dict(
                            "val",
                            scores=(
                                np.asarray(_fused_vl, dtype=np.float64).reshape(-1)
                                if _fused_vl is not None
                                else None
                            ),
                            threshold=float(metrics["threshold"]),
                            window_hours=_vwh_rate,
                            objective_min=_obj_rate,
                        )
                    )

                    metrics["a4_enabled"] = True
                    metrics["a4_fusion_mode"] = _fusion_mode
                    metrics["a4_stage2_model_backend"] = "lightgbm"
                    metrics["a4_stage2_features"] = list(avail_cols)
                    metrics["_a4_stage2_model"] = _stage2
                except ValueError as _a4_val_exc:
                    if str(_a4_val_exc).startswith("rated A4:"):
                        raise
                    logger.warning(
                        "A4 two-stage training failed (fallback to Stage-1 only): %s", _a4_val_exc
                    )
                    metrics["a4_enabled"] = False
                    metrics["a4_failure_reason"] = str(_a4_val_exc)
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

    if metrics.get("a4_enabled"):
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
            k: v
            for k, v in metrics.items()
            if isinstance(k, str) and k.startswith("test_")
        }

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
    metrics["trainer_device_mode_requested"] = _REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS
    _trainer_dev_eff = (
        "gpu"
        if (
            str(_EFFECTIVE_LIGHTGBM_DEVICE).lower() == "gpu"
            or str(_LAST_GBM_BACKEND_EFFECTIVE_DEVICE).lower() == "gpu"
        )
        else "cpu"
    )
    metrics["trainer_device_mode_effective"] = _trainer_dev_eff
    metrics["gpu_fallback_used"] = bool(_LIGHTGBM_GPU_FALLBACK_USED or _GBM_BACKEND_GPU_FALLBACK_USED)

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
        "a4_stage1_threshold_before_final_calibration": metrics.get(
            "a4_stage1_threshold_before_final_calibration"
        ),
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

    ``payout_complete_dtm`` is included when present so DEC-026 / field-test HPO can
    compute ``val_window_hours`` on B+ paths where ``valid_df`` is not in memory.
    """
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(split_path)
    available = set(pf.schema.names)
    cols = [c for c in feature_cols if c in available]
    for extra in ("label", "is_rated", "payout_complete_dtm"):
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


def _step8_resolve_sample_strategy(raw: Any) -> str:
    """Normalize Step 8 screening row-sample strategy (head | tail | head_tail)."""
    s = str(raw or "head").strip().lower()
    if s not in ("head", "tail", "head_tail"):
        logger.warning("STEP8_SCREEN_SAMPLE_STRATEGY=%r invalid; using head", raw)
        return "head"
    return s


def _step8_sample_in_memory_train(
    train_df: pd.DataFrame,
    *,
    strategy: str,
    sample_n: Optional[int],
    default_cap: int,
) -> pd.DataFrame:
    """Return up to ``cap`` rows from *train_df* for screening (no full-train scan)."""
    cap = int(sample_n) if sample_n is not None and int(sample_n) >= 1 else int(default_cap)
    n = min(cap, len(train_df))
    if n <= 0:
        return train_df.head(0)
    if strategy == "tail":
        return train_df.tail(n)
    if strategy == "head_tail":
        nh = max(1, n // 2)
        nt = n - nh
        h = train_df.head(nh)
        t = train_df.tail(nt)
        out = pd.concat([h, t], ignore_index=True)
        if "canonical_id" in out.columns and "bet_id" in out.columns:
            out = out.drop_duplicates(subset=["canonical_id", "bet_id"], keep="first")
        else:
            out = out.drop_duplicates()
        return out.iloc[:n].copy()
    return train_df.head(n).copy()


def _read_parquet_tail_step8(path: Path, n: int) -> pd.DataFrame:
    """Last *n* train rows by ``payout_complete_dtm`` (B+ train parquet is time-sorted ascending)."""
    if n <= 0:
        return pd.DataFrame()
    import duckdb

    p = str(path).replace("'", "''")
    con = duckdb.connect(":memory:")
    try:
        return con.execute(
            f"SELECT * FROM read_parquet('{p}') "
            f"ORDER BY payout_complete_dtm DESC NULLS LAST, "
            f"bet_id DESC NULLS LAST LIMIT {int(n)}"
        ).df()
    finally:
        con.close()


def _read_parquet_head_tail_step8(
    path: Path,
    n: int,
    *,
    read_head: Callable[[Path, int], pd.DataFrame],
) -> pd.DataFrame:
    """Combine earliest and latest rows (deduped) for screening sample."""
    if n <= 0:
        return pd.DataFrame()
    nh = max(1, n // 2)
    nt = max(1, n - nh)
    head_df = read_head(path, nh)
    tail_df = _read_parquet_tail_step8(path, nt)
    if head_df.empty and tail_df.empty:
        return pd.DataFrame()
    if head_df.empty:
        return tail_df
    if tail_df.empty:
        return head_df
    out = pd.concat([head_df, tail_df], ignore_index=True)
    if "canonical_id" in out.columns and "bet_id" in out.columns:
        out = out.drop_duplicates(subset=["canonical_id", "bet_id"], keep="first")
    else:
        out = out.drop_duplicates()
    if len(out) > n:
        out = out.iloc[:n].copy()
    return out


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
    *,
    rated_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Row-level split summaries from in-memory train/valid/test frames."""
    if rated_only:
        def _rated_view(df: pd.DataFrame) -> pd.DataFrame:
            if "is_rated" not in df.columns:
                return df.head(0).copy()
            return df.loc[df["is_rated"].astype(bool)].copy()

        train_df = _rated_view(train_df)
        valid_df = _rated_view(valid_df)
        test_df = _rated_view(test_df)
    return {
        "train": _one_split_block_from_dataframe(train_df),
        "valid": _one_split_block_from_dataframe(valid_df),
        "test": _one_split_block_from_dataframe(test_df),
    }


def split_row_metadata_from_parquet_paths(
    train_path: Path,
    valid_path: Path,
    test_path: Path,
    *,
    rated_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Row-level split summaries via DuckDB aggregates (no full-frame load)."""
    import duckdb

    def _q_one(p: Path) -> dict[str, Any]:
        s = str(p).replace("'", "''")
        con = duckdb.connect(":memory:")
        try:
            cols_probe = con.execute(
                f"SELECT * FROM read_parquet('{s}') LIMIT 0"
            )
            cols = {str(d[0]).strip().lower() for d in (cols_probe.description or [])}
            where_sql = (
                " WHERE coalesce(try_cast(is_rated AS BOOLEAN), FALSE)"
                if rated_only and "is_rated" in cols
                else ""
            )
            row = con.execute(
                f"SELECT count(*) AS n, "
                f"coalesce(sum(cast(label AS INTEGER)), 0) AS pos, "
                f"min(payout_complete_dtm) AS dt_min, "
                f"max(payout_complete_dtm) AS dt_max "
                f"FROM read_parquet('{s}')"
                f"{where_sql}"
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


def split_row_metadata_from_parquet_path_sequences(
    train_paths: Sequence[Path],
    valid_paths: Sequence[Path],
    test_paths: Sequence[Path],
    *,
    rated_only: bool = False,
) -> dict[str, dict[str, Any]]:
    """Row-level split summaries via DuckDB over one or many Parquet files per split."""
    import duckdb

    def _q_many(paths: Sequence[Path]) -> dict[str, Any]:
        if not paths:
            return {"start": None, "end": None, "rows": 0, "positives": 0, "negatives": 0}
        from_sql = _libsvm_duckdb_read_parquet_expr(paths)
        con = duckdb.connect(":memory:")
        try:
            cols_probe = con.execute(f"SELECT * FROM {from_sql} LIMIT 0")
            cols = {str(d[0]).strip().lower() for d in (cols_probe.description or [])}
            where_sql = (
                " WHERE coalesce(try_cast(is_rated AS BOOLEAN), FALSE)"
                if rated_only and "is_rated" in cols
                else ""
            )
            row = con.execute(
                f"SELECT count(*) AS n, "
                f"coalesce(sum(cast(label AS INTEGER)), 0) AS pos, "
                f"min(payout_complete_dtm) AS dt_min, "
                f"max(payout_complete_dtm) AS dt_max "
                f"FROM {from_sql}"
                f"{where_sql}"
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
        "train": _q_many(train_paths),
        "valid": _q_many(valid_paths),
        "test": _q_many(test_paths),
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
    model_used_splits: Optional[dict[str, dict[str, Any]]] = None,
    identity_mapping_mode: str = "cutoff_window",
    t_game_features_enabled: bool = False,
    t_game_visible_time_column: str = "none",
    l2_snapshot_id: Optional[str] = None,
    source_snapshot_id: Optional[str] = None,
    l2_training_bundle_dir: Optional[str] = None,
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
    _lineage: dict[str, Any] = {}
    if l2_snapshot_id:
        _lineage["l2_snapshot_id"] = str(l2_snapshot_id)
    if source_snapshot_id:
        _lineage["source_snapshot_id"] = str(source_snapshot_id)
    if l2_training_bundle_dir:
        _lineage["l2_training_bundle_dir"] = str(l2_training_bundle_dir)
    _out: dict[str, Any] = {
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
        "model_used_splits": model_used_splits,
        "training_params": {
            "skip_optuna": bool(skip_optuna),
            "optuna_hpo_effective_enabled": _rated_d.get("optuna_hpo_effective_enabled"),
            "optuna_hpo_objective_mode": _rated_d.get("optuna_hpo_objective_mode"),
            "sample_rated_n": sample_rated_n,
            "neg_sample_frac_effective": float(neg_sample_frac_effective),
            "threshold_min_recall": THRESHOLD_MIN_RECALL,
            "threshold_min_alert_count": int(THRESHOLD_MIN_ALERT_COUNT),
            # A2 / DEC-044: echo rated training recipe (same key as training_metrics.json rated block).
            "ranking_recipe": _rated_d.get("ranking_recipe"),
            "trainer_device_mode_requested": _rated_d.get("trainer_device_mode_requested"),
            "trainer_device_mode_effective": _rated_d.get("trainer_device_mode_effective"),
            "gpu_fallback_used": _rated_d.get("gpu_fallback_used"),
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
            "final_score_surface": _rated_d.get("final_score_surface"),
            "a4_threshold_calibrated_on": _rated_d.get("a4_threshold_calibrated_on"),
            "a4_stage1_threshold_before_final_calibration": _rated_d.get(
                "a4_stage1_threshold_before_final_calibration"
            ),
            "identity_mapping_mode": str(identity_mapping_mode),
            "t_game_features_enabled": bool(t_game_features_enabled),
            "t_game_visible_time_column": str(t_game_visible_time_column),
        },
        "artifacts": {
            "bundle_dir": str(bundle_dir.resolve()),
            "training_metrics_path": str((bundle_dir / "training_metrics.json").resolve()),
            "pipeline_diagnostics_path": str((bundle_dir / "pipeline_diagnostics.json").resolve()),
            "model_metadata_path": str((bundle_dir / "model_metadata.json").resolve()),
        },
    }
    if _lineage:
        _out["lineage"] = _lineage
    return _out


# ---------------------------------------------------------------------------
# Main training pipeline
# ---------------------------------------------------------------------------


def _run_pipeline_core(args) -> None:
    """Phase-1 training pipeline implementation (see ``run_pipeline`` wrapper)."""
    from trainer.training.pipeline_run_core import run_pipeline_core

    run_pipeline_core(args)


@_run_pipeline_with_step_cleanup
def run_pipeline(args) -> None:
    """Phase-1 training pipeline entry point."""
    from trainer.training.pipeline_orchestrator import run_pipeline_impl

    run_pipeline_impl(args)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    from trainer.training.trainer_argparse import build_trainer_argparser

    args = build_trainer_argparser().parse_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()
