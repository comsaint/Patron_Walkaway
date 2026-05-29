"""Tests for MLflow adapter run-targeted artifact upload helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from trainer_hightier.core import mlflow_adapter as ma


def test_log_artifact_to_run_safe_returns_false_when_unavailable() -> None:
    """Skip upload when MLflow is unavailable."""
    with patch.object(ma, "is_mlflow_available", return_value=False):
        assert ma.log_artifact_to_run_safe("run-1", "/tmp/x.zip", artifact_path="prod_diag/x.zip") is False


def test_resolve_mlflow_run_id_by_name() -> None:
    """Resolve latest FINISHED run_id by run_name."""
    mock_run = MagicMock()
    mock_run.info.run_id = "abc123"
    mock_client = MagicMock()
    mock_client.get_experiment_by_name.return_value = MagicMock(experiment_id="exp1")
    mock_client.search_runs.return_value = [mock_run]
    with patch.object(ma, "is_mlflow_available", return_value=True):
        with patch("mlflow.tracking.MlflowClient", return_value=mock_client):
            run_id = ma.resolve_mlflow_run_id_by_name("exp/name", "20260527-080231-e71b79e")
    assert run_id == "abc123"


def test_log_artifact_to_run_safe_success() -> None:
    """Upload returns True on client success."""
    mock_client = MagicMock()
    with patch.object(ma, "is_mlflow_available", return_value=True):
        with patch("mlflow.tracking.MlflowClient", return_value=mock_client):
            ok = ma.log_artifact_to_run_safe(
                "run-1",
                "C:/tmp/out.zip",
                artifact_path="prod_diag/out.zip",
            )
    assert ok is True
    mock_client.log_artifact.assert_called_once()
