from __future__ import annotations

import json
from pathlib import Path

from trainer.scripts import build_w1_freeze_evidence as mod


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_run_builds_evidence_from_precondition_and_runs(tmp_path: Path) -> None:
    precondition = tmp_path / "field_test_objective_precondition_check.json"
    run_dir = tmp_path / "run1"
    out_json = tmp_path / "out" / "w1_freeze_evidence.json"
    out_md = tmp_path / "out" / "w1_freeze_evidence.md"

    _write_json(
        precondition,
        {
            "objective_decision": "BLOCKED",
            "allowed_reason_codes": [
                "empty_subset",
                "single_class",
                "invalid_input_nan",
                "infeasible_constraint",
                "missing_required_column",
            ],
        },
    )
    _write_json(
        run_dir / "training_metrics.json",
        {
            "run_id": "r1",
            "selection_mode": "field_test",
            "optuna_hpo_objective_mode": "gate_blocked",
            "optuna_hpo_gate_blocked_reason_code": "infeasible_constraint",
        },
    )
    _write_json(
        run_dir / "backtest_metrics.json",
        {
            "selection_mode": "field_test",
            "model_default": {
                "test_precision_prod_adjusted_reason_code": "empty_subset",
            },
            "optuna": {
                "test_precision_at_recall_0.01_reason_code": "single_class",
            },
        },
    )

    rc = mod.run(
        [
            "--precondition-json",
            str(precondition),
            "--run-dir",
            str(run_dir),
            "--output-json",
            str(out_json),
            "--output-md",
            str(out_md),
        ]
    )
    assert rc == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "w1-freeze-evidence-v1"
    assert payload["summary"]["run_row_count"] == 1
    assert payload["reason_code_evidence"]["unknown_reason_codes"] == []
    checks = {c["check_id"]: c for c in payload["contract_checks"]}
    assert checks["dec043_selection_mode_field_test"]["status"] == "pass"
    assert checks["dec043_reason_code_enum_freeze"]["status"] == "pass"
    md = out_md.read_text(encoding="utf-8")
    assert "W1 Freeze Evidence Package" in md
    assert "Contract Checks" in md


def test_run_tolerates_missing_optional_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_missing_backtest"
    out_json = tmp_path / "out" / "evidence.json"
    out_md = tmp_path / "out" / "evidence.md"
    _write_json(
        run_dir / "training_metrics.json",
        {
            "model_version": "m1",
            "selection_mode": "field_test",
            "optuna_hpo_objective_mode": "field_test_dec026_val_precision_prod_adj",
        },
    )
    rc = mod.run(
        [
            "--run-dir",
            str(run_dir),
            "--output-json",
            str(out_json),
            "--output-md",
            str(out_md),
        ]
    )
    assert rc == 0
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["summary"]["runs_with_training_metrics"] == 1
    assert payload["summary"]["runs_with_backtest_metrics"] == 0
    assert len(payload["run_rows"]) == 1
    assert payload["run_rows"][0]["has_backtest_metrics"] is False
