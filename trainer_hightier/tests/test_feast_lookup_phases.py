"""Tests for Feast online lookup phase timing logs."""

from __future__ import annotations

import logging

import pytest

from trainer_hightier.serving.feast_online_adapter import (
    FeastLookupPhaseTimings,
    _format_feast_lookup_phases_log,
    _log_feast_lookup_phases,
)


def test_format_feast_lookup_phases_log_includes_lock_and_store_size() -> None:
    """Phase log string includes refresh lock and online store bytes when present."""
    phases = FeastLookupPhaseTimings(
        feature_store_init_ms=120.0,
        resolve_refs_ms=30.0,
        get_online_features_ms=400.0,
        to_df_ms=50.0,
        dedupe_ms=5.0,
        total_ms=605.0,
    )
    msg = _format_feast_lookup_phases_log(
        phases,
        n_entities=22,
        n_refs=17,
        n_cols=13,
        ctx={"refresh_lock_present": True, "online_store_bytes": 1_048_576},
    )
    assert "store_init=120.0" in msg
    assert "rpc=400.0" in msg
    assert "total=605.0" in msg
    assert "n_entities=22" in msg
    assert "refresh_lock=1" in msg
    assert "online_store_bytes=1048576" in msg


def test_log_feast_lookup_phases_warns_when_total_exceeds_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slow total emits WARNING with phase breakdown (default threshold 500 ms)."""
    phases = FeastLookupPhaseTimings(
        feature_store_init_ms=2000.0,
        resolve_refs_ms=100.0,
        get_online_features_ms=2500.0,
        to_df_ms=200.0,
        dedupe_ms=10.0,
        total_ms=4810.0,
    )
    with caplog.at_level(logging.DEBUG, logger="trainer_hightier.serving.feast_online_adapter"):
        _log_feast_lookup_phases(
            phases,
            n_entities=71,
            n_refs=17,
            n_cols=13,
            ctx={"refresh_lock_present": False},
        )
    assert any("lookup phases_ms" in r.message for r in caplog.records if r.levelno == logging.DEBUG)
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn_records) == 1
    assert "slow lookup phases_ms" in warn_records[0].message
    assert "store_init=2000.0" in warn_records[0].message
