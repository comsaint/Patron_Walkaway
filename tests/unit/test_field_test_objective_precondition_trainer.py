from __future__ import annotations

import json
from pathlib import Path

from trainer.training.field_test_objective_precondition import (
    training_metrics_overlay_from_precondition,
    try_load_precondition_json,
)


def test_try_load_precondition_json_missing(tmp_path: Path) -> None:
    assert try_load_precondition_json(tmp_path / "nope.json") is None


def test_try_load_precondition_json_valid(tmp_path: Path) -> None:
    p = tmp_path / "pre.json"
    p.write_text(json.dumps({"blocking_reasons": [], "objective_decision": "single_constrained"}), encoding="utf-8")
    doc = try_load_precondition_json(p)
    assert doc is not None
    assert doc["objective_decision"] == "single_constrained"


def test_try_load_precondition_json_invalid_array(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert try_load_precondition_json(p) is None


def test_training_metrics_overlay_truncates_blocking(tmp_path: Path) -> None:
    reasons = [f"r{i}" for i in range(20)]
    doc = {
        "objective_decision": "composite",
        "single_objective_allowed": False,
        "blocking_reasons": reasons,
    }
    overlay = training_metrics_overlay_from_precondition(doc, source_path=str(tmp_path / "x.json"), max_blocking_list=5)
    assert overlay["field_test_precondition_blocking_reason_count"] == 20
    assert overlay["field_test_precondition_blocking_reasons_head"].count(";") == 4
    assert overlay["field_test_constrained_optuna_objective_allowed"] is False


def test_training_metrics_overlay_malformed_defaults() -> None:
    doc: dict = {}
    overlay = training_metrics_overlay_from_precondition(doc, source_path="/tmp/p.json")
    assert overlay["field_test_objective_decision"] == "unknown"
    assert overlay["field_test_single_objective_allowed"] is True
    assert overlay["field_test_precondition_blocking_reason_count"] == 0
