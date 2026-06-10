"""Tests for MLflow adapter run-targeted artifact upload helpers."""

from __future__ import annotations

import sys
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
    mock_tracking = MagicMock()
    mock_tracking.MlflowClient.return_value = mock_client
    mock_mlflow = MagicMock()
    mock_mlflow.tracking = mock_tracking
    with patch.object(ma, "is_mlflow_available", return_value=True):
        with patch.dict(sys.modules, {"mlflow": mock_mlflow, "mlflow.tracking": mock_tracking}):
            run_id = ma.resolve_mlflow_run_id_by_name("exp/name", "20260527-080231-e71b79e")
    assert run_id == "abc123"


def test_log_artifact_to_run_safe_success() -> None:
    """Upload returns True on client success."""
    mock_client = MagicMock()
    mock_tracking = MagicMock()
    mock_tracking.MlflowClient.return_value = mock_client
    with patch.object(ma, "is_mlflow_available", return_value=True):
        with patch.dict(sys.modules, {"mlflow": MagicMock(), "mlflow.tracking": mock_tracking}):
            ok = ma.log_artifact_to_run_safe(
                "run-1",
                "C:/tmp/out.zip",
                artifact_path="prod_diag/out.zip",
            )
    assert ok is True
    mock_client.log_artifact.assert_called_once()


def test_safe_start_run_swallows_unicode_encode_error_on_exit() -> None:
    """Teardown must not fail when MLflow prints emoji to cp932 stdout."""
    inner_cm = MagicMock()
    inner_cm.__enter__.return_value = MagicMock()
    inner_cm.__exit__.side_effect = UnicodeEncodeError(
        "cp932",
        "\U0001f3c3",
        0,
        1,
        "illegal multibyte sequence",
    )
    mock_mlflow = MagicMock()
    mock_mlflow.start_run.return_value = inner_cm
    with patch.object(ma, "is_mlflow_available", return_value=True):
        with patch.object(ma, "_configure_stdio_replace_on_encode_error"):
            with patch.dict(sys.modules, {"mlflow": mock_mlflow}):
                with ma.safe_start_run(run_name="20260609-test"):
                    pass
    inner_cm.__exit__.assert_called_once()


def test_end_run_safe_swallows_unicode_encode_error() -> None:
    """``end_run`` teardown must not fail when MLflow prints emoji to cp932 stdout."""
    mock_mlflow = MagicMock()
    mock_mlflow.active_run.return_value = MagicMock()
    mock_mlflow.end_run.side_effect = UnicodeEncodeError(
        "cp932",
        "\U0001f3c3",
        0,
        1,
        "illegal multibyte sequence",
    )
    with patch.object(ma, "is_mlflow_available", return_value=True):
        with patch.object(ma, "_configure_stdio_replace_on_encode_error"):
            with patch.dict(sys.modules, {"mlflow": mock_mlflow}):
                ma.end_run_safe()
    mock_mlflow.end_run.assert_called_once()


def test_configure_stdio_replace_on_encode_error_is_idempotent() -> None:
    """Stdio guard should only reconfigure streams once per process."""
    ma._stdio_encode_guard_configured = False
    mock_stdout = MagicMock()
    mock_stderr = MagicMock()
    with patch.object(ma, "sys") as mock_sys:
        mock_sys.stdout = mock_stdout
        mock_sys.stderr = mock_stderr
        ma._configure_stdio_replace_on_encode_error()
        ma._configure_stdio_replace_on_encode_error()
    assert mock_stdout.reconfigure.call_count == 1
    assert mock_stderr.reconfigure.call_count == 1
    ma._stdio_encode_guard_configured = False
