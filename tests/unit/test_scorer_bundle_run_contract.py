"""W2: scorer load_dual_artifacts exposes bundle run contract (selection_mode, ...)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import joblib

import trainer.core.config as core_config
from trainer.serving import scorer as scorer_mod


def test_load_dual_artifacts_run_contract_from_training_metrics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        joblib.dump({"model": None, "threshold": 0.5, "features": []}, root / "model.pkl")
        (root / "training_metrics.json").write_text(
            json.dumps({"selection_mode": "field_test", "model_version": "t"}),
            encoding="utf-8",
        )
        with mock.patch.object(scorer_mod, "FEATURE_SPEC_PATH", Path("/nonexistent/features.yaml")):
            art = scorer_mod.load_dual_artifacts(root)
    assert art["selection_mode"] == "field_test"
    assert art["selection_mode_source"] == "artifact_training_metrics.json"
    assert "production_neg_pos_ratio" in art


def test_load_dual_artifacts_run_contract_prefers_v2_when_both_exist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        joblib.dump({"model": None, "threshold": 0.5, "features": []}, root / "model.pkl")
        (root / "training_metrics.json").write_text(
            json.dumps({"selection_mode": "legacy", "model_version": "v1"}),
            encoding="utf-8",
        )
        (root / "training_metrics.v2.json").write_text(
            json.dumps(
                {"schema_version": "training-metrics.v2", "selection_mode": "field_test"}
            ),
            encoding="utf-8",
        )
        with mock.patch.object(scorer_mod, "FEATURE_SPEC_PATH", Path("/nonexistent/features.yaml")):
            art = scorer_mod.load_dual_artifacts(root)
    assert art["selection_mode"] == "field_test"
    assert art["selection_mode_source"] == "artifact_training_metrics.v2.json"


def test_load_dual_artifacts_run_contract_config_when_no_tm() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        joblib.dump({"model": None, "threshold": 0.5, "features": []}, root / "model.pkl")
        with mock.patch.object(scorer_mod, "FEATURE_SPEC_PATH", Path("/nonexistent/features.yaml")):
            with mock.patch.object(core_config, "SELECTION_MODE", "legacy"):
                art = scorer_mod.load_dual_artifacts(root)
    assert art["selection_mode"] == "legacy"
    assert art["selection_mode_source"] == "config"
