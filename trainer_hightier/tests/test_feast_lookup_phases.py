"""Tests for Feast online lookup phase timing logs."""

from __future__ import annotations

import logging

import pytest

from trainer_hightier.serving.feast_online_adapter import (
    FeastLookupPhaseTimings,
    _format_feast_lookup_phases_log,
    _log_feast_lookup_phases,
    clear_feature_store_cache,
    get_cached_feature_store,
    get_cached_online_feature_refs,
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


def test_feature_store_cache_reuses_store_until_registry_changes(tmp_path, monkeypatch) -> None:
    """Second lookup reuses FeatureStore; registry mtime bump rebuilds cache."""
    repo = tmp_path / "feast_repo"
    data_dir = repo / "data"
    data_dir.mkdir(parents=True)
    registry = data_dir / "registry.db"
    registry.write_bytes(b"v1")

    init_count = 0

    class _FakeStore:
        def __init__(self, repo_path: str) -> None:
            nonlocal init_count
            init_count += 1
            self.repo_path = repo_path

    monkeypatch.setattr("feast.FeatureStore", _FakeStore)
    clear_feature_store_cache()

    store1, init_ms1, reused1 = get_cached_feature_store(repo)
    store2, init_ms2, reused2 = get_cached_feature_store(repo)
    assert init_count == 1
    assert store1 is store2
    assert reused1 is False
    assert reused2 is True
    assert init_ms2 == 0.0

    registry.write_bytes(b"v2")
    store3, _init_ms3, reused3 = get_cached_feature_store(repo)
    assert init_count == 2
    assert store3 is not store1
    assert reused3 is False


def test_cached_online_feature_refs_reuses_column_set(tmp_path, monkeypatch) -> None:
    """Resolved online refs are cached per repo mtime and column tuple."""
    repo = tmp_path / "feast_repo"
    data_dir = repo / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "registry.db").write_bytes(b"v1")

    calls = {"n": 0}

    def _fake_resolve(mid_columns, slow_columns, *, feast_repo=None):
        calls["n"] += 1
        return ("fv:a",)

    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_adapter.resolve_online_feature_refs",
        _fake_resolve,
    )
    clear_feature_store_cache()

    mid = ("fe__x",)
    slow: tuple[str, ...] = ()
    refs1, ms1, reused1 = get_cached_online_feature_refs(mid, slow, feast_repo=repo)
    refs2, ms2, reused2 = get_cached_online_feature_refs(mid, slow, feast_repo=repo)
    assert calls["n"] == 1
    assert refs1 == refs2 == ("fv:a",)
    assert reused1 is False
    assert reused2 is True
    assert ms2 == 0.0
