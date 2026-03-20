"""
T13 MLflow cold-start mitigation — Code Review 風險點轉成最小可重現測試。

對應 STATUS.md「Code Review：T13 MLflow cold-start mitigation 變更（2026-03-19）」。
- 僅新增 tests，不修改 production code。
- 預期行為尚未實作的項目使用 @unittest.expectedFailure，待 production 修復後移除。
"""

import unittest
from unittest.mock import MagicMock, patch

import pytest

from trainer.core import mlflow_utils


def _is_transient_mlflow_error():
    """Access private helper for guardrail test (Review #2)."""
    return getattr(mlflow_utils, "_is_transient_mlflow_error", None)


# ---------------------------------------------------------------------------
# Review #1: [安全性] 失敗時 warning 不得包含 tracking URI / 主機名
# ---------------------------------------------------------------------------


def test_t13_review1_log_params_failure_warning_must_not_contain_uri_or_hostname():
    """T13 Review #1: When log_params_safe exhausts retries, warning must NOT contain https:// or run.app (Credential §2)."""
    pytest.importorskip("mlflow")
    fake_url_msg = "API request to https://mlflow-server-72672742800.us-central1.run.app/api/2.0/mlflow/runs/log-batch failed"
    # Exhaust retries: 4 attempts (max_retries=3 + 1)
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("trainer.core.mlflow_utils.time.sleep"):
            with patch("mlflow.log_params", side_effect=[Exception(fake_url_msg)] * 4):
                with patch("trainer.core.mlflow_utils._log.warning") as mock_warn:
                    mlflow_utils.log_params_safe({"k": "v"})
    assert mock_warn.call_count >= 1
    # Build the message that would be logged (same as logger.warning(fmt, *args))
    call_args = mock_warn.call_args
    fmt = call_args[0][0]
    args = call_args[0][1:] if len(call_args[0]) > 1 else ()
    msg = (fmt % args) if args else fmt
    assert "https://" not in msg, "Warning must not leak tracking URI (Credential §2)"
    assert "run.app" not in msg, "Warning must not leak hostname (Credential §2)"


# ---------------------------------------------------------------------------
# Review #2: [邊界條件] _is_transient_mlflow_error 對 "Error 50342" 的現狀鎖定
# ---------------------------------------------------------------------------


def test_t13_review2_is_transient_mlflow_error_error_50342_locks_current_behavior():
    """T13 Review #2: Lock current behavior for Exception('Error 50342: invalid state'); docstring says may change to False if option A."""
    fn = _is_transient_mlflow_error()
    if fn is None:
        pytest.skip("_is_transient_mlflow_error not found (private)")
    exc = Exception("Error 50342: invalid state")
    result = fn(exc)
    # Current implementation: "503" in msg → True. If production adopts option A, change to assert result is False.
    assert result is True, "Current behavior: 50342 contains '503' so treated as transient; change to False if option A adopted"


# ---------------------------------------------------------------------------
# Review #3: [邊界條件] log_params_safe / log_tags_safe 空 dict 不應呼叫 MLflow
# ---------------------------------------------------------------------------


def test_t13_review3_log_params_safe_empty_dict_should_not_call_mlflow():
    """T13 Review #3: log_params_safe({}) must not call mlflow.log_params (avoid unnecessary API / retry)."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.log_params") as mock_log_params:
            mlflow_utils.log_params_safe({})
    assert mock_log_params.call_count == 0


def test_t13_review3_log_tags_safe_empty_dict_should_not_call_mlflow():
    """T13 Review #3: log_tags_safe({}) must not call mlflow.set_tags (avoid unnecessary API / retry)."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.set_tags") as mock_set_tags:
            mlflow_utils.log_tags_safe({})
    assert mock_set_tags.call_count == 0


# ---------------------------------------------------------------------------
# Review #4: [邊界條件] warm_up_mlflow_run_safe 當 run.info.run_id 缺失時不應呼叫 get_run
# ---------------------------------------------------------------------------


def test_t13_review4_warm_up_mlflow_run_safe_no_run_id_should_not_call_get_run():
    """T13 Review #4: When active_run().info.run_id is None, warm_up_mlflow_run_safe must not call mlflow.get_run."""
    mlflow = pytest.importorskip("mlflow")
    run_without_id = MagicMock()
    run_without_id.info = MagicMock()
    run_without_id.info.run_id = None
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch.object(mlflow, "active_run", return_value=run_without_id):
            with patch.object(mlflow, "get_run") as mock_get_run:
                mlflow_utils.warm_up_mlflow_run_safe()
    assert mock_get_run.call_count == 0


def test_t13_review4_warm_up_mlflow_run_safe_no_run_id_logs_at_least_one_warning():
    """T13 Review #4: When run_id is None, warm_up must not crash and should log at least one warning (observability)."""
    mlflow = pytest.importorskip("mlflow")
    run_without_id = MagicMock()
    run_without_id.info = MagicMock()
    run_without_id.info.run_id = None
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch.object(mlflow, "active_run", return_value=run_without_id):
            with patch.object(mlflow, "get_run", side_effect=TypeError("run_id required")):
                with patch("trainer.core.mlflow_utils._log.warning") as mock_warn:
                    mlflow_utils.warm_up_mlflow_run_safe()
    assert mock_warn.call_count >= 1


# ---------------------------------------------------------------------------
# Review #5: [可觀測性] 重試時應有含 "retry in" 或 attempt 的 INFO
# ---------------------------------------------------------------------------


def test_t13_review5_retry_logs_info_with_retry_in_or_attempt():
    """T13 Review #5: When a transient error triggers retry, log should contain 'retry in' or 'attempt' (observability)."""
    mlflow = pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("trainer.core.mlflow_utils.time.sleep"):
            with patch.object(mlflow, "log_params", side_effect=[Exception("503 Service Unavailable"), None]):
                with patch("trainer.core.mlflow_utils._log.info") as mock_info:
                    mlflow_utils.log_params_safe({"k": "v"})
    assert mock_info.call_count >= 1
    all_info_msgs = " ".join(str(call) for call in mock_info.call_args_list)
    assert "retry" in all_info_msgs.lower() or "attempt" in all_info_msgs.lower()
