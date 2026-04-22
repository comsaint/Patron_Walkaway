"""Batch calibration CLI tests (prediction_log + prediction_ground_truth)."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from trainer.scripts import calibrate_threshold_from_prediction_log as cli
from trainer.serving import scorer as scorer_mod


def _seed_prediction_tables(pred_db: Path) -> None:
    now = datetime.now(scorer_mod.HK_TZ)
    with sqlite3.connect(pred_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        scorer_mod.ensure_prediction_calibration_schema(conn)
        rows_pred = []
        rows_gt = []
        for i in range(12):
            bet_id = f"b{i}"
            score = 0.95 - (i * 0.05)
            scored_at = (now - timedelta(hours=1, minutes=i)).isoformat()
            rows_pred.append(
                (
                    scored_at,
                    bet_id,
                    "sess",
                    "p",
                    "c",
                    "cp",
                    "t",
                    "m1",
                    float(score),
                    float(score - 0.5),
                    1 if score >= 0.5 else 0,
                    1,
                    12,
                    1,
                    0,
                    "0_100",
                )
            )
            label = 1.0 if i < 4 else 0.0
            rows_gt.append((bet_id, label, "final", now.isoformat(), None))
        conn.executemany(
            """
            INSERT INTO prediction_log (
                scored_at, bet_id, session_id, player_id, canonical_id, casino_player_id,
                table_id, model_version, score, margin, is_alert, is_rated_obs,
                hour_of_day, day_of_week, is_weekend, bet_size_bucket
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_pred,
        )
        conn.executemany(
            """
            INSERT INTO prediction_ground_truth (bet_id, label, status, labeled_at, prediction_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows_gt,
        )
        conn.commit()


def test_batch_calibration_no_rows_logs_skipped(tmp_path: Path, monkeypatch) -> None:
    pred_db = tmp_path / "pred_empty.db"
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate_threshold_from_prediction_log.py",
            "--run-batch-calibration",
            "--prediction-log-db",
            str(pred_db),
            "--state-db",
            str(state_db),
            "--calibration-window-hours",
            "1",
        ],
    )
    cli.main()
    with sqlite3.connect(pred_db) as conn:
        row = conn.execute(
            "SELECT skipped_reason, applied_to_state FROM calibration_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == "no_rows_in_window"
    assert int(row[1]) == 0


def test_batch_calibration_apply_to_state_and_log_summary(tmp_path: Path, monkeypatch) -> None:
    pred_db = tmp_path / "pred.db"
    state_db = tmp_path / "state.db"
    _seed_prediction_tables(pred_db)
    monkeypatch.setattr(cli.config, "THRESHOLD_MIN_ALERTS_PER_HOUR", None)
    monkeypatch.setattr(cli.config, "THRESHOLD_MIN_ALERT_COUNT", 1)
    monkeypatch.setattr(cli.config, "THRESHOLD_MIN_RECALL", 0.01)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate_threshold_from_prediction_log.py",
            "--run-batch-calibration",
            "--apply-batch-threshold-to-state",
            "--selection-mode",
            "field_test",
            "--source",
            "unit_batch",
            "--prediction-log-db",
            str(pred_db),
            "--state-db",
            str(state_db),
            "--calibration-window-hours",
            "24",
        ],
    )
    cli.main()

    with sqlite3.connect(state_db) as conn:
        rt = conn.execute(
            "SELECT rated_threshold, source, selection_mode FROM runtime_rated_threshold WHERE id = 1"
        ).fetchone()
    assert rt is not None
    assert 0.0 < float(rt[0]) < 1.0
    assert str(rt[1]).startswith("unit_batch:batch_calibration")
    assert rt[2] == "field_test"

    with sqlite3.connect(pred_db) as conn:
        row = conn.execute(
            "SELECT applied_to_state, summary_json FROM calibration_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 1
    summary = json.loads(row[1])
    assert summary["operation"] == "batch_calibration"
    assert summary["source"] == "unit_batch"
    assert summary["batch_pick"]["is_fallback"] is False
