from __future__ import annotations

import csv
import json
from pathlib import Path

from trainer.scripts import report_w2_objective_parity as mod


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_row_from_run_dir_reads_training_and_backtest(tmp_path: Path) -> None:
    run = tmp_path / "r1"
    _write_json(
        run / "training_metrics.json",
        {
            "model_version": "m1",
            "selection_mode": "field_test",
            "optuna_hpo_objective_mode": "field_test_dec026_val_precision_prod_adj",
            "optuna_hpo_study_best_trial_value": 0.44,
            "test_precision": 0.31,
            "test_precision_prod_adjusted": 0.28,
            "test_recall": 0.12,
        },
    )
    _write_json(
        run / "backtest_metrics.json",
        {
            "selection_mode": "field_test",
            "model_default": {
                "test_ap": 0.2,
                "test_precision": 0.25,
                "test_precision_prod_adjusted": 0.22,
                "test_recall": 0.11,
                "alerts_per_hour": 5.0,
                "test_precision_at_recall_0.01": 0.5,
                "test_precision_at_recall_0.01_prod_adjusted": 0.45,
            },
            "optuna": {
                "test_ap": 0.21,
                "test_precision": 0.27,
                "test_precision_prod_adjusted": 0.24,
                "test_recall": 0.13,
                "alerts_per_hour": 5.3,
                "test_precision_at_recall_0.01": 0.52,
                "test_precision_at_recall_0.01_prod_adjusted": 0.47,
            },
        },
    )
    row = mod.row_from_run_dir(run)
    assert row.run_id == "m1"
    assert row.selection_mode_train == "field_test"
    assert row.bt_optuna_test_precision_prod_adjusted == 0.24


def test_main_writes_csv_and_md(tmp_path: Path, monkeypatch) -> None:
    run1 = tmp_path / "r1"
    run2 = tmp_path / "r2"
    _write_json(
        run1 / "training_metrics.json",
        {
            "model_version": "r1",
            "selection_mode": "legacy",
            "optuna_hpo_objective_mode": "validation_ap",
            "test_precision": 0.2,
        },
    )
    _write_json(
        run1 / "backtest_metrics.json",
        {"selection_mode": "legacy", "optuna": {"test_precision_prod_adjusted": 0.2, "test_recall": 0.1}},
    )
    _write_json(
        run2 / "training_metrics.json",
        {
            "model_version": "r2",
            "selection_mode": "field_test",
            "optuna_hpo_objective_mode": "field_test_dec026_val_precision_prod_adj",
            "test_precision": 0.3,
        },
    )
    _write_json(
        run2 / "backtest_metrics.json",
        {"selection_mode": "field_test", "optuna": {"test_precision_prod_adjusted": 0.35, "test_recall": 0.2}},
    )
    out_csv = tmp_path / "out" / "parity.csv"
    out_md = tmp_path / "out" / "parity.md"
    monkeypatch.setattr(
        "sys.argv",
        [
            "report_w2_objective_parity.py",
            "--run-dir",
            str(run1),
            "--run-dir",
            str(run2),
            "--output-csv",
            str(out_csv),
            "--output-md",
            str(out_md),
        ],
    )
    rc = mod.main()
    assert rc == 0
    assert out_csv.is_file()
    assert out_md.is_file()
    with out_csv.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    md = out_md.read_text(encoding="utf-8")
    assert "Objective Group Summary" in md
    assert "Frozen Field Mapping Snapshot" in md
