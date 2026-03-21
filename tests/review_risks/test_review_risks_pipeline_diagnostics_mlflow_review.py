"""
Pipeline diagnostics §3–§4 MLflow — Code Review 風險點 → 最小可重現／契約測試。

對應 STATUS.md「Code Review：`pipeline_diagnostics` §3–§4（2026-03-21）」。
僅新增 tests，不修改 production code。
"""

from __future__ import annotations

import inspect
import pathlib
import re
import unittest
from unittest.mock import patch

import pytest

from trainer.core import mlflow_utils
from trainer.training import trainer as trainer_mod

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MLFLOW_ENV_EXAMPLE = _REPO_ROOT / "credential" / "mlflow.env.example"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _run_pipeline_src() -> str:
    return inspect.getsource(trainer_mod.run_pipeline)


def _log_artifact_safe_src() -> str:
    return inspect.getsource(mlflow_utils.log_artifact_safe)


def _bundle_artifact_section(src: str) -> str:
    """Slice run_pipeline from bundle-artifact comment through first log_metrics_safe(mlflow_metrics)."""
    m = re.search(
        r"# Phase 2 / pipeline plan: small-file artifacts.*?"
        r"log_metrics_safe\(mlflow_metrics\)",
        src,
        re.DOTALL,
    )
    assert m is not None, "run_pipeline must contain bundle block + log_metrics_safe(mlflow_metrics)"
    return m.group(0)


# ---------------------------------------------------------------------------
# Review #1: log_artifact_safe 無 transient 重試（現況鎖定）
# ---------------------------------------------------------------------------


def test_review1_log_artifact_safe_does_not_retry_on_503_like_transient_error():
    """Review #1: log_artifact_safe calls mlflow.log_artifact once; no retry loop (unlike log_metrics_safe)."""
    pytest.importorskip("mlflow")
    with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
        with patch("mlflow.log_artifact", side_effect=Exception("503 Service Unavailable")) as mock_la:
            mlflow_utils.log_artifact_safe("/tmp/fake.json", artifact_path="bundle")
    assert mock_la.call_count == 1, "Current contract: single attempt; if retry is added, update this test"


def test_review1_log_artifact_safe_source_has_no_retry_loop():
    """Review #1: log_artifact_safe body must not use the same retry pattern as log_metrics_safe."""
    src = _log_artifact_safe_src()
    assert "_MLFLOW_RETRY_MAX_RETRIES" not in src
    assert "for attempt in range" not in src


# ---------------------------------------------------------------------------
# Review #2: Artifact 上傳在 log_metrics_safe 之前（契約）
# ---------------------------------------------------------------------------


class TestReview2ArtifactOrderInRunPipeline(unittest.TestCase):
    """Review #2: In success path, bundle log_artifact_safe runs before log_metrics_safe(mlflow_metrics)."""

    def test_bundle_block_before_log_metrics_safe(self):
        chunk = _bundle_artifact_section(_run_pipeline_src())
        i_art = chunk.find("log_artifact_safe")
        i_met = chunk.find("log_metrics_safe(mlflow_metrics)")
        self.assertGreater(i_art, -1, "bundle block should call log_artifact_safe")
        self.assertGreater(i_met, -1, "T12.2 block should call log_metrics_safe(mlflow_metrics)")
        self.assertLess(
            i_art,
            i_met,
            "Artifacts should be logged before success-path log_metrics_safe (UI may show files first)",
        )


# ---------------------------------------------------------------------------
# Review #3: training_metrics 上傳無 bytes 門檻（現況鎖定）
# ---------------------------------------------------------------------------


class TestReview3NoSizeThresholdOnBundleArtifacts(unittest.TestCase):
    """Review #3: bundle loop does not skip by file size (risk if training_metrics.json grows)."""

    def test_bundle_for_loop_has_no_st_size_guard(self):
        src = _run_pipeline_src()
        # Narrow to the has_active_run bundle for-loop
        m = re.search(
            r"if has_active_run\(\):.*?for _fname in \(\s*\"training_metrics\.json\".*?log_artifact_safe\(_ap",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "expected if has_active_run + for _fname + log_artifact_safe block")
        block = m.group(0)
        self.assertNotIn("st_size", block, "No size-based skip in bundle loop (if added, document + test threshold)")
        self.assertNotIn("stat()", block, "No stat() size guard in bundle loop")


# ---------------------------------------------------------------------------
# Review #4: mlflow.env.example + pyproject optional-deps 文件契約
# ---------------------------------------------------------------------------


class TestReview4MlflowEnvExampleAndPyproject(unittest.TestCase):
    """Review #4: System metrics env + psutil documented; optional extra present."""

    def test_mlflow_env_example_mentions_system_metrics_and_psutil(self):
        text = _MLFLOW_ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING", text)
        self.assertIn("psutil", text)

    def test_pyproject_has_optional_mlflow_system_metrics(self):
        text = _PYPROJECT.read_text(encoding="utf-8")
        self.assertIn("[project.optional-dependencies]", text)
        self.assertIn("mlflow-system-metrics", text)
        self.assertIn("psutil", text)


# ---------------------------------------------------------------------------
# Review #6: run_pipeline 以 has_active_run 包住 bundle；log_artifact_safe 不檢查 active run
# ---------------------------------------------------------------------------


class TestReview6HasActiveRunGuardAndLogArtifactSafe(unittest.TestCase):
    """Review #6: trainer guards with has_active_run(); log_artifact_safe only checks is_mlflow_available."""

    def test_run_pipeline_bundle_inside_has_active_run(self):
        src = _run_pipeline_src()
        # First has_active_run after pipeline diagnostics write should precede log_artifact_safe in bundle block
        idx_diag = src.find("_write_pipeline_diagnostics_json")
        self.assertGreater(idx_diag, 0)
        tail = src[idx_diag:]
        idx_if = tail.find("if has_active_run():")
        idx_la = tail.find("log_artifact_safe(_ap")
        self.assertGreater(idx_if, -1)
        self.assertGreater(idx_la, -1)
        self.assertLess(idx_if, idx_la, "bundle uploads must be under if has_active_run()")

    def test_log_artifact_safe_swallows_exception_without_active_run(self):
        """log_artifact_safe does not require has_active_run(); mlflow error is caught (callers may guard)."""
        pytest.importorskip("mlflow")
        with patch("trainer.core.mlflow_utils.is_mlflow_available", return_value=True):
            with patch("mlflow.log_artifact", side_effect=RuntimeError("no active run")):
                mlflow_utils.log_artifact_safe("/tmp/x.json", artifact_path="bundle")
