"""Tests for ClickHouse time-machine bundle credential bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest

from trainer_hightier.config import (
    default_hightier_serving_config,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.serving.ch_time_machine import _bootstrap_bundle_clickhouse


@pytest.fixture(autouse=True)
def _reset_serving_override() -> None:
    """Isolate global serving config between tests."""
    set_hightier_serving_deploy_override(None)
    yield
    set_hightier_serving_deploy_override(None)


def test_bootstrap_loads_ch_pass_from_bundle_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone time machine must read ``.env`` like deploy main (not empty password)."""
    for key in ("CH_USER", "CH_PASS", "CH_PASSWORD", "CH_HOST"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "CH_HOST=ch.example\nCH_USER=svc\nCH_PASS=from-dotenv\n",
        encoding="utf-8",
    )
    _bootstrap_bundle_clickhouse(tmp_path)
    cfg = default_hightier_serving_config()
    assert cfg.ch_host == "ch.example"
    assert cfg.ch_user == "svc"
    assert cfg.ch_password == "from-dotenv"
