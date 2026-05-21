"""Tests for deploy Feast startup helpers and readiness persistence."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from trainer_hightier.config import default_hightier_serving_config
from trainer_hightier.deploy import main as deploy_main
from trainer_hightier.serving.feast_readiness import (
    FeastLayerReadiness,
    FeastOnlineReadiness,
    write_feast_online_readiness,
    write_minimal_test_feast_readiness,
)
from trainer_hightier.serving.feature_state_store import (
    feature_state_meta_get,
    init_feature_state_db,
    persist_feast_online_readiness_latest,
)


def test_write_feast_online_readiness_atomic(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "feast_online_readiness.json"
    write_minimal_test_feast_readiness(out, feast_repo=tmp_path / "feast_repo")
    assert out.is_file()
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw.get("mid_term") is not None


def test_persist_feast_online_readiness_latest(tmp_path: Path) -> None:
    db_path = tmp_path / "feature_state.db"
    init_feature_state_db(db_path)
    doc_path = tmp_path / "feast_online_readiness.json"
    write_minimal_test_feast_readiness(doc_path)
    payload = json.loads(doc_path.read_text(encoding="utf-8"))
    sha = persist_feast_online_readiness_latest("run-test-1", payload, path=db_path)
    assert len(sha) == 64
    assert feature_state_meta_get("feast_online_readiness_latest_run_id", path=db_path) == "run-test-1"
    stored = feature_state_meta_get("feast_online_readiness_latest_json", path=db_path)
    assert stored is not None
    assert json.loads(stored)["schema_version"] == payload["schema_version"]


def test_feast_refresh_lock_timeout(tmp_path: Path) -> None:
    cfg = replace(
        default_hightier_serving_config(),
        scorer_feast_readiness_path=tmp_path / "artifacts" / "feast" / "feast_online_readiness.json",
        feast_startup_refresh_lock_wait_seconds=1,
    )
    lock = deploy_main._feast_refresh_lock_path(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held", encoding="utf-8")
    assert deploy_main._try_acquire_feast_refresh_lock(cfg) is None


def test_feast_refresh_lock_acquire_and_release(tmp_path: Path) -> None:
    cfg = replace(
        default_hightier_serving_config(),
        scorer_feast_readiness_path=tmp_path / "artifacts" / "feast" / "feast_online_readiness.json",
        feast_startup_refresh_lock_wait_seconds=5,
    )
    fd = deploy_main._try_acquire_feast_refresh_lock(cfg)
    assert fd is not None
    deploy_main._release_feast_refresh_lock(cfg, fd)
    assert not deploy_main._feast_refresh_lock_path(cfg).exists()


def test_needs_feast_startup_refresh_when_missing(tmp_path: Path) -> None:
    cfg = replace(
        default_hightier_serving_config(),
        scorer_feast_readiness_path=tmp_path / "missing.json",
    )
    need, reason = deploy_main._needs_feast_startup_refresh(
        cfg, force=False, require_mid=True, require_slow=True
    )
    assert need is True
    assert "missing" in reason.lower() or "readiness" in reason.lower()
