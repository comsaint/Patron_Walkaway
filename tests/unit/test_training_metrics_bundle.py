"""Tests for training_metrics v2-first bundle loader."""

from __future__ import annotations

import json
from pathlib import Path

from trainer.core.bundle_run_contract import read_bundle_run_contract_block
from trainer.core.training_metrics_bundle import load_training_metrics_for_contract, load_training_metrics_merged


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def test_load_training_metrics_merged_v2_only_flattens_datasets(tmp_path: Path) -> None:
    _write(
        tmp_path / "training_metrics.v2.json",
        {
            "schema_version": "training-metrics.v2",
            "model_version": "mv",
            "selection_mode": "field_test",
            "production_neg_pos_ratio": 10.0,
            "datasets": {
                "test": {
                    "precision": 0.71,
                    "precision_prod_adjusted": 0.45,
                    "recall": 0.1,
                }
            },
            "selection": {
                "optuna_hpo_objective_mode": "field_test_dec026_val_precision_prod_adj",
                "optuna_hpo_study_best_trial_value": 0.42,
            },
        },
    )
    src, flat = load_training_metrics_merged(tmp_path)
    assert src == "training_metrics.v2.json"
    assert flat["selection_mode"] == "field_test"
    assert flat["test_precision"] == 0.71
    assert flat["test_precision_prod_adjusted"] == 0.45
    assert flat["optuna_hpo_objective_mode"] == "field_test_dec026_val_precision_prod_adj"


def test_load_training_metrics_merged_v1_nested_rated(tmp_path: Path) -> None:
    _write(
        tmp_path / "training_metrics.json",
        {
            "model_version": "m1",
            "selection_mode": "legacy",
            "rated": {"test_precision": 0.5, "val_ap": 0.3},
        },
    )
    src, flat = load_training_metrics_merged(tmp_path)
    assert src == "training_metrics.json"
    assert flat["test_precision"] == 0.5
    assert flat["selection_mode"] == "legacy"


def test_load_training_metrics_merged_v3_first_merges_and_overrides_v2(tmp_path: Path) -> None:
    _write(
        tmp_path / "training_metrics.v2.json",
        {
            "schema_version": "training-metrics.v2",
            "model_version": "mv2",
            "selection_mode": "field_test",
            "production_neg_pos_ratio": 10.0,
            "datasets": {
                "test": {"precision": 0.71, "recall": 0.1},
            },
            "selection": {"optuna_hpo_objective_mode": "from_v2"},
        },
    )
    _write(
        tmp_path / "training_metrics.v3.json",
        {
            "schema_version": "training-metrics.v3",
            "model_version": "mv3",
            "selection_mode": "field_test",
            "production_neg_pos_ratio": 10.0,
            "objective_contract": {
                "selection_metric_id": "x",
                "threshold": {"selected": 0.4, "recall_floor": 0.01},
                "constraints": {},
                "gate": {},
                "ratio_assumption": {},
                "observed_split_ratios": {},
            },
            "datasets": {
                "test": {
                    "precision": 0.88,
                    "recall": 0.2,
                    "field_test": {
                        "precision_raw": 0.88,
                        "precision_used_for_selection": 0.88,
                        "precision_prod_adjusted": None,
                        "precision_type": "raw",
                    },
                }
            },
            "segmentation": {"enabled": False},
            "selection": {},
            "execution": {"optuna_hpo_objective_mode": "from_v3_exec"},
        },
    )
    src, flat = load_training_metrics_merged(tmp_path)
    assert src == "training_metrics.v3.json"
    assert flat["test_precision"] == 0.88
    assert flat["optuna_hpo_objective_mode"] == "from_v3_exec"


def test_load_training_metrics_for_contract_prefers_v2(tmp_path: Path) -> None:
    _write(
        tmp_path / "training_metrics.v2.json",
        {"schema_version": "training-metrics.v2", "selection_mode": "field_test"},
    )
    _write(tmp_path / "training_metrics.json", {"selection_mode": "legacy"})
    tm, label = load_training_metrics_for_contract(tmp_path)
    assert tm is not None
    assert tm["selection_mode"] == "field_test"
    assert label == "artifact_training_metrics.v2.json"


def test_load_training_metrics_for_contract_prefers_v3(tmp_path: Path) -> None:
    _write(
        tmp_path / "training_metrics.v3.json",
        {"schema_version": "training-metrics.v3", "selection_mode": "field_test_v3"},
    )
    _write(
        tmp_path / "training_metrics.v2.json",
        {"schema_version": "training-metrics.v2", "selection_mode": "field_test"},
    )
    tm, label = load_training_metrics_for_contract(tmp_path)
    assert tm is not None
    assert tm["selection_mode"] == "field_test_v3"
    assert label == "artifact_training_metrics.v3.json"


def test_read_bundle_run_contract_block_uses_v2_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "trainer.core.config.SELECTION_MODE",
        "legacy",
        raising=False,
    )
    monkeypatch.setattr(
        "trainer.core.config.PRODUCTION_NEG_POS_RATIO",
        None,
        raising=False,
    )
    _write(
        tmp_path / "training_metrics.v2.json",
        {"schema_version": "training-metrics.v2", "selection_mode": "field_test"},
    )
    _write(tmp_path / "training_metrics.json", {"selection_mode": "should_not_win"})
    out = read_bundle_run_contract_block(tmp_path)
    assert out["selection_mode"] == "field_test"
    assert out["selection_mode_source"] == "artifact_training_metrics.v2.json"


def test_read_bundle_run_contract_block_uses_v3_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "trainer.core.config.SELECTION_MODE",
        "legacy",
        raising=False,
    )
    monkeypatch.setattr(
        "trainer.core.config.PRODUCTION_NEG_POS_RATIO",
        None,
        raising=False,
    )
    _write(
        tmp_path / "training_metrics.v3.json",
        {"schema_version": "training-metrics.v3", "selection_mode": "field_test_v3"},
    )
    _write(
        tmp_path / "training_metrics.v2.json",
        {"schema_version": "training-metrics.v2", "selection_mode": "field_test"},
    )
    out = read_bundle_run_contract_block(tmp_path)
    assert out["selection_mode"] == "field_test_v3"
    assert out["selection_mode_source"] == "artifact_training_metrics.v3.json"


def test_report_w2_row_from_run_dir_v2_only(tmp_path: Path) -> None:
    from trainer.scripts.report_w2_objective_parity import row_from_run_dir

    run = tmp_path / "r1"
    _write(
        run / "training_metrics.v2.json",
        {
            "schema_version": "training-metrics.v2",
            "model_version": "m2",
            "selection_mode": "field_test",
            "datasets": {
                "test": {
                    "precision": 0.31,
                    "precision_prod_adjusted": 0.28,
                    "recall": 0.12,
                }
            },
            "selection": {
                "optuna_hpo_objective_mode": "field_test_dec026_val_precision_prod_adj",
                "optuna_hpo_study_best_trial_value": 0.44,
            },
        },
    )
    _write(
        run / "backtest_metrics.json",
        {
            "selection_mode": "field_test",
            "optuna": {"test_precision_prod_adjusted": 0.24, "test_recall": 0.13},
        },
    )
    row = row_from_run_dir(run)
    assert row.run_id == "m2"
    assert row.selection_mode_train == "field_test"
    assert row.train_test_precision == 0.31
    assert row.train_test_precision_prod_adjusted == 0.28
