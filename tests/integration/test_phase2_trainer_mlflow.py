"""
Phase 2 T2: Integration tests for trainer MLflow provenance.

- Trainer does not crash when MLFLOW_TRACKING_URI is unset (provenance no-op).
- When MLflow is available (mocked), provenance params are passed to log_params_safe.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from trainer.core import mlflow_utils
from trainer.training import trainer as trainer_mod


def _call_log_provenance(git_commit=None, artifact_dir="/art", **kwargs):
    defaults = {
        "model_version": "v1",
        "training_window_start": "2026-01-01",
        "training_window_end": "2026-01-07",
        "feature_spec_path": "spec.yaml",
        "training_metrics_path": "/art/training_metrics.json",
    }
    defaults.update(kwargs)
    return trainer_mod._log_training_provenance_to_mlflow(
        artifact_dir=artifact_dir,
        git_commit=git_commit,
        **defaults,
    )


class TestTrainerProvenanceMlflowUnavailable(unittest.TestCase):
    """Phase 2 T2: With MLflow unavailable, _log_training_provenance_to_mlflow completes without raise."""

    def setUp(self):
        mlflow_utils.reset_availability_cache()
        os.environ.pop("MLFLOW_TRACKING_URI", None)

    def test_log_training_provenance_completes_when_uri_unset(self):
        """_log_training_provenance_to_mlflow returns without raising when URI unset."""
        trainer_mod._log_training_provenance_to_mlflow(
            model_version="test-v",
            artifact_dir="/tmp/m",
            training_window_start="2026-01-01T00:00:00",
            training_window_end="2026-01-07T00:00:00",
            feature_spec_path="/tmp/spec.yaml",
            training_metrics_path="/tmp/m/training_metrics.json",
        )


class TestTrainerProvenanceParamsPayload(unittest.TestCase):
    """Phase 2 T2: When MLflow available (mocked), provenance params dict is passed to log_params_safe."""

    def test_provenance_params_contain_required_keys(self):
        """Params passed to log_params_safe contain schema keys (model_version, etc.)."""
        with patch.object(trainer_mod, "safe_start_run") as mock_start:
            with patch.object(trainer_mod, "log_params_safe") as mock_log:
                from contextlib import nullcontext
                mock_start.return_value = nullcontext()
                trainer_mod._log_training_provenance_to_mlflow(
                    model_version="v1",
                    artifact_dir="/art",
                    training_window_start="2026-01-01",
                    training_window_end="2026-01-07",
                    feature_spec_path="spec.yaml",
                    training_metrics_path="/art/training_metrics.json",
                    git_commit="abc1234",
                )
                mock_log.assert_called_once()
                (params,) = mock_log.call_args[0]
                self.assertIn("model_version", params)
                self.assertEqual(params["model_version"], "v1")
                self.assertIn("git_commit", params)
                self.assertIn("training_window_start", params)
                self.assertIn("training_window_end", params)
                self.assertIn("artifact_dir", params)
                self.assertIn("feature_spec_path", params)
                self.assertIn("training_metrics_path", params)

    def test_safe_start_run_called_with_run_name_model_version(self):
        """Code Review §3: safe_start_run must be called with run_name=model_version."""
        with patch.object(trainer_mod, "safe_start_run") as mock_start:
            with patch.object(trainer_mod, "log_params_safe"):
                from contextlib import nullcontext
                mock_start.return_value = nullcontext()
                trainer_mod._log_training_provenance_to_mlflow(
                    model_version="20260101-120000-abc1234",
                    artifact_dir="/art",
                    training_window_start="2026-01-01",
                    training_window_end="2026-01-07",
                    feature_spec_path="spec.yaml",
                    training_metrics_path="/art/training_metrics.json",
                    git_commit="abc1234",
                )
                mock_start.assert_called_once()
                self.assertEqual(mock_start.call_args[1].get("run_name"), "20260101-120000-abc1234")


class TestLogProvenanceGitFallback(unittest.TestCase):
    """Code Review §1: When git fails (e.g. subprocess raises), git_commit becomes 'nogit' and no raise."""

    def test_git_failure_sets_git_commit_nogit_and_does_not_raise(self):
        with patch.object(trainer_mod, "safe_start_run") as mock_start:
            with patch.object(trainer_mod, "log_params_safe") as mock_log:
                with patch.object(trainer_mod.subprocess, "check_output", side_effect=FileNotFoundError("git not found")):
                    from contextlib import nullcontext
                    mock_start.return_value = nullcontext()
                    _call_log_provenance(git_commit=None)
                mock_log.assert_called_once()
                (params,) = mock_log.call_args[0]
                self.assertEqual(params["git_commit"], "nogit")


class TestLogProvenanceLongArtifactDir(unittest.TestCase):
    """Code Review §2: Very long artifact_dir still results in a single log_params_safe call (no crash)."""

    def test_long_artifact_dir_log_params_safe_called_once(self):
        long_path = "C:\\" + "x" * 600
        with patch.object(trainer_mod, "safe_start_run") as mock_start:
            with patch.object(trainer_mod, "log_params_safe") as mock_log:
                from contextlib import nullcontext
                mock_start.return_value = nullcontext()
                _call_log_provenance(artifact_dir=long_path, git_commit="abc")
                mock_log.assert_called_once()
                (params,) = mock_log.call_args[0]
                self.assertEqual(params["artifact_dir"], long_path)
