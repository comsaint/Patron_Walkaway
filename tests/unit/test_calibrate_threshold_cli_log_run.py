"""CLI tests for calibrate_threshold_from_prediction_log --log-calibration-run."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from trainer.scripts import calibrate_threshold_from_prediction_log as cli


def test_log_calibration_run_requires_set_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["calibrate_threshold_from_prediction_log.py", "--log-calibration-run"])
    with pytest.raises(SystemExit, match="--log-calibration-run requires --set-runtime-threshold"):
        cli.main()


def test_log_calibration_run_requires_prediction_log_path_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(sys, "argv", [
        "calibrate_threshold_from_prediction_log.py",
        "--set-runtime-threshold",
        "0.61",
        "--log-calibration-run",
        "--state-db",
        str(state_db),
    ])
    monkeypatch.setattr(cli.config, "PREDICTION_LOG_DB_PATH", "")
    with pytest.raises(
        SystemExit,
        match="--log-calibration-run requires --prediction-log-db or config PREDICTION_LOG_DB_PATH",
    ):
        cli.main()


def test_log_calibration_run_writes_calibration_runs_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pred_db = tmp_path / "prediction_log.db"
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(sys, "argv", [
        "calibrate_threshold_from_prediction_log.py",
        "--set-runtime-threshold",
        "0.62",
        "--selection-mode",
        "field_test",
        "--source",
        "unit",
        "--log-calibration-run",
        "--prediction-log-db",
        str(pred_db),
        "--state-db",
        str(state_db),
    ])

    cli.main()

    with sqlite3.connect(pred_db) as conn:
        row = conn.execute(
            "SELECT suggested_threshold, applied_to_state, summary_json FROM calibration_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert abs(float(row[0]) - 0.62) < 1e-9
    assert int(row[1]) == 1
    summary = json.loads(row[2])
    assert summary["operation"] == "set_runtime_threshold"
    assert summary["runtime_threshold_source"] == "unit"
    assert summary["selection_mode_written_to_state"] == "field_test"
    assert "selection_mode" in summary
    assert "selection_mode_source" in summary
