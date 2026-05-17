"""Serving runtime constants (trainer-compatible names, no implicit env reads).

Deployment should tune values via :mod:`trainer_hightier.config` (``HightierServingConfig``)
and pass paths into entrypoints; this module exposes defaults for ``validator`` logic
that historically used ``import config`` as a bag of attributes.
"""

from __future__ import annotations

from pathlib import Path

from trainer_hightier.config import default_hightier_serving_config

_cfg = default_hightier_serving_config()

# --- timezone & warehouse (names mirror ``trainer.core``) ---
HK_TZ: str = _cfg.hk_tz
SOURCE_DB: str = _cfg.source_db
TBET: str = _cfg.tbet
TSESSION: str = _cfg.tsession
CASINO_PLAYER_ID_CLEAN_SQL: str = _cfg.casino_player_id_clean_sql

# --- domain (walkaway labels) ---
WALKAWAY_GAP_MIN: int = _cfg.walkaway_gap_min
ALERT_HORIZON_MIN: int = _cfg.alert_horizon_min
LABEL_LOOKAHEAD_MIN: int = _cfg.label_lookahead_min
PLACEHOLDER_PLAYER_ID: int = _cfg.placeholder_player_id

# --- validator knobs (``trainer.core._config_validator`` defaults) ---
VALIDATOR_ALERT_RETENTION_DAYS: int = _cfg.validator_alert_retention_days
VALIDATION_RESULTS_RETENTION_DAYS: int = _cfg.validation_results_retention_days
VALIDATOR_CACHE_PRUNE_INTERVAL_SECONDS: int = _cfg.validator_cache_prune_interval_seconds
VALIDATOR_FRESHNESS_BUFFER_MINUTES: int = _cfg.validator_freshness_buffer_minutes
VALIDATOR_EXTENDED_WAIT_MINUTES: int = _cfg.validator_extended_wait_minutes
VALIDATOR_FINALITY_HOURS: int = _cfg.validator_finality_hours
VALIDATOR_FINALIZE_ON_HORIZON: bool = _cfg.validator_finalize_on_horizon
VALIDATOR_NO_BET_BET_ID_LOOKUP_ENABLED: bool = _cfg.validator_no_bet_bet_id_lookup_enabled
VALIDATOR_FETCH_PRE_CONTEXT_MINUTES: int = _cfg.validator_fetch_pre_context_minutes
VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES: int = _cfg.validator_fetch_max_lookback_minutes
VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES_CAP: int = _cfg.validator_fetch_max_lookback_minutes_cap
VALIDATOR_NO_BET_RETRY_MAX_WINDOW_MINUTES: int = _cfg.validator_no_bet_retry_max_window_minutes
VALIDATOR_NO_BET_BET_ID_CHUNK_SIZE: int = _cfg.validator_no_bet_bet_id_chunk_size
VALIDATOR_NO_BET_RETRY_MAX_ALERTS: int = _cfg.validator_no_bet_retry_max_alerts

# --- scorer-ish retention (optional reads in shared state tooling) ---
SCORER_STATE_RETENTION_HOURS: int = _cfg.scorer_state_retention_hours

# --- paths ---
STATE_DB_PATH: Path = Path(_cfg.state_db_path).resolve()
FEATURE_STATE_DB_PATH: Path = Path(_cfg.feature_state_db_path).resolve()
SNAPSHOT_MANIFEST_DIR: Path = Path(_cfg.snapshot_manifest_dir).resolve()

VALIDATOR_OUT_DIR: Path = Path(_cfg.validator_out_dir).resolve()
RESULTS_PATH: Path = VALIDATOR_OUT_DIR / "validation_results.csv"
