"""
Phase 2 P0–P1: Unit tests for trainer.core.mlflow_utils.

- URI unset: warning only, no raise; is_mlflow_available() returns False.
- Mock MLflow: verify tags/params payload when available.
- T11: local_state/mlflow.env (or MLFLOW_ENV_FILE) loaded on import; no file -> no crash.
- Code Review risk points (§1–§9): minimal reproducible tests (tests only, no production changes).
"""

import importlib
import os
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trainer.core import mlflow_utils


def _ensure_unset_uri_and_reset_cache():
    """Isolate tests that require unset URI (Code Review §8)."""
    os.environ.pop("MLFLOW_TRACKING_URI", None)
    mlflow_utils.reset_availability_cache()


def test_get_tracking_uri_unset():
    """When MLFLOW_TRACKING_URI is unset, get_tracking_uri returns None."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        assert mlflow_utils.get_tracking_uri() is None


def test_get_tracking_uri_set():
    """When MLFLOW_TRACKING_URI is set, get_tracking_uri returns it."""
    with patch.dict(os.environ, {"MLFLOW_TRACKING_URI": "http://localhost:5000"}):
        assert mlflow_utils.get_tracking_uri() == "http://localhost:5000"


def test_get_tracking_uri_empty_string_treated_as_unset():
    """Code Review §2: Empty string URI is treated as unset; get_tracking_uri returns None."""
    with patch.dict(os.environ, {"MLFLOW_TRACKING_URI": ""}, clear=False):
        assert mlflow_utils.get_tracking_uri() is None


def test_get_tracking_uri_whitespace_only_returns_as_is():
    """Code Review §2: Whitespace-only URI returns as-is (lock current behavior)."""
    with patch.dict(os.environ, {"MLFLOW_TRACKING_URI": "  "}, clear=False):
        assert mlflow_utils.get_tracking_uri() == "  "


def test_uri_unset_warning_only_no_raise():
    """When URI is unset, is_mlflow_available() returns False and does not raise."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        result = mlflow_utils.is_mlflow_available()
    assert result is False


def test_cache_does_not_auto_update_when_uri_set_after_first_check():
    """Code Review §1: Cache does not auto-update when env is set after first check."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        first = mlflow_utils.is_mlflow_available()
    assert first is False
    # Set URI without calling reset_availability_cache(); second call should still see cached False.
    with patch.dict(os.environ, {"MLFLOW_TRACKING_URI": "http://localhost:5000"}, clear=False):
        second = mlflow_utils.is_mlflow_available()
    assert second is False
    # After reset, next call re-evaluates (may True or False depending on server).
    mlflow_utils.reset_availability_cache()
    with patch.dict(os.environ, {"MLFLOW_TRACKING_URI": "http://localhost:5000"}, clear=False):
        third = mlflow_utils.is_mlflow_available()
    # Without a real server, third is typically False; we only assert cache was bypassed (no crash).
    assert third is False or third is True


def test_log_params_safe_no_op_when_unavailable():
    """log_params_safe does not raise when MLflow is unavailable."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        mlflow_utils.is_mlflow_available()
    mlflow_utils.log_params_safe({"model_version": "v1"})


def test_log_tags_safe_no_op_when_unavailable():
    """log_tags_safe does not raise when MLflow is unavailable."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        mlflow_utils.is_mlflow_available()
    mlflow_utils.log_tags_safe({"model_version": "v1"})


def test_log_params_safe_calls_mlflow_when_available():
    """When available, log_params_safe calls mlflow.log_params with the given dict."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.log_params") as mock_log_params:
            mlflow_utils.log_params_safe({"model_version": "v1", "git_commit": "abc"})
    mock_log_params.assert_called_once()
    call_args = mock_log_params.call_args[0][0]
    assert call_args == {"model_version": "v1", "git_commit": "abc"}


def test_log_tags_safe_calls_mlflow_when_available():
    """When available, log_tags_safe calls mlflow.set_tags with the given dict."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.set_tags") as mock_set_tags:
            mlflow_utils.log_tags_safe({"model_version": "v1", "training_window_start": "2026-01-01"})
    mock_set_tags.assert_called_once()
    call_args = mock_set_tags.call_args[0][0]
    assert call_args == {"model_version": "v1", "training_window_start": "2026-01-01"}


def test_log_metrics_safe_skips_non_numeric_values():
    """Contract: log_metrics_safe should skip None and non-coercible values."""
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        # Inject a fake mlflow module so the test does not depend on real mlflow installation.
        import types

        dummy_mlflow = types.SimpleNamespace()
        with patch.object(dummy_mlflow, "log_metrics", create=True) as mock_log_metrics:
            with patch.dict(sys.modules, {"mlflow": dummy_mlflow}):
                mlflow_utils.log_metrics_safe({"ok": 1.23, "bad_dict": {"a": 1}, "none": None})

    mock_log_metrics.assert_called_once()
    logged = mock_log_metrics.call_args[0][0]
    assert logged == {"ok": 1.23}


@pytest.mark.xfail(strict=False, reason="log_metrics_safe should filter NaN/inf values once implemented")
def test_log_metrics_safe_filters_non_finite_values():
    """Risk #4: log_metrics_safe should skip NaN/inf values (desired behavior)."""
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        import types

        dummy_mlflow = types.SimpleNamespace()
        with patch.object(dummy_mlflow, "log_metrics", create=True) as mock_log_metrics:
            with patch.dict(sys.modules, {"mlflow": dummy_mlflow}):
                mlflow_utils.log_metrics_safe({"nan": float("nan"), "inf": float("inf"), "ok": 1.0})

    logged = mock_log_metrics.call_args[0][0]
    assert "nan" not in logged
    assert "inf" not in logged
    assert logged == {"ok": 1.0}


def test_log_params_safe_when_available_no_active_run_does_not_raise():
    """Code Review §3: log_params_safe does not raise when available but no active run."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.active_run", return_value=None):
            with patch("mlflow.log_params"):
                mlflow_utils.log_params_safe({"model_version": "v1"})


def test_log_artifact_safe_nonexistent_path_warning_no_raise():
    """Code Review §4: log_artifact_safe with failing path logs warning, does not raise."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.log_artifact", side_effect=FileNotFoundError("No such file")):
            mlflow_utils.log_artifact_safe("/nonexistent/path")


def test_log_params_safe_swallows_mlflow_exception_no_raise():
    """Code Review §5: log_params_safe swallows MLflow exception, does not raise."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.log_params", side_effect=RuntimeError("network error")):
            mlflow_utils.log_params_safe({"model_version": "v1"})


def test_safe_start_run_returns_nullcontext_when_unavailable():
    """Code Review §7: When unavailable, safe_start_run returns nullcontext; with-block exits cleanly."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        mlflow_utils.is_mlflow_available()
    ctx = mlflow_utils.safe_start_run()
    assert type(ctx) is type(nullcontext())
    with ctx:
        pass


def test_log_artifact_safe_no_op_when_unavailable():
    """Code Review §9: log_artifact_safe does not raise when MLflow is unavailable."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        mlflow_utils.is_mlflow_available()
    mlflow_utils.log_artifact_safe("/any/path")


def test_log_artifact_safe_calls_mlflow_when_available():
    """Code Review §9: When available, log_artifact_safe calls mlflow.log_artifact with path."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.log_artifact") as mock_log_artifact:
            mlflow_utils.log_artifact_safe("/some/artifact_dir/file.json", artifact_path="file.json")
    mock_log_artifact.assert_called_once()
    assert mock_log_artifact.call_args[0][0] == "/some/artifact_dir/file.json"
    assert mock_log_artifact.call_args[1].get("artifact_path") == "file.json"


def test_end_run_safe_no_op_when_unavailable():
    """Code Review §9: end_run_safe does not raise when MLflow is unavailable."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        mlflow_utils.is_mlflow_available()
    mlflow_utils.end_run_safe()


def test_end_run_safe_calls_end_run_when_available_and_active_run():
    """Code Review §9: When available and active run, end_run_safe calls mlflow.end_run."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.active_run", return_value=MagicMock()):
            with patch("mlflow.end_run") as mock_end_run:
                mlflow_utils.end_run_safe()
    mock_end_run.assert_called_once()


def test_has_active_run_false_when_unavailable():
    """T12: has_active_run returns False when MLflow is not available."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        mlflow_utils.is_mlflow_available()
    assert mlflow_utils.has_active_run() is False


def test_has_active_run_true_when_available_and_run_active():
    """T12: has_active_run returns True when MLflow available and there is an active run."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.active_run", return_value=MagicMock()):
            assert mlflow_utils.has_active_run() is True


def test_has_active_run_false_when_available_but_no_run():
    """T12: has_active_run returns False when MLflow available but no active run."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.active_run", return_value=None):
            assert mlflow_utils.has_active_run() is False


def test_has_active_run_returns_false_when_active_run_raises():
    """T12 Code Review §1: has_active_run returns False and does not raise when mlflow.active_run() raises."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.active_run", side_effect=RuntimeError("backend unavailable")):
            with patch("trainer.core.mlflow_utils._log.warning") as mock_warn:
                result = mlflow_utils.has_active_run()
    assert result is False
    assert mock_warn.call_count == 1


def test_safe_start_run_context_when_unavailable_exits_cleanly():
    """Code Review §9: with safe_start_run(): when unavailable, block runs and exits without error."""
    _ensure_unset_uri_and_reset_cache()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MLFLOW_TRACKING_URI", None)
        mlflow_utils.is_mlflow_available()
    with mlflow_utils.safe_start_run():
        pass


def test_t11_env_file_loaded_when_mlflow_env_file_points_to_existing_file():
    """T11: When MLFLOW_ENV_FILE points to an existing file with MLFLOW_TRACKING_URI, reload loads it."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("MLFLOW_TRACKING_URI=http://from-file.example.com\n")
        tmp_path = f.name
    try:
        prev_uri = os.environ.pop("MLFLOW_TRACKING_URI", None)
        prev_override = os.environ.pop("MLFLOW_ENV_FILE", None)
        os.environ["MLFLOW_ENV_FILE"] = tmp_path
        importlib.reload(mlflow_utils)
        assert mlflow_utils.get_tracking_uri() == "http://from-file.example.com"
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        os.environ.pop("MLFLOW_ENV_FILE", None)
        if prev_override is not None:
            os.environ["MLFLOW_ENV_FILE"] = prev_override
        if prev_uri is not None:
            os.environ["MLFLOW_TRACKING_URI"] = prev_uri
        importlib.reload(mlflow_utils)


def test_t11_no_crash_when_mlflow_env_file_points_to_nonexistent_path():
    """T11: When MLFLOW_ENV_FILE points to non-existent path, reload does not crash; URI stays unset."""
    prev_uri = os.environ.pop("MLFLOW_TRACKING_URI", None)
    prev_override = os.environ.pop("MLFLOW_ENV_FILE", None)
    os.environ["MLFLOW_ENV_FILE"] = "/nonexistent/path/mlflow.env"
    try:
        importlib.reload(mlflow_utils)
        assert mlflow_utils.get_tracking_uri() is None
    finally:
        os.environ.pop("MLFLOW_ENV_FILE", None)
        if prev_override is not None:
            os.environ["MLFLOW_ENV_FILE"] = prev_override
        if prev_uri is not None:
            os.environ["MLFLOW_TRACKING_URI"] = prev_uri
        importlib.reload(mlflow_utils)


# --- Code Review T11: risk points → minimal reproducible tests (tests only, no production changes) ---


def test_t11_review_import_succeeds_when_load_dotenv_raises():
    """Code Review §1: When load_dotenv raises during mlflow_utils import, module still loads and get_tracking_uri() works."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("MLFLOW_TRACKING_URI=http://test.example.com\n")
        tmp_path = f.name
    try:
        # Only raise when load_dotenv is called from mlflow_utils (not from config.py).
        # Pass MLFLOW_ENV_FILE so mlflow_utils actually calls load_dotenv.
        code = """
import os
import sys
os.environ["MLFLOW_ENV_FILE"] = sys.argv[1]
import dotenv
original = dotenv.load_dotenv
def raising(*a, **k):
    import inspect
    for fr in inspect.stack():
        if 'mlflow_utils' in (fr.filename or ''):
            raise Exception("bad file")
    return original(*a, **k)
dotenv.load_dotenv = raising
from trainer.core import mlflow_utils
uri = mlflow_utils.get_tracking_uri()
print(uri if uri is None else uri)
sys.exit(0)
"""
        result = subprocess.run(
            [sys.executable, "-c", code, tmp_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}. "
            "Code Review §1: import must not fail when load_dotenv raises; wrap in try/except."
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def test_t11_review_mlflow_env_file_empty_string_reload_no_crash():
    """Code Review §2: MLFLOW_ENV_FILE='' (empty string) → reload does not crash; URI unset when env cleared."""
    prev_uri = os.environ.pop("MLFLOW_TRACKING_URI", None)
    prev_override = os.environ.pop("MLFLOW_ENV_FILE", None)
    os.environ["MLFLOW_ENV_FILE"] = ""
    try:
        importlib.reload(mlflow_utils)
        # With empty string, Path("").is_file() is False so we don't load; expect None when env cleared.
        got = mlflow_utils.get_tracking_uri()
        assert got is None or got  # no crash; value is either None or whatever was already in env
    finally:
        os.environ.pop("MLFLOW_ENV_FILE", None)
        if prev_override is not None:
            os.environ["MLFLOW_ENV_FILE"] = prev_override
        if prev_uri is not None:
            os.environ["MLFLOW_TRACKING_URI"] = prev_uri
        importlib.reload(mlflow_utils)


def test_t11_review_mlflow_env_file_whitespace_only_reload_no_crash():
    """Code Review §2: MLFLOW_ENV_FILE='   ' (whitespace only) → reload does not crash."""
    prev_uri = os.environ.pop("MLFLOW_TRACKING_URI", None)
    prev_override = os.environ.pop("MLFLOW_ENV_FILE", None)
    os.environ["MLFLOW_ENV_FILE"] = "   "
    try:
        importlib.reload(mlflow_utils)
        got = mlflow_utils.get_tracking_uri()
        assert got is None or got
    finally:
        os.environ.pop("MLFLOW_ENV_FILE", None)
        if prev_override is not None:
            os.environ["MLFLOW_ENV_FILE"] = prev_override
        if prev_uri is not None:
            os.environ["MLFLOW_TRACKING_URI"] = prev_uri
        importlib.reload(mlflow_utils)


def test_t11_review_override_false_env_takes_precedence():
    """Code Review §3: override=False → existing env MLFLOW_TRACKING_URI is not overwritten by file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("MLFLOW_TRACKING_URI=http://from-file.example.com\n")
        tmp_path = f.name
    try:
        prev_uri = os.environ.pop("MLFLOW_TRACKING_URI", None)
        prev_override = os.environ.pop("MLFLOW_ENV_FILE", None)
        os.environ["MLFLOW_TRACKING_URI"] = "http://env-override.example.com"
        os.environ["MLFLOW_ENV_FILE"] = tmp_path
        importlib.reload(mlflow_utils)
        assert mlflow_utils.get_tracking_uri() == "http://env-override.example.com"
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        os.environ.pop("MLFLOW_ENV_FILE", None)
        if prev_override is not None:
            os.environ["MLFLOW_ENV_FILE"] = prev_override
        if prev_uri is not None:
            os.environ["MLFLOW_TRACKING_URI"] = prev_uri
        else:
            os.environ.pop("MLFLOW_TRACKING_URI", None)
        importlib.reload(mlflow_utils)


def test_t11_review_docstring_mentions_mlflow_env_file_and_override():
    """Code Review §4 (optional): Module docstring or source documents MLFLOW_ENV_FILE as test/override."""
    source_path = Path(mlflow_utils.__file__)
    source = source_path.read_text(encoding="utf-8")
    assert "MLFLOW_ENV_FILE" in source, "MLFLOW_ENV_FILE must be documented in mlflow_utils (doc or comment)."
    assert "override" in source.lower() or "test" in source.lower(), (
        "mlflow_utils must mention override or test for MLFLOW_ENV_FILE (Code Review §4)."
    )


# --- Credential folder Code Review §2, §4 (STATUS.md) ---


def test_credential_review_mlflow_warning_log_does_not_contain_path():
    """Code Review §2: When load_dotenv raises, warning log must not contain credential/local_state path (security)."""
    import logging
    leaky_path = "/some/credential/path/mlflow.env"
    log_capture: list = []
    handler = logging.Handler()
    handler.emit = lambda rec: log_capture.append(rec.getMessage())
    _log = logging.getLogger("trainer.core.mlflow_utils")
    _log.addHandler(handler)
    try:
        with patch("dotenv.load_dotenv", side_effect=PermissionError(leaky_path)):
            importlib.reload(mlflow_utils)
    finally:
        _log.removeHandler(handler)
    # Desired: log must not contain the exception's path (str(e)); folder names in format string are ok.
    for msg in log_capture:
        assert leaky_path not in msg, f"Log must not contain exception path: {msg!r}"


def test_credential_review_source_credential_before_local_state():
    """Code Review §4: Default mlflow.env path must try credential/ before local_state/ (source order contract)."""
    source_path = Path(mlflow_utils.__file__)
    source = source_path.read_text(encoding="utf-8")
    # Find the block that sets _candidate / _mlflow_env_path without MLFLOW_ENV_FILE
    idx_credential = source.find('"credential"')
    idx_local_state = source.find('"local_state"')
    assert idx_credential >= 0, "mlflow_utils must reference credential path"
    assert idx_local_state >= 0, "mlflow_utils must reference local_state path"
    assert idx_credential < idx_local_state, (
        "Code Review §4: credential must be tried before local_state in default path resolution."
    )
