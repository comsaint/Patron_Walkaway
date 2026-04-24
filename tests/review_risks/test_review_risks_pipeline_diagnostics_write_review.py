"""
STATUS.md「Code Review：`pipeline_diagnostics` 寫檔、`copy_model_bundle` 與 §6 單元測試（2026-03-21）」
風險點 → 原始碼契約／MRE 測試（僅 tests，不改 production）。

對應 STATUS 小節 §1（寫入路徑）、§2（default=str）、§4（僅 pipeline_diagnostics 缺檔 warning）。
"""

from __future__ import annotations

import json
import inspect
import tempfile
import unittest
from pathlib import Path

from package import build_deploy_package
from trainer.training import trainer as trainer_mod


def _write_pipeline_diagnostics_src() -> str:
    return inspect.getsource(trainer_mod._write_pipeline_diagnostics_json)


def _copy_model_bundle_src() -> str:
    return inspect.getsource(build_deploy_package.copy_model_bundle)


class TestReviewerWritePipelineDiagnosticsSourceContracts(unittest.TestCase):
    """STATUS Code Review §1 / §2: lock current implementation for intentional changes."""

    def test_mre_uses_write_text_not_os_replace_in_helper(self):
        """§1 MRE: direct write_text (crash mid-write → partial file risk); flip when tmp+replace lands."""
        src = _write_pipeline_diagnostics_src()
        self.assertIn("write_text", src)
        self.assertNotIn(
            "os.replace",
            src,
            "If atomic write is implemented with os.replace, update this contract (STATUS §1).",
        )

    def test_contract_json_dumps_uses_default_str(self):
        """§2 MRE: default=str silences type errors as strings — intentional until production tightens types."""
        src = _write_pipeline_diagnostics_src()
        self.assertIn("json.dumps", src)
        self.assertIn("default=str", src)


class TestReviewerCopyModelBundleWarnBranchContract(unittest.TestCase):
    """STATUS Code Review §4: only pipeline_diagnostics.json triggers missing-file warning in loop."""

    def test_elif_warns_only_pipeline_diagnostics_json_literal(self):
        src = _copy_model_bundle_src()
        self.assertIn('elif name == "pipeline_diagnostics.json":', src)
        self.assertIn("logger.warning", src)


class TestReviewerPipelineDiagnosticsStep78Evidence(unittest.TestCase):
    """Behavior guard for newly added Step 7/8 runtime evidence fields."""

    def test_helper_writes_step7_step8_runtime_evidence_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            trainer_mod._write_pipeline_diagnostics_json(
                model_version="v-test",
                pipeline_started_at="2026-04-24T00:00:00+00:00",
                pipeline_finished_at="2026-04-24T00:10:00+00:00",
                total_duration_sec=600.0,
                step7_chunk_parquet_total_bytes=123456789,
                step7_chunk_parquet_est_ram_gb=4.5,
                step8_screening_source="in_memory_head",
                step8_screening_stats_source="screening_sample_df",
                step8_screening_sample_rows=2000000,
                step8_screening_full_train_rows=9876543,
                step8_screening_candidate_cols=71,
                step8_screened_feature_count=29,
                output_dir=out_dir,
            )
            payload = json.loads(
                (out_dir / "pipeline_diagnostics.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["step7_chunk_parquet_total_bytes"], 123456789)
            self.assertEqual(payload["step7_chunk_parquet_est_ram_gb"], 4.5)
            self.assertEqual(payload["step8_screening_source"], "in_memory_head")
            self.assertEqual(
                payload["step8_screening_stats_source"], "screening_sample_df"
            )
            self.assertEqual(payload["step8_screening_sample_rows"], 2000000)
            self.assertEqual(payload["step8_screening_full_train_rows"], 9876543)
            self.assertEqual(payload["step8_screening_candidate_cols"], 71)
            self.assertEqual(payload["step8_screened_feature_count"], 29)

