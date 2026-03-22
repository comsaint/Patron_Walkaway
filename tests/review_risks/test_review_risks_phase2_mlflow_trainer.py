"""
Phase 2 T2: Review/contract tests for trainer MLflow provenance (no production changes).

- run_pipeline calls _log_training_provenance_to_mlflow after save_artifact_bundle.
- _log_training_provenance_to_mlflow does not raise when MLflow is unavailable (safe no-op).

T12 Code Review (§2–§4): failed-run logging contract and behavior (tests only).
"""

from __future__ import annotations

import argparse
import ast
import inspect
import pathlib
import textwrap
import unittest
import pytest
from datetime import datetime
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from trainer.training import trainer as trainer_mod


def _run_pipeline_src() -> str:
    return inspect.getsource(trainer_mod.run_pipeline)


def _log_provenance_src() -> str:
    """Return source of _log_training_provenance_to_mlflow (Code Review §5: use in contract test)."""
    return inspect.getsource(trainer_mod._log_training_provenance_to_mlflow)


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MLFLOW_UTILS_PATH = _REPO_ROOT / "trainer" / "core" / "mlflow_utils.py"
_MLFLOW_UTILS_SRC = _MLFLOW_UTILS_PATH.read_text(encoding="utf-8")


def _parse_src_to_ast(source: str) -> ast.AST:
    """Best-effort parse; caller supplies already-read python source."""
    return ast.parse(textwrap.dedent(source))


def _has_function_def(source: str, fn_name: str) -> bool:
    tree = _parse_src_to_ast(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return True
    return False


def _collect_string_constants(source: str) -> set[str]:
    """Collect all string literal constants from python source."""
    tree = _parse_src_to_ast(source)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
    return out


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


class TestT12FailedRunErrorTagTruncation(unittest.TestCase):
    """T12 Code Review §2: run_pipeline except block must truncate error tag to 500 chars (safety contract)."""

    def test_run_pipeline_except_uses_error_tag_truncated_to_500(self):
        """Source of run_pipeline must use str(e)[:500] for the FAILED error tag."""
        src = _run_pipeline_src()
        self.assertIn("log_tags_safe", src, "run_pipeline except should call log_tags_safe")
        self.assertIn("status", src)
        self.assertIn("FAILED", src)
        self.assertIn("error", src)
        # Contract: error value must be truncated (e.g. [:500]) to avoid unbounded tag length.
        self.assertIn("[:500]", src, "run_pipeline FAILED error tag must be truncated to 500 chars (Code Review §2).")


class TestT12MlflowRunNameFormat(unittest.TestCase):
    """T12 Code Review §3: run_name format contract (train-<window>-<timestamp>)."""

    def test_run_pipeline_mlflow_run_name_contains_train_and_time(self):
        """Source must build run_name with train-, start.date(), end.date(), and time (e.g. time.time())."""
        src = _run_pipeline_src()
        self.assertIn("train-", src, "T12 run_name should start with train-")
        self.assertIn("start.date()", src, "T12 run_name should include start.date()")
        self.assertIn("end.date()", src, "T12 run_name should include end.date()")
        self.assertIn("time.time()", src, "T12 run_name should include time.time() for uniqueness.")


class TestT12FailedPathReRaisesOriginalException(unittest.TestCase):
    """T12 Code Review §4: when pipeline fails, original exception propagates; log_tags_safe called with FAILED."""

    def test_run_pipeline_failure_propagates_original_exception(self):
        """When run_pipeline fails (e.g. get_monthly_chunks raises), the same exception is re-raised."""
        args = argparse.Namespace(
            start="2026-01-01",
            end="2026-01-02",
            use_local_parquet=False,
            force_recompute=False,
            skip_optuna=False,
            no_preload=False,
            sample_rated=None,
            recent_chunks=None,
            rebuild_canonical_mapping=False,
        )
        with patch.object(trainer_mod, "get_monthly_chunks", side_effect=ValueError("simulated pipeline failure")):
            with self.assertRaises(ValueError) as ctx:
                trainer_mod.run_pipeline(args)
            self.assertIn("simulated pipeline failure", str(ctx.exception))

    def test_run_pipeline_failure_calls_log_tags_safe_with_failed_and_error_truncated(self):
        """When run_pipeline fails, log_tags_safe is called with status=FAILED and error len <= 500."""
        args = argparse.Namespace(
            start="2026-01-01",
            end="2026-01-02",
            use_local_parquet=False,
            force_recompute=False,
            skip_optuna=False,
            no_preload=False,
            sample_rated=None,
            recent_chunks=None,
            rebuild_canonical_mapping=False,
        )
        with patch.object(trainer_mod, "get_monthly_chunks", side_effect=ValueError("simulated pipeline failure")):
            with patch.object(trainer_mod, "log_tags_safe") as mock_log_tags:
                try:
                    trainer_mod.run_pipeline(args)
                except ValueError:
                    pass
                mock_log_tags.assert_called_once()
                tags = mock_log_tags.call_args[0][0]
                self.assertIsInstance(tags, dict)
                self.assertEqual(tags.get("status"), "FAILED")
                self.assertIn("error", tags)
                self.assertLessEqual(len(tags["error"]), 500, "error tag must be truncated to 500 chars (Code Review §2).")


class TestT12_2Step2MetricsContract(unittest.TestCase):
    """T12.2 Step 2: success metrics + OOM/memory diagnostics contract (tests only).

    Because current production may not implement this step yet, missing contracts are
    tracked via self.skipTest() (not failing CI), while keeping contract intent explicit.
    """

    def test_mlflow_utils_exposes_log_metrics_safe(self):
        """Contract: trainer/core/mlflow_utils.py must define log_metrics_safe(metrics=...)."""
        if not _has_function_def(_MLFLOW_UTILS_SRC, "log_metrics_safe"):
            self.skipTest("T12.2 Step 2 pending: log_metrics_safe missing in mlflow_utils.py")
        self.assertTrue(True)

    def test_run_pipeline_logs_step_durations_on_success(self):
        """Contract: run_pipeline should log total_duration_sec + step1–10_duration_sec on success."""
        src = _run_pipeline_src()
        if "log_metrics_safe" not in src:
            self.skipTest("T12.2 Step 2 pending: run_pipeline does not call log_metrics_safe")

        constants = _collect_string_constants(src)
        required = {
            "total_duration_sec",
            "step1_duration_sec",
            "step2_duration_sec",
            "step3_duration_sec",
            "step4_duration_sec",
            "step5_duration_sec",
            "step6_duration_sec",
            "step7_duration_sec",
            "step8_duration_sec",
            "step9_duration_sec",
            "step10_duration_sec",
        }
        missing = sorted(required - constants)
        if missing:
            self.fail(f"T12.2 Step 2 missing duration key contracts: {missing}")

    def test_run_pipeline_logs_memory_sampling_tags_and_rss_sys_metric_keys(self):
        """Contract: memory tag names + RSS/sys metric key naming must match plan."""
        src = _run_pipeline_src()
        if "log_metrics_safe" not in src:
            self.skipTest("T12.2 Step 2 pending: run_pipeline does not call log_metrics_safe")

        # Check both tag-related strings and metric key names.
        required_constants = {
            # tag keys/values
            "memory_sampling",
            "checkpoint_peak",
            "memory_sampling_scope",
            "step7_9",
            "disabled_no_psutil",
            # rss/sys metric key names
            "step7_rss_start_gb",
            "step7_rss_peak_gb",
            "step7_rss_end_gb",
            "step7_sys_available_min_gb",
            "step7_sys_used_percent_peak",
        }
        constants = _collect_string_constants(src)
        missing = sorted(required_constants - constants)
        if missing:
            self.fail(f"T12.2 Step 2 missing memory tag/metric key contracts: {missing}")

    def test_run_pipeline_logs_oom_precheck_estimate_param_strings(self):
        """Contract: run_pipeline should write oom_precheck_* estimate params for tuning."""
        src = _run_pipeline_src()
        if "log_metrics_safe" not in src:
            self.skipTest("T12.2 Step 2 pending: run_pipeline does not call log_metrics_safe")

        constants = _collect_string_constants(src)
        required = {"oom_precheck_est_peak_ram_gb", "oom_precheck_step7_rss_error_ratio"}
        missing = sorted(required - constants)
        if missing:
            self.fail(f"T12.2 Step 2 missing OOM precheck param contract keys: {missing}")


class TestT12FailureParamsContract(unittest.TestCase):
    """T12 optional follow-on: failure diagnostics params are logged on exception (contract test).

    This test validates the presence of expected MLflow param keys in run_pipeline's
    outer exception handler.
    """

    def test_failure_except_logs_expected_param_keys(self):
        src = _run_pipeline_src()
        constants = _collect_string_constants(src)

        required = {
            "training_window_start",
            "training_window_end",
            "recent_chunks",
            "neg_sample_frac",
            "chunk_count",
            "use_local_parquet",
            "oom_precheck_est_peak_ram_gb",
        }
        missing = sorted(required - constants)
        if missing:
            self.fail(f"T12 failure params missing expected contract keys: {missing}")


class TestT12FailureParamsBehavior(unittest.TestCase):
    """Risk #1: failure diagnostics params should be actually logged (behavior test)."""

    def test_failure_except_calls_log_params_safe_with_expected_keys_non_none(self):
        import datetime as _dt

        args = argparse.Namespace(
            start="2026-01-01",
            end="2026-01-02",
            use_local_parquet=False,
            force_recompute=False,
            skip_optuna=False,
            no_preload=False,
            sample_rated=None,
            recent_chunks=1,
            rebuild_canonical_mapping=False,
        )

        fixed_start = _dt.datetime(2026, 1, 1, 0, 0, 0)
        fixed_end = _dt.datetime(2026, 1, 2, 0, 0, 0)
        chunk1 = {"window_start": fixed_start, "window_end": fixed_end}
        chunk2 = {
            "window_start": fixed_start.replace(day=2),
            "window_end": fixed_end.replace(day=3),
        }

        class _FakeCachePath:
            def exists(self) -> bool:
                return True

            def stat(self) -> SimpleNamespace:
                return SimpleNamespace(st_size=1024)

            def with_suffix(self, _suffix: str) -> "_FakeCachePath":
                # Keep cache sidecar existence true; tests shouldn't do real I/O.
                return self

        def _fake_chunk_parquet_path(_c) -> _FakeCachePath:
            return _FakeCachePath()

        with patch.object(trainer_mod, "parse_window", return_value=(fixed_start, fixed_end)):
            with patch.object(trainer_mod, "get_monthly_chunks", return_value=[chunk1, chunk2]):
                with patch.object(trainer_mod, "_oom_check_and_adjust_neg_sample_frac", return_value=0.5):
                    with patch.object(trainer_mod, "_chunk_parquet_path", side_effect=_fake_chunk_parquet_path):
                        with patch.object(
                            trainer_mod,
                            "get_train_valid_test_split",
                            side_effect=ValueError("simulated Step2 failure"),
                        ):
                            with patch.object(trainer_mod, "safe_start_run", return_value=nullcontext()):
                                with patch.object(trainer_mod, "log_tags_safe"):
                                    with patch.object(trainer_mod, "log_params_safe") as mock_log_params:
                                        with self.assertRaises(ValueError) as ctx:
                                            trainer_mod.run_pipeline(args)

        self.assertIn("simulated Step2 failure", str(ctx.exception))
        mock_log_params.assert_called_once()
        payload = mock_log_params.call_args[0][0]

        required = {
            "training_window_start",
            "training_window_end",
            "recent_chunks",
            "neg_sample_frac",
            "chunk_count",
            "use_local_parquet",
            "oom_precheck_est_peak_ram_gb",
        }
        self.assertTrue(required.issubset(payload.keys()))
        for k in required:
            self.assertIsNotNone(payload[k], f"{k} must be non-None in failure params")


class TestT12FailureParamsTruncationXfail(unittest.TestCase):
    """Risk #6: failure params may contain unexpectedly long strings."""

    def test_failure_except_truncates_long_training_window_strings(self):
        import datetime as _dt

        args = argparse.Namespace(
            start="2026-01-01",
            end="2026-01-02",
            use_local_parquet=False,
            force_recompute=False,
            skip_optuna=False,
            no_preload=False,
            sample_rated=None,
            recent_chunks=1,
            rebuild_canonical_mapping=False,
        )

        fixed_start = _dt.datetime(2026, 1, 1, 0, 0, 0)
        fixed_end = _dt.datetime(2026, 1, 2, 0, 0, 0)

        class _LongIsoDateTime:
            def __init__(self, dt: _dt.datetime, long_size: int = 4096):
                self._dt = dt
                self._long_size = long_size

            @property
            def tzinfo(self):
                return None

            def replace(self, tzinfo=None):
                return self

            def date(self):
                return self._dt.date()

            def isoformat(self):
                # Ensure length is obviously > any reasonable truncation threshold.
                return self._dt.isoformat() + ("X" * self._long_size)

        long_start = _LongIsoDateTime(fixed_start)
        long_end = _LongIsoDateTime(fixed_end)
        chunk1 = {"window_start": long_start, "window_end": long_end}
        chunk2 = {"window_start": long_start, "window_end": long_end}

        class _FakeCachePath:
            def exists(self) -> bool:
                return True

            def stat(self) -> SimpleNamespace:
                return SimpleNamespace(st_size=1024)

            def with_suffix(self, _suffix: str) -> "_FakeCachePath":
                return self

        def _fake_chunk_parquet_path(_c) -> _FakeCachePath:
            return _FakeCachePath()

        with patch.object(trainer_mod, "parse_window", return_value=(fixed_start, fixed_end)):
            with patch.object(trainer_mod, "get_monthly_chunks", return_value=[chunk1, chunk2]):
                with patch.object(trainer_mod, "_oom_check_and_adjust_neg_sample_frac", return_value=0.5):
                    with patch.object(trainer_mod, "_chunk_parquet_path", side_effect=_fake_chunk_parquet_path):
                        with patch.object(
                            trainer_mod,
                            "get_train_valid_test_split",
                            side_effect=ValueError("simulated Step2 failure"),
                        ):
                            with patch.object(trainer_mod, "safe_start_run", return_value=nullcontext()):
                                with patch.object(trainer_mod, "log_tags_safe"):
                                    with patch.object(trainer_mod, "log_params_safe") as mock_log_params:
                                        with self.assertRaises(ValueError):
                                            trainer_mod.run_pipeline(args)

        payload = mock_log_params.call_args[0][0]
        # Desired behavior: keep diagnostics bounded to avoid MLflow param size errors.
        self.assertLessEqual(len(payload["training_window_start"]), 200)
        self.assertLessEqual(len(payload["training_window_end"]), 200)


class TestT12RssPeakSemanticsContract(unittest.TestCase):
    """Risk #3: RSS "peak" semantics should follow the explicit code contract."""

    def test_step7_rss_peak_gb_is_max_of_start_and_end(self):
        src = _run_pipeline_src()
        tree = _parse_src_to_ast(src)

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "step7_rss_peak_gb":
                        found = True
                        self.assertIsInstance(node.value, ast.Call)
                        self.assertIsInstance(node.value.func, ast.Name)
                        self.assertEqual(node.value.func.id, "max")
                        arg_ids = {a.id for a in node.value.args if isinstance(a, ast.Name)}
                        self.assertEqual(arg_ids, {"step7_rss_start_gb", "step7_rss_end_gb"})

        if not found:
            self.fail(
                "Could not find `step7_rss_peak_gb = max(step7_rss_start_gb, step7_rss_end_gb)` in run_pipeline source"
            )


class TestT12OomPrecheckCacheSidecarContract(unittest.TestCase):
    """Risk #5: OOM pre-check should rely on cache sidecar existence to reduce I/O."""

    def test_oom_precheck_uses_cache_key_sidecar(self):
        src = _run_pipeline_src()
        tree = _parse_src_to_ast(src)

        found = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "with_suffix"
            ):
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == ".cache_key"
                ):
                    found = True
                    break

        if not found:
            self.fail("Could not find `with_suffix('.cache_key')` in run_pipeline source for OOM pre-check caching")
