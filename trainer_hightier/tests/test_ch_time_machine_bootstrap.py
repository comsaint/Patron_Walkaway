"""Tests for ClickHouse time-machine bundle credential bootstrap."""

from __future__ import annotations

from pathlib import Path

import pytest

from trainer_hightier.config import (
    default_hightier_serving_config,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.serving.ch_time_machine import (
    _bootstrap_bundle_clickhouse,
    summarize_capture_readiness,
)
from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.window_registry import register_window


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


def test_readiness_reports_missing_window_registry(tmp_path: Path) -> None:
    """Zero captures should explain when no scorer/validator windows were registered."""
    summary = summarize_capture_readiness(tmp_path / "recording", FlightRecorderConfig())
    assert summary["reason"] == "window_registry_missing"
    assert summary["registry_exists"] is False
    assert summary["windows"] == 0


def test_readiness_reports_pending_not_due_yet(tmp_path: Path) -> None:
    """Registered windows are not due until their scheduled offset has elapsed."""
    root = tmp_path / "recording"
    register_window(
        root,
        source="cycles/scorer/cycle_000001/clickhouse",
        fetch="fetch_bets_incremental",
        query_meta={"fetch": "fetch_bets_incremental", "parameters": {}},
        t0_final_parquet="cycles/scorer/cycle_000001/clickhouse/incremental_t_bet.final.parquet",
    )
    window = root / "ch_time_machine" / "windows.json"
    assert window.is_file()
    summary = summarize_capture_readiness(
        root,
        FlightRecorderConfig(requery_schedule_minutes=(0, 15)),
    )
    assert summary["reason"] == "pending_not_due_yet"
    assert summary["windows"] == 1
    assert summary["pending_labels"] == 1
    assert summary["due_labels"] == 0
