"""Plan §6: pipeline_diagnostics JSON shape + copy_model_bundle missing-file warning."""

from __future__ import annotations

import json
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from package.build_deploy_package import copy_model_bundle
from trainer.training import trainer as trainer_mod


class TestCopyModelBundlePipelineDiagnosticsWarning(unittest.TestCase):
    """doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md §2 / §6: warn when optional file missing."""

    def test_warns_when_pipeline_diagnostics_json_missing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            source = tdp / "src"
            source.mkdir()
            dest_models = tdp / "pkg" / "models"
            (source / "model.pkl").write_bytes(b"0")
            (source / "feature_list.json").write_text("[]", encoding="utf-8")

            with self.assertLogs("package.build_deploy_package", level=logging.WARNING) as cm:
                copy_model_bundle(source, dest_models)

            pd_warnings = [
                r
                for r in cm.records
                if r.levelno >= logging.WARNING and "pipeline_diagnostics.json" in r.getMessage()
            ]
            self.assertEqual(
                len(pd_warnings),
                1,
                "STATUS Code Review #8 MRE: exactly one WARNING mentioning pipeline_diagnostics.json "
                "(other optional-file warnings must not break this contract)",
            )
            messages = pd_warnings[0].getMessage()
            low = messages.lower()
            self.assertTrue("missing" in low or "omit" in low, messages)


class TestWritePipelineDiagnosticsJsonShape(unittest.TestCase):
    """doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md §6: JSON keys and None omission."""

    def test_writes_expected_keys_and_omits_none(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod._write_pipeline_diagnostics_json(
                    model_version="test-mv-1",
                    pipeline_started_at="2026-03-21T00:00:00+00:00",
                    pipeline_finished_at="2026-03-21T01:00:00+00:00",
                    total_duration_sec=3600.0,
                    step7_duration_sec=120.5,
                    step8_duration_sec=None,
                    oom_precheck_est_peak_ram_gb=48.0,
                    step7_rss_peak_gb=44.0,
                )
            out_path = model_dir / "pipeline_diagnostics.json"
            self.assertTrue(out_path.is_file())
            data = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(data["model_version"], "test-mv-1")
        self.assertEqual(data["pipeline_started_at"], "2026-03-21T00:00:00+00:00")
        self.assertEqual(data["pipeline_finished_at"], "2026-03-21T01:00:00+00:00")
        self.assertEqual(data["total_duration_sec"], 3600.0)
        self.assertEqual(data["step7_duration_sec"], 120.5)
        self.assertIn("oom_precheck_est_peak_ram_gb", data)
        self.assertIn("step7_rss_peak_gb", data)
        self.assertNotIn("step8_duration_sec", data)
        self.assertNotIn("oom_precheck_step7_rss_error_ratio", data)

    def test_section6_writes_all_rss_and_oom_ratio_keys_when_provided(self):
        """§6：合法 JSON 同時含 OOM 預檢、RSS 起迄峰、比值（與 run_pipeline 可傳入欄位對齊）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod._write_pipeline_diagnostics_json(
                    model_version="mv",
                    pipeline_started_at="2026-03-21T10:00:00+00:00",
                    pipeline_finished_at="2026-03-21T10:30:00+00:00",
                    total_duration_sec=1800.0,
                    step7_duration_sec=300.0,
                    oom_precheck_est_peak_ram_gb=50.0,
                    oom_precheck_step7_rss_error_ratio=0.88,
                    step7_rss_start_gb=10.0,
                    step7_rss_peak_gb=44.0,
                    step7_rss_end_gb=12.0,
                )
            data = json.loads((model_dir / "pipeline_diagnostics.json").read_text(encoding="utf-8"))

        self.assertEqual(data["oom_precheck_step7_rss_error_ratio"], 0.88)
        self.assertEqual(data["step7_rss_start_gb"], 10.0)
        self.assertEqual(data["step7_rss_peak_gb"], 44.0)
        self.assertEqual(data["step7_rss_end_gb"], 12.0)
        self.assertAlmostEqual(
            data["oom_precheck_step7_rss_error_ratio"],
            data["step7_rss_peak_gb"] / data["oom_precheck_est_peak_ram_gb"],
            places=12,
            msg="STATUS Code Review #5 MRE: JSON must be internally consistent when caller passes matching numbers",
        )

    def test_writer_preserves_caller_supplied_oom_ratio_even_if_inconsistent_with_peak(
        self,
    ):
        """STATUS Code Review #5 MRE: writer does not recompute ratio from peak/precheck (run_pipeline owns that)."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod._write_pipeline_diagnostics_json(
                    model_version="mv",
                    pipeline_started_at="2026-03-21T10:00:00+00:00",
                    pipeline_finished_at="2026-03-21T10:30:00+00:00",
                    total_duration_sec=1.0,
                    oom_precheck_est_peak_ram_gb=50.0,
                    oom_precheck_step7_rss_error_ratio=0.99,
                    step7_rss_peak_gb=10.0,
                )
            data = json.loads((model_dir / "pipeline_diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(data["oom_precheck_step7_rss_error_ratio"], 0.99)
        self.assertNotAlmostEqual(
            data["oom_precheck_step7_rss_error_ratio"],
            data["step7_rss_peak_gb"] / data["oom_precheck_est_peak_ram_gb"],
        )

    def test_step7_duration_sec_zero_is_written_not_omitted(self):
        """STATUS Code Review §3: 0.0 is not None — key must appear."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod._write_pipeline_diagnostics_json(
                    model_version="z",
                    pipeline_started_at="2026-01-01T00:00:00+00:00",
                    pipeline_finished_at="2026-01-01T00:01:00+00:00",
                    total_duration_sec=1.0,
                    step7_duration_sec=0.0,
                )
            data = json.loads((model_dir / "pipeline_diagnostics.json").read_text(encoding="utf-8"))
        self.assertIn("step7_duration_sec", data)
        self.assertEqual(data["step7_duration_sec"], 0.0)

    def test_step1_and_step10_durations_written_when_provided(self):
        """T-PipelineStepDurations: optional step1/step10 keys appear in JSON like step7."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod._write_pipeline_diagnostics_json(
                    model_version="mv",
                    pipeline_started_at="2026-03-22T00:00:00+00:00",
                    pipeline_finished_at="2026-03-22T01:00:00+00:00",
                    total_duration_sec=100.0,
                    step1_duration_sec=1.5,
                    step10_duration_sec=2.5,
                )
            data = json.loads((model_dir / "pipeline_diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(data["step1_duration_sec"], 1.5)
        self.assertEqual(data["step10_duration_sec"], 2.5)
        self.assertNotIn("step2_duration_sec", data)

    def test_non_serializable_value_becomes_string_via_default_str_documents_risk(self):
        """STATUS Code Review §2 MRE: default=str coerces unknown types — file stays valid JSON."""

        class _Weird:
            def __str__(self) -> str:
                return "WEIRD_MARKER"

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod._write_pipeline_diagnostics_json(
                    model_version="z",
                    pipeline_started_at="a",
                    pipeline_finished_at="b",
                    total_duration_sec=_Weird(),  # type: ignore[arg-type]
                )
            raw = (model_dir / "pipeline_diagnostics.json").read_text(encoding="utf-8")
            data = json.loads(raw)
        self.assertEqual(data["total_duration_sec"], "WEIRD_MARKER")
