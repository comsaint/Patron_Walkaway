"""Tests for ClickHouse time-machine requery skip detection."""

from __future__ import annotations

from trainer_hightier.serving.flight_recorder.ch_capture import build_incremental_query_record
from trainer_hightier.serving.flight_recorder.ch_requery import requery_skip_reason


def test_allowlist_with_player_ids_is_requeryable() -> None:
    """Allowlist windows are requeryable when external_inputs contains player ids."""
    meta = build_incremental_query_record(
        last_etl=None,
        lookback_hours=6.0,
        limit_rows=100,
        allowlist_player_ids=frozenset({1, 2}),
    )
    assert requery_skip_reason(meta) is None


def test_skip_allowlist_missing_payload() -> None:
    """Allowlist_size-only legacy manifests must be skipped."""
    meta = {
        "fetch": "fetch_bets_incremental",
        "mode": "allowlist_external_input",
        "sql_final": "SELECT bet_id FROM t FINAL",
        "requeryable": False,
        "skip_reason": "allowlist_external_input_missing_payload",
    }
    assert requery_skip_reason(meta) == "allowlist_external_input_missing_payload"


def test_skip_placeholder_sql() -> None:
    """Pseudo SQL with ellipsis is not executable."""
    meta = {
        "fetch": "fetch_bet_pool_window",
        "sql_final": "SELECT bet_id, ... FROM t FINAL",
        "requeryable": False,
        "skip_reason": "placeholder_sql_not_executable",
    }
    assert requery_skip_reason(meta) == "placeholder_sql_not_executable"
