"""
STATUS.md「Code Review：`pipeline_diagnostics` 寫檔、`copy_model_bundle` 與 §6 單元測試（2026-03-21）」
風險點 → 原始碼契約／MRE 測試（僅 tests，不改 production）。

對應 STATUS 小節 §1（寫入路徑）、§2（default=str）、§4（僅 pipeline_diagnostics 缺檔 warning）。
"""

from __future__ import annotations

import inspect
import unittest

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

