"""
Phase 2 T2: Review/contract tests for trainer MLflow provenance (no production changes).

- run_pipeline calls _log_training_provenance_to_mlflow after save_artifact_bundle.
- _log_training_provenance_to_mlflow does not raise when MLflow is unavailable (safe no-op).
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime
from unittest.mock import patch

from trainer.training import trainer as trainer_mod


def _run_pipeline_src() -> str:
    return inspect.getsource(trainer_mod.run_pipeline)


def _log_provenance_src() -> str:
    """Return source of _log_training_provenance_to_mlflow (Code Review §5: use in contract test)."""
    return inspect.getsource(trainer_mod._log_training_provenance_to_mlflow)


class TestLogProvenanceHelperContract(unittest.TestCase):
    """Code Review §5: _log_provenance_src used; helper implementation uses mlflow_utils."""

    def test_log_provenance_source_uses_safe_start_run_and_log_params_safe(self):
        """_log_training_provenance_to_mlflow source must use safe_start_run and log_params_safe."""
        src = _log_provenance_src()
        self.assertIn("safe_start_run", src, "_log_training_provenance_to_mlflow should use safe_start_run")
        self.assertIn("log_params_safe", src, "_log_training_provenance_to_mlflow should use log_params_safe")


class TestRunPipelineCallsProvenanceAfterSaveArtifact(unittest.TestCase):
    """Phase 2 T2: run_pipeline must call _log_training_provenance_to_mlflow after save_artifact_bundle."""

    def test_run_pipeline_calls_log_training_provenance_after_save_artifact_bundle(self):
        src = _run_pipeline_src()
        save_idx = src.find("save_artifact_bundle(")
        self.assertGreater(save_idx, 0, "run_pipeline should call save_artifact_bundle")
        after_save = src[save_idx:]
        self.assertIn(
            "_log_training_provenance_to_mlflow",
            after_save,
            "run_pipeline should call _log_training_provenance_to_mlflow after save_artifact_bundle (Phase 2 T2).",
        )

    def test_run_pipeline_wraps_provenance_call_in_try_except(self):
        src = _run_pipeline_src()
        self.assertIn("_log_training_provenance_to_mlflow", src)
        # Provenance block should be in try/except so training still succeeds on MLflow failure.
        prov_idx = src.find("_log_training_provenance_to_mlflow")
        block_before = src[max(0, prov_idx - 200) : prov_idx]
        self.assertIn("try:", block_before, "Provenance logging should be in try block (T2 failure strategy).")


class TestLogTrainingProvenanceToMlflowNoRaise(unittest.TestCase):
    """Phase 2 T2: _log_training_provenance_to_mlflow does not raise when MLflow unavailable."""

    def test_log_provenance_no_raise_when_mlflow_unavailable(self):
        """When safe_start_run returns nullcontext and log_params_safe no-ops, no exception."""
        with patch.object(trainer_mod, "safe_start_run") as mock_start:
            with patch.object(trainer_mod, "log_params_safe") as mock_log:
                from contextlib import nullcontext
                mock_start.return_value = nullcontext()
                trainer_mod._log_training_provenance_to_mlflow(
                    model_version="test-20260101-120000-abc1234",
                    artifact_dir="/tmp/models",
                    training_window_start=datetime(2026, 1, 1),
                    training_window_end=datetime(2026, 1, 7),
                    feature_spec_path="/tmp/feature_spec.yaml",
                    training_metrics_path="/tmp/models/training_metrics.json",
                )
                mock_start.assert_called_once()
                mock_log.assert_called_once()
