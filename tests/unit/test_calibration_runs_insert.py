"""calibration_runs.summary_json W2 contract via insert_calibration_run_row."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

from trainer.serving import scorer as scorer_mod


def test_insert_calibration_run_row_summary_has_contract_keys(tmp_path: Path) -> None:
    db = tmp_path / "pl.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        rid = scorer_mod.insert_calibration_run_row(
            conn,
            suggested_threshold=0.61,
            applied_to_state=True,
            summary_extras={"operation": "test"},
        )
        conn.commit()
    assert rid >= 1
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT applied_to_state, summary_json FROM calibration_runs WHERE id = ?",
            (rid,),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 1
    summary = json.loads(row[1])
    assert summary["operation"] == "test"
    assert "selection_mode" in summary
    assert "selection_mode_source" in summary
    assert "production_neg_pos_ratio" in summary


def test_insert_respects_production_ratio_from_config(tmp_path: Path) -> None:
    db = tmp_path / "pl2.db"
    with mock.patch.object(scorer_mod.config, "PRODUCTION_NEG_POS_RATIO", 12.5):
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            scorer_mod.insert_calibration_run_row(
                conn,
                suggested_threshold=0.5,
                applied_to_state=False,
                skipped_reason="unit",
            )
            conn.commit()
    with sqlite3.connect(db) as conn:
        sj = conn.execute("SELECT summary_json FROM calibration_runs LIMIT 1").fetchone()[0]
    assert json.loads(sj)["production_neg_pos_ratio"] == 12.5
