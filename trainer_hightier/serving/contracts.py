"""trainer_hightier serving: SQLite column contracts aligned with ``trainer`` Phase-1 ML API.

Keeps migration tuples and logical column lists for shared ``state.db`` between
scorer, validator, and API without importing ``trainer.serving`` at runtime.
"""

from __future__ import annotations

from typing import Final

#: New alert columns (Phase-1) — mirrors ``trainer.serving.scorer._NEW_ALERT_COLS``.
NEW_ALERT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("canonical_id", "TEXT"),
    ("is_rated_obs", "INTEGER"),
    ("reason_codes", "TEXT"),
    ("model_version", "TEXT"),
    ("margin", "REAL"),
    ("scored_at", "TEXT"),
    ("casino_player_id", "TEXT"),
)

#: Alerts ALTER list for validator-first DB — mirrors ``trainer.serving.validator._ALERTS_MIGRATION_COLS``.
ALERTS_MIGRATION_COLUMNS: Final[tuple[tuple[str, str], ...]] = NEW_ALERT_COLUMNS

#: validation_results legacy migration — mirrors ``trainer.serving.scorer._VALIDATION_RESULTS_MIGRATION_COLS``.
VALIDATION_RESULTS_BASE_MIGRATION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (("bet_ts", "TEXT"),)

#: Phase-1 validation_results extras — mirrors ``trainer.serving.validator._NEW_VAL_COLS``.
VALIDATION_RESULTS_PHASE1_MIGRATION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("canonical_id", "TEXT"),
    ("model_version", "TEXT"),
    ("casino_player_id", "TEXT"),
    ("bet_ts", "TEXT"),
)

VALIDATION_COLUMNS: Final[tuple[str, ...]] = (
    "alert_ts",
    "validated_at",
    "player_id",
    "casino_player_id",
    "canonical_id",
    "table_id",
    "position_idx",
    "session_id",
    "bet_id",
    "score",
    "result",
    "gap_start",
    "gap_minutes",
    "reason",
    "bet_ts",
    "model_version",
)

META_KEY_LAST_ETL_WATERMARK: Final[str] = "last_processed_etl_insert"
META_KEY_LAST_PROCESSED_END: Final[str] = "last_processed_end"
META_KEY_ACTIVE_SNAPSHOT_VERSION: Final[str] = "active_feature_snapshot_version"
META_KEY_ACTIVE_ADT_ALLOWLIST_SHA256: Final[str] = "active_adt_allowlist_sha256"
META_KEY_ACTIVE_ADT_ALLOWLIST_VERSION: Final[str] = "active_adt_allowlist_version"
META_KEY_ADT_ALLOWLIST_HEALTH: Final[str] = "adt_allowlist_health"
META_KEY_SCHEMA_VERSION: Final[str] = "hightier_state_schema_version"

STATE_SCHEMA_VERSION: Final[str] = "1"
