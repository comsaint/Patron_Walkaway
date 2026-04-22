from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import trainer.training.field_test_objective_precondition as ftp

from trainer.training.field_test_objective_precondition import (
    build_field_test_precondition_for_orchestration,
    expand_repo_relative_json_globs,
    precondition_constrained_optuna_allowed,
    trainer_env_updates_from_precondition_manifest,
    training_metrics_overlay_from_precondition,
    try_load_precondition_json,
)
from trainer.training.trainer import train_dual_model


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


def test_precondition_constrained_optuna_allowed_none() -> None:
    assert precondition_constrained_optuna_allowed(None) is True


def test_precondition_constrained_optuna_allowed_matches_overlay_gate() -> None:
    doc = {
        "objective_decision": "single_constrained",
        "single_objective_allowed": True,
        "blocking_reasons": [],
    }
    assert precondition_constrained_optuna_allowed(doc) is True
    overlay = training_metrics_overlay_from_precondition(doc, source_path="/x.json")
    assert overlay["field_test_constrained_optuna_objective_allowed"] is precondition_constrained_optuna_allowed(doc)


def test_precondition_constrained_optuna_allowed_false_when_blockers() -> None:
    doc = {"blocking_reasons": ["x"], "single_objective_allowed": True}
    assert precondition_constrained_optuna_allowed(doc) is False


def test_precondition_constrained_optuna_allowed_false_when_schema_bad() -> None:
    doc = {"blocking_reasons": "not-a-list", "single_objective_allowed": True}
    assert precondition_constrained_optuna_allowed(doc) is False
    overlay = training_metrics_overlay_from_precondition(doc, source_path="/x.json")
    assert overlay["field_test_constrained_optuna_objective_allowed"] is False


def test_training_metrics_overlay_malformed_defaults() -> None:
    doc: dict = {}
    overlay = training_metrics_overlay_from_precondition(doc, source_path="/tmp/p.json")
    assert overlay["field_test_objective_decision"] == "unknown"
    assert overlay["field_test_single_objective_allowed"] is True
    assert overlay["field_test_precondition_blocking_reason_count"] == 0
    assert overlay["field_test_precondition_blocking_reasons_schema_ok"] is True


def test_training_metrics_overlay_blocking_reasons_wrong_type() -> None:
    doc = {"blocking_reasons": "not-a-list", "objective_decision": "single_constrained"}
    overlay = training_metrics_overlay_from_precondition(doc, source_path="/x.json")
    assert overlay["field_test_precondition_blocking_reasons_schema_ok"] is False
    assert overlay["field_test_single_objective_allowed"] is False
    assert overlay["field_test_constrained_optuna_objective_allowed"] is False


def test_try_load_precondition_json_rejects_large_file(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setattr(ftp, "MAX_PRECONDITION_JSON_BYTES", 5)
    p = tmp_path / "big.json"
    p.write_text('{"a":1}', encoding="utf-8")
    assert ftp.try_load_precondition_json(p) is None


def test_expand_repo_relative_json_globs_collects_under_repo(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir(parents=True)
    (tmp_path / "nested" / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "nested" / "b.txt").write_text("x", encoding="utf-8")
    (tmp_path / "nested" / "c.json").write_text("{}", encoding="utf-8")
    found, meta = expand_repo_relative_json_globs(tmp_path, ["nested/**/*.json"])
    assert len(found) == 2
    assert meta["matched_unique_count"] == 2
    assert not meta.get("truncated")


def test_expand_repo_relative_json_globs_truncates(tmp_path: Path) -> None:
    (tmp_path / "g").mkdir()
    for i in range(5):
        (tmp_path / "g" / f"f{i}.json").write_text("{}", encoding="utf-8")
    found, meta = expand_repo_relative_json_globs(tmp_path, ["g/*.json"], max_files=2)
    assert len(found) == 2
    assert meta.get("truncated") is True


def test_build_field_test_precondition_for_orchestration_smoke(tmp_path: Path) -> None:
    f1 = tmp_path / "f1.json"
    f2 = tmp_path / "f2.json"
    f1.write_text(
        json.dumps(
            {
                "test_positives": 100,
                "test_samples": 1000,
                "test_neg_pos_ratio": 9.0,
                "tp": 20,
                "window_hours": 24,
                "threshold_at_recall_0.01": 0.9,
                "threshold_at_recall_0.1": 0.8,
                "threshold_at_recall_0.5": 0.5,
            }
        ),
        encoding="utf-8",
    )
    f2.write_text(
        json.dumps(
            {
                "test_positives": 50,
                "test_samples": 500,
                "test_neg_pos_ratio": 8.0,
                "tp": 10,
                "window_hours": 24,
                "threshold_at_recall_0.01": 0.85,
                "threshold_at_recall_0.1": 0.75,
                "threshold_at_recall_0.5": 0.45,
            }
        ),
        encoding="utf-8",
    )
    manifest = build_field_test_precondition_for_orchestration(
        tmp_path,
        run_id="orch_smoke",
        start_ts="2026-04-01T00:00:00+08:00",
        end_ts="2026-04-08T00:00:00+08:00",
        fold_metrics_abs_paths=[f1.resolve(), f2.resolve()],
        production_neg_pos_ratio=20.0,
    )
    assert manifest.get("applied") is True
    out = Path(str(manifest["output_json"]))
    assert out.is_file()
    env_u = trainer_env_updates_from_precondition_manifest(manifest)
    assert "FIELD_TEST_OBJECTIVE_PRECONDITION_JSON" in env_u
    assert Path(env_u["FIELD_TEST_OBJECTIVE_PRECONDITION_JSON"]) == out.resolve()


def test_train_dual_model_writes_field_test_overlay_on_rated_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pre = tmp_path / "pre.json"
    pre.write_text(
        json.dumps(
            {
                "blocking_reasons": [],
                "single_objective_allowed": True,
                "objective_decision": "single_constrained",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ftp.FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV, str(pre))

    train_df = pd.DataFrame(
        {
            "is_rated": [True] * 12 + [False] * 8,
            "label": ([1] * 4 + [0] * 8) + ([0] * 4 + [1] * 4),
            "f1": range(20),
        }
    )
    valid_df = pd.DataFrame(
        {
            "is_rated": [True] * 6 + [False] * 4,
            "label": ([1] * 2 + [0] * 4) + ([0] * 2 + [1] * 2),
            "f1": range(10, 20),
        }
    )
    _, _, combined = train_dual_model(train_df, valid_df, ["f1"], run_optuna=False)
    assert combined["rated"] is not None
    assert combined["rated"]["field_test_constrained_optuna_objective_allowed"] is True
    assert combined["nonrated"] is not None
    assert "field_test_objective_decision" not in combined["nonrated"]
