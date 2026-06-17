"""trainer_hightier serving: SQLite column contracts aligned with ``trainer`` Phase-1 ML API.

Keeps migration tuples and logical column lists for shared ``state.db`` between
scorer, validator, and API without importing ``trainer.serving`` at runtime.
"""

from __future__ import annotations

from typing import Any, Final

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

#: Player-game alert metadata (representative ``bet_id`` remains primary key).
PLAYER_GAME_ALERT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("game_id", "TEXT"),
    ("player_game_score", "REAL"),
    ("player_game_bet_count", "INTEGER"),
)

#: Alerts ALTER list for validator-first DB — mirrors ``trainer.serving.validator._ALERTS_MIGRATION_COLS``.
ALERTS_MIGRATION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    *NEW_ALERT_COLUMNS,
    *PLAYER_GAME_ALERT_COLUMNS,
)

#: validation_results legacy migration — mirrors ``trainer.serving.scorer._VALIDATION_RESULTS_MIGRATION_COLS``.
VALIDATION_RESULTS_BASE_MIGRATION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (("bet_ts", "TEXT"),)

#: Phase-1 validation_results extras — mirrors ``trainer.serving.validator._NEW_VAL_COLS``.
VALIDATION_RESULTS_PHASE1_MIGRATION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("canonical_id", "TEXT"),
    ("model_version", "TEXT"),
    ("casino_player_id", "TEXT"),
    ("bet_ts", "TEXT"),
)

#: Columns scorer v2 must populate for ``validator.parse_alerts`` / ``validate_alert_row``.
ALERTS_VALIDATOR_REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "bet_id",
    "ts",
    "bet_ts",
    "player_id",
    "table_id",
    "position_idx",
    "session_id",
    "score",
    "canonical_id",
)

#: ML API ``/alerts`` protocol keys produced by :func:`api_server._alerts_to_protocol_records`.
ALERTS_API_PROTOCOL_COLUMNS: Final[tuple[str, ...]] = (
    "bet_id",
    "ts",
    "bet_ts",
    "player_id",
    "game_id",
    "casino_player_id",
    "table_id",
    "position_idx",
    "session_id",
    "visit_avg_bet",
    "is_known_player",
)


def assert_alerts_dataframe_validator_ready(alerts: Any) -> None:
    """Fail fast when scorer alert rows lack validator / API contract columns."""
    import pandas as pd

    if not isinstance(alerts, pd.DataFrame):
        raise TypeError(f"alerts must be a DataFrame, got {type(alerts)!r}")
    missing = [c for c in ALERTS_VALIDATOR_REQUIRED_COLUMNS if c not in alerts.columns]
    if missing:
        raise ValueError(
            "[contracts] alerts missing validator-required columns: "
            f"{missing}; got columns={list(alerts.columns)}"
        )
    if alerts.empty:
        return
    for col in ALERTS_VALIDATOR_REQUIRED_COLUMNS:
        if alerts[col].isna().all():
            raise ValueError(
                f"[contracts] alerts column {col!r} is all-null; validator cannot process rows"
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
META_KEY_MID_TERM_FRESHNESS_STATUS: Final[str] = "mid_term_freshness_status"
META_KEY_MID_TERM_ANCHOR_MAX: Final[str] = "mid_term_anchor_gaming_day_event_max"
META_KEY_MID_TERM_STALENESS_DAYS: Final[str] = "mid_term_staleness_days"
META_KEY_SLOW_FRESHNESS_STATUS: Final[str] = "slow_freshness_status"
META_KEY_SLOW_ANCHOR_MAX: Final[str] = "slow_anchor_gaming_day_event_max"
META_KEY_SLOW_STALENESS_DAYS: Final[str] = "slow_staleness_days"
META_KEY_SNAPSHOT_SCORING_DEGRADED: Final[str] = "snapshot_scoring_degraded"
META_KEY_REFRESH_SUPERVISOR_LAST_CHECK: Final[str] = "refresh_supervisor_last_check_iso"
META_KEY_MID_TERM_REFRESH_LAST_ATTEMPT: Final[str] = "mid_term_refresh_last_attempt_iso"
META_KEY_SLOW_REFRESH_LAST_ATTEMPT: Final[str] = "slow_refresh_last_attempt_iso"
META_KEY_SLOW_REFRESH_LAST_CHECK_DAY: Final[str] = "slow_refresh_last_check_day"
META_KEY_SOURCE_MIRROR_BET_STATUS: Final[str] = "source_mirror_bet_status"
META_KEY_SOURCE_MIRROR_SESSION_STATUS: Final[str] = "source_mirror_session_status"
META_KEY_FEAST_REFRESH_SUPERVISOR_LAST_CHECK: Final[str] = "feast_refresh_supervisor_last_check_iso"
META_KEY_FEAST_REFRESH_SUPERVISOR_LAST_ATTEMPT: Final[str] = "feast_refresh_supervisor_last_attempt_iso"
META_KEY_FEAST_REFRESH_SUPERVISOR_LAST_SUCCESS: Final[str] = "feast_refresh_supervisor_last_success_iso"
META_KEY_FEAST_SLOW_REFRESH_LAST_CHECK_DAY: Final[str] = "feast_slow_refresh_last_check_day"
META_KEY_FEAST_READINESS_LATEST_JSON: Final[str] = "feast_online_readiness_latest_json"
META_KEY_FEAST_READINESS_LATEST_SHA256: Final[str] = "feast_online_readiness_latest_sha256"
META_KEY_FEAST_READINESS_LATEST_RUN_ID: Final[str] = "feast_online_readiness_latest_run_id"
META_KEY_FEAST_READINESS_LATEST_GENERATED_AT: Final[str] = "feast_online_readiness_latest_generated_at"
META_KEY_SCHEMA_VERSION: Final[str] = "hightier_state_schema_version"

STATE_SCHEMA_VERSION: Final[str] = "1"
