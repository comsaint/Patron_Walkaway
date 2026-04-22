from __future__ import annotations

import json
from pathlib import Path

from trainer.scripts.build_field_test_objective_precondition import run


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run_builder(tmp_path: Path, *, extra_args: list[str] | None = None) -> dict:
    fold1 = tmp_path / "fold1.json"
    fold2 = tmp_path / "fold2.json"
    out_json = tmp_path / "precondition.json"
    out_md = tmp_path / "precondition.md"

    _write_json(
        fold1,
        {
            "test_positives": 100,
            "test_samples": 1000,
            "test_neg_pos_ratio": 9.0,
            "tp": 20,
            "window_hours": 24,
            "threshold_at_recall_0.001": 0.96,
            "threshold_at_recall_0.01": 0.90,
            "threshold_at_recall_0.1": 0.80,
            "threshold_at_recall_0.5": 0.55,
        },
    )
    _write_json(
        fold2,
        {
            "test_positives": 10,
            "test_samples": 50,
            "test_neg_pos_ratio": 4.0,
            "tp": 1,
            "window_hours": 24,
            "threshold_at_recall_0.01": None,
        },
    )

    args = [
        "--fold-metrics-json",
        str(fold1),
        "--fold-metrics-json",
        str(fold2),
        "--run-id",
        "test_run",
        "--start-ts",
        "2026-04-01T00:00:00+08:00",
        "--end-ts",
        "2026-04-08T00:00:00+08:00",
        "--production-neg-pos-ratio",
        "20",
        "--output-json",
        str(out_json),
        "--output-md",
        str(out_md),
    ]
    if extra_args:
        args.extend(extra_args)

    rc = run(args)
    assert rc == 0
    return json.loads(out_json.read_text(encoding="utf-8"))


def test_insufficient_fold_count_respects_cli_threshold(tmp_path: Path) -> None:
    output = _run_builder(tmp_path, extra_args=["--min-t-feasible-size", "5"])
    assert output["t_feasible_stats"]["insufficient_fold_count"] == 1


def test_test_neg_pos_ratio_uses_weighted_average(tmp_path: Path) -> None:
    output = _run_builder(tmp_path)
    # weighted by test_samples: (9*1000 + 4*50) / 1050 = 8.7619...
    assert abs(float(output["test_neg_pos_ratio"]) - 8.7619047619) < 1e-6


def test_single_override_rejected_when_blockers_exist(tmp_path: Path) -> None:
    output = _run_builder(
        tmp_path,
        extra_args=["--objective-decision-override", "single_constrained"],
    )
    assert output["objective_decision"] == "composite"
    assert output["single_objective_allowed"] is False


def test_execution_plan_uses_any_fold_gate_wording() -> None:
    plan_path = Path("c:/Users/longp/Patron_Walkaway/.cursor/plans/EXECUTION PLAN - Precision Uplift.md")
    text = plan_path.read_text(encoding="utf-8")
    assert "若多數 folds 的 `T_feasible` 過小或常為空，不得硬切單一 constrained objective。" not in text
    assert "若任一 fold 的 `T_feasible` 過小或常為空，不得硬切單一 constrained objective。" in text

