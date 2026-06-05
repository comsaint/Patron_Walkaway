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
    run_window_capture,
    summarize_capture_readiness,
)
from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.window_registry import register_window
from trainer_hightier.serving.flight_recorder.window_registry import list_windows


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


def test_time_machine_capture_error_does_not_mark_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed requery writes evidence and remains pending for retry."""
    root = tmp_path / "recording"
    window_id = register_window(
        root,
        source="cycles/scorer/cycle_000001/clickhouse",
        fetch="fetch_bets_incremental",
        query_meta={
            "fetch": "fetch_bets_incremental",
            "sql": "SELECT bet_id FROM db.t_bet FINAL",
            "parameters": {},
        },
        t0_final_parquet="cycles/scorer/cycle_000001/clickhouse/incremental_t_bet.final.parquet",
    )

    def _raise_requery(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("trainer_hightier.serving.ch_time_machine.execute_query", _raise_requery)
    window = list_windows(root)[0]
    with pytest.raises(RuntimeError, match="boom"):
        run_window_capture(root, window, "t_plus_15m", include_non_final=False)

    error_path = root / "ch_time_machine" / window_id / "capture_t_plus_15m" / "capture_error.json"
    assert error_path.is_file()
    refreshed = list_windows(root)[0]
    assert not refreshed.get("captures", {}).get("t_plus_15m")
