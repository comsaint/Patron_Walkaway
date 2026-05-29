"""Tests for post-startup Feast refresh supervisor helpers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trainer_hightier.config import HK_TZ, default_hightier_serving_config
from trainer_hightier.deploy import main as deploy_main
from trainer_hightier.serving.feast_readiness import (
    FEAST_READINESS_SCOPE_PRODUCTION,
    FeastLayerReadiness,
    FeastOnlineReadiness,
)
from trainer_hightier.serving.feature_state_store import (
    feature_state_meta_get,
    init_feature_state_db,
)
from trainer_hightier.serving.feast_production_constants import (
    PRODUCTION_LONG_TERM_FEATURE_COLUMNS,
    PRODUCTION_MID_TERM_FEATURE_COLUMNS,
)
from trainer_hightier.serving.snapshot_freshness import expected_mid_term_anchor


def _hk_dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(HK_TZ))


def _layer(
    *,
    layer: str,
    anchor: date,
    feature_columns: tuple[str, ...],
    feast_feature_view: str,
) -> FeastLayerReadiness:
    generated = _hk_dt(2026, 5, 20, 12)
    return FeastLayerReadiness(
        layer=layer,
        source_scope=FEAST_READINESS_SCOPE_PRODUCTION,
        anchor_gaming_day_event_max=anchor,
        generated_at=generated,
        row_count=100,
        distinct_canonical_count=100,
        cell_null_counts={},
        lookup_sample_size=10,
        lookup_entity_present_rate=1.0,
        feature_columns=feature_columns,
        feast_feature_view=feast_feature_view,
        materialize_source="test_fixture",
    )


def _readiness(
    *,
    mid_anchor: date,
    slow_anchor: date | None = None,
) -> FeastOnlineReadiness:
    generated = _hk_dt(2026, 5, 20, 12)
    slow = slow_anchor if slow_anchor is not None else mid_anchor
    return FeastOnlineReadiness(
        schema_version=1,
        generated_at=generated,
        feast_repo="/tmp/feast_repo",
        mid_term=_layer(
            layer="mid_term",
            anchor=mid_anchor,
            feature_columns=PRODUCTION_MID_TERM_FEATURE_COLUMNS,
            feast_feature_view="mid_term_daily_spike_features",
        ),
        slow_patron=_layer(
            layer="slow_patron",
            anchor=slow,
            feature_columns=PRODUCTION_LONG_TERM_FEATURE_COLUMNS,
            feast_feature_view="long_term_slow_spike_features",
        ),
    )


def _cfg(tmp_path: Path) -> object:
    return replace(
        default_hightier_serving_config(),
        scorer_feast_readiness_path=tmp_path / "artifacts" / "feast" / "feast_online_readiness.json",
        feature_state_db_path=tmp_path / "local_state" / "feature_state.db",
        scorer_feast_repo_path=tmp_path / "feast_repo",
    )


def test_feast_refresh_lock_nonblocking_when_held(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    lock = deploy_main._feast_refresh_lock_path(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held", encoding="utf-8")
    assert deploy_main._try_acquire_feast_refresh_lock(cfg, wait_seconds=0) is None


def test_feast_mid_refresh_needed_fresh(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    now = _hk_dt(2026, 5, 20, 5)
    from trainer_hightier.serving.snapshot_freshness import serving_gaming_day_event

    serving = serving_gaming_day_event(now)
    anchor = expected_mid_term_anchor(serving)
    need, _reason = deploy_main._feast_mid_refresh_needed(
        cfg,
        _readiness(mid_anchor=anchor),
        require_mid=True,
        now=now,
    )
    assert need is False


def test_feast_mid_refresh_needed_stale_before_target_hour(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    now = _hk_dt(2026, 5, 20, 3, 59)
    from trainer_hightier.serving.snapshot_freshness import serving_gaming_day_event

    serving = serving_gaming_day_event(now)
    anchor = expected_mid_term_anchor(serving) - timedelta(days=1)
    need, _reason = deploy_main._feast_mid_refresh_needed(
        cfg,
        _readiness(mid_anchor=anchor),
        require_mid=True,
        now=now,
    )
    assert need is False


def test_feast_mid_refresh_needed_stale_after_target_hour(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    now = _hk_dt(2026, 5, 20, 4, 1)
    from trainer_hightier.serving.snapshot_freshness import serving_gaming_day_event

    serving = serving_gaming_day_event(now)
    anchor = expected_mid_term_anchor(serving) - timedelta(days=1)
    need, _reason = deploy_main._feast_mid_refresh_needed(
        cfg,
        _readiness(mid_anchor=anchor),
        require_mid=True,
        now=now,
    )
    assert need is True


def test_feast_mid_refresh_needed_hard_cap(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    now = _hk_dt(2026, 5, 20, 5)
    from trainer_hightier.serving.snapshot_freshness import serving_gaming_day_event

    serving = serving_gaming_day_event(now)
    anchor = expected_mid_term_anchor(serving) - timedelta(days=4)
    need, _reason = deploy_main._feast_mid_refresh_needed(
        cfg,
        _readiness(mid_anchor=anchor),
        require_mid=True,
        now=now,
    )
    assert need is True


def test_feast_slow_refresh_needed_hard_cap_bypasses_daily(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    init_feature_state_db(cfg.feature_state_db_path)
    today = _hk_dt(2026, 5, 19, 10).date().isoformat()
    from trainer_hightier.serving.contracts import META_KEY_FEAST_SLOW_REFRESH_LAST_CHECK_DAY
    from trainer_hightier.serving.feature_state_store import feature_state_meta_set

    feature_state_meta_set(
        META_KEY_FEAST_SLOW_REFRESH_LAST_CHECK_DAY,
        today,
        path=cfg.feature_state_db_path,
    )
    need, _reason = deploy_main._feast_slow_refresh_needed(
        cfg,
        _readiness(mid_anchor=date(2026, 5, 18), slow_anchor=date(2026, 3, 31)),
        require_slow=True,
        now=_hk_dt(2026, 5, 19, 10),
    )
    assert need is True


def test_feast_slow_refresh_needed_daily_gate(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    init_feature_state_db(cfg.feature_state_db_path)
    now = _hk_dt(2026, 5, 19, 10)
    serving = date(2026, 5, 19)
    anchor = date(2026, 4, 30)
    readiness = _readiness(mid_anchor=anchor, slow_anchor=anchor)
    need1, _ = deploy_main._feast_slow_refresh_needed(
        cfg, readiness, require_slow=True, now=now
    )
    assert need1 is False
    need2, reason2 = deploy_main._feast_slow_refresh_needed(
        cfg, readiness, require_slow=True, now=now
    )
    assert need2 is False
    assert "already checked today" in reason2


def test_feast_refresh_supervisor_once_skips_when_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    init_feature_state_db(cfg.feature_state_db_path)
    called: list[bool] = []

    def _fake_refresh(_opts: object) -> dict[str, str]:
        called.append(True)
        return {"verdict": "ok"}

    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh.run_feast_online_refresh",
        _fake_refresh,
    )
    now = _hk_dt(2026, 5, 20, 5)
    serving = date(2026, 5, 20)
    anchor = expected_mid_term_anchor(serving)
    readiness = _readiness(mid_anchor=anchor, slow_anchor=date(2026, 4, 30))

    def _fake_load(_path: Path) -> FeastOnlineReadiness:
        return readiness

    def _fake_mid(*_a: object, **_k: object) -> tuple[bool, str]:
        return False, "fresh"

    def _fake_slow(*_a: object, **_k: object) -> tuple[bool, str]:
        return False, "fresh"

    monkeypatch.setattr(
        "trainer_hightier.serving.feast_readiness.load_feast_online_readiness",
        _fake_load,
    )
    monkeypatch.setattr(deploy_main, "_feast_mid_refresh_needed", _fake_mid)
    monkeypatch.setattr(deploy_main, "_feast_slow_refresh_needed", _fake_slow)

    deploy_main._feast_refresh_supervisor_once(
        tmp_path,
        {"model_bundle_dir": "models", "canonical_mapping_parquet": "mapping/x.parquet"},
        cfg,
        mapping=tmp_path / "mapping.parquet",
        allowlist=tmp_path / "allowlist.parquet",
        require_mid=True,
        require_slow=True,
    )
    assert called == []


def test_feast_refresh_supervisor_once_calls_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    init_feature_state_db(cfg.feature_state_db_path)
    (tmp_path / "feast_repo").mkdir()
    called: list[str] = []

    def _fake_refresh(_opts: object) -> dict[str, str]:
        called.append("refresh")
        return {"verdict": "ok"}

    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh.run_feast_online_refresh",
        _fake_refresh,
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_adapter.feast_registry_missing",
        lambda _repo: False,
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh._resolve_refresh_options",
        lambda **_k: object(),
    )

    deploy_main._feast_refresh_supervisor_once(
        tmp_path,
        {"model_bundle_dir": "models", "canonical_mapping_parquet": "mapping/x.parquet"},
        cfg,
        mapping=tmp_path / "mapping.parquet",
        allowlist=tmp_path / "allowlist.parquet",
        require_mid=True,
        require_slow=False,
    )
    assert called == ["refresh"]
    from trainer_hightier.serving.contracts import META_KEY_FEAST_REFRESH_SUPERVISOR_LAST_SUCCESS

    assert feature_state_meta_get(
        META_KEY_FEAST_REFRESH_SUPERVISOR_LAST_SUCCESS,
        path=cfg.feature_state_db_path,
    ) is not None


def test_feast_refresh_supervisor_once_skips_when_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    init_feature_state_db(cfg.feature_state_db_path)
    called: list[bool] = []

    def _fake_refresh(_opts: object) -> dict[str, str]:
        called.append(True)
        return {"verdict": "ok"}

    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh.run_feast_online_refresh",
        _fake_refresh,
    )
    monkeypatch.setattr(deploy_main, "_feast_mid_refresh_needed", lambda *_a, **_k: (True, "stale"))
    monkeypatch.setattr(deploy_main, "_feast_slow_refresh_needed", lambda *_a, **_k: (False, "fresh"))
    lock = deploy_main._feast_refresh_lock_path(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held", encoding="utf-8")

    deploy_main._feast_refresh_supervisor_once(
        tmp_path,
        {"model_bundle_dir": "models"},
        cfg,
        mapping=tmp_path / "mapping.parquet",
        allowlist=tmp_path / "allowlist.parquet",
        require_mid=True,
        require_slow=False,
    )
    assert called == []


def test_feast_refresh_supervisor_once_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    init_feature_state_db(cfg.feature_state_db_path)
    (tmp_path / "feast_repo").mkdir()

    def _boom(_opts: object) -> dict[str, str]:
        raise RuntimeError("simulated refresh failure")

    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh.run_feast_online_refresh",
        _boom,
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_adapter.feast_registry_missing",
        lambda _repo: False,
    )
    monkeypatch.setattr(
        "trainer_hightier.serving.feast_online_refresh._resolve_refresh_options",
        lambda **_k: object(),
    )
    monkeypatch.setattr(deploy_main, "_feast_mid_refresh_needed", lambda *_a, **_k: (True, "stale"))
    monkeypatch.setattr(deploy_main, "_feast_slow_refresh_needed", lambda *_a, **_k: (False, "fresh"))

    deploy_main._feast_refresh_supervisor_once(
        tmp_path,
        {"model_bundle_dir": "models"},
        cfg,
        mapping=tmp_path / "mapping.parquet",
        allowlist=tmp_path / "allowlist.parquet",
        require_mid=True,
        require_slow=False,
    )
