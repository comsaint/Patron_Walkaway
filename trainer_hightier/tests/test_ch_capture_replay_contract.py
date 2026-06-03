"""Tests for ClickHouse flight recorder replay manifest contract."""

from __future__ import annotations

from datetime import datetime, timezone

from trainer_hightier.serving.flight_recorder.ch_capture import (
    build_incremental_query_record,
    build_pool_query_record,
    build_validator_bet_id_query_record,
    build_validator_canonical_query_record,
    finalize_query_manifest,
)
from trainer_hightier.serving.flight_recorder.ch_requery import requery_skip_reason


def test_incremental_global_is_requeryable() -> None:
    """Global incremental manifest has executable SQL and bet_id business key."""
    meta = build_incremental_query_record(
        last_etl=None,
        lookback_hours=6.0,
        limit_rows=100,
        allowlist_player_ids=None,
    )
    assert meta["requeryable"] is True
    assert meta["business_key"] == "bet_id"
    assert "..." not in meta["sql_final"]
    assert "FINAL" in meta["sql_final"]
    assert "FINAL" not in meta["sql_non_final"]


def test_incremental_allowlist_stores_player_ids() -> None:
    """Allowlist mode must persist ids for time-machine replay, not only size."""
    meta = build_incremental_query_record(
        last_etl=None,
        lookback_hours=6.0,
        limit_rows=100,
        allowlist_player_ids=frozenset({10, 20, 30}),
    )
    assert meta["requeryable"] is True
    assert meta["external_inputs"]["allowlist_player_ids"] == [10, 20, 30]
    assert requery_skip_reason(meta) is None


def test_incremental_allowlist_missing_payload_not_requeryable() -> None:
    """Legacy allowlist_size-only manifests are non-requeryable."""
    meta = finalize_query_manifest(
        {
            "fetch": "fetch_bets_incremental",
            "mode": "allowlist_external_input",
            "sql": "SELECT bet_id FROM t FINAL",
            "parameters": {"allowlist_size": 3},
        },
    )
    assert meta["requeryable"] is False
    assert meta["skip_reason"] == "allowlist_external_input_missing_payload"


def test_pool_window_executable_sql() -> None:
    """Pool manifest must not use placeholder column ellipsis."""
    now = datetime.now(timezone.utc)
    meta = build_pool_query_record(
        player_ids=[5255629, 6458461],
        window_start=now,
        window_end=now,
    )
    assert meta["requeryable"] is True
    assert "..." not in meta["sql_final"]
    assert "5255629" in meta["sql_final"]
    assert meta["external_inputs"]["player_ids"] == [5255629, 6458461]


def test_validator_canonical_includes_bet_id() -> None:
    """Validator canonical replay uses bet_id diff key and stores player ids."""
    now = datetime.now(timezone.utc)
    meta = build_validator_canonical_query_record(
        player_ids=[99],
        start=now,
        end=now,
    )
    assert meta["requeryable"] is True
    assert "bet_id" in meta["sql_final"]
    assert meta["external_inputs"]["player_ids"] == [99]


def test_validator_bet_id_lookup_stores_ids() -> None:
    """No-bet retry path stores bet ids for replay."""
    meta = build_validator_bet_id_query_record(bet_ids=[101, 202])
    assert meta["requeryable"] is True
    assert meta["external_inputs"]["bet_ids"] == [101, 202]
