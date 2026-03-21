"""
STATUS.md「Code Review：Pipeline §7 文件變更」風險 → 文件／設定契約測試（僅 tests）。

- §1：README 連結之 plan doc 須提及 out/models 或 MODEL_DIR；`config.DEFAULT_MODEL_DIR` 須指向 out/models（MRE）。
- §2：README 部署／MLflow 小節須同時含 bundle/ 與「有檔／present／best-effort」類語意。
- §3：provenance runbook 區分 Parameters 與 Artifacts（低脆度字串契約）。
- §5：繁／簡／英產物區塊均含 pipeline_diagnostics.json 與 bundle/（計數一致）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"
_PLAN_PIPELINE = _REPO_ROOT / "doc" / "plan_pipeline_diagnostics_and_mlflow_artifacts.md"
_CONFIG_PY = _REPO_ROOT / "trainer" / "core" / "config.py"
_RUNBOOK = _REPO_ROOT / "doc" / "phase2_provenance_query_runbook.md"


def _readme_text() -> str:
    return _README.read_text(encoding="utf-8")


def _slice_between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i == -1:
        return ""
    j = text.find(end, i + len(start))
    if j == -1:
        return ""
    return text[i:j]


class TestReviewerS7ConfigDefaultModelDirMre(unittest.TestCase):
    """§1 MRE: DEFAULT_MODEL_DIR is under out/models (trainer/core/config.py)."""

    def test_default_model_dir_assignment_in_config(self):
        cfg = _CONFIG_PY.read_text(encoding="utf-8")
        self.assertIn("DEFAULT_MODEL_DIR", cfg)
        m = re.search(
            r"DEFAULT_MODEL_DIR\s*:\s*Path\s*=\s*([^\n]+)",
            cfg,
        )
        self.assertIsNotNone(m, "config.py must define DEFAULT_MODEL_DIR: Path = ...")
        rhs = m.group(1)
        self.assertIn("out", rhs.replace(" ", ""))
        self.assertIn("models", rhs)


class TestReviewerS7ReadmeLinksPlanWithModelDirHint(unittest.TestCase):
    """§1: README points to plan doc that mentions out/models or MODEL_DIR (SSOT bridge)."""

    def test_readme_cites_plan_pipeline_doc(self):
        text = _readme_text()
        self.assertIn("doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md", text)

    def test_plan_pipeline_doc_mentions_out_models_or_model_dir(self):
        plan = _PLAN_PIPELINE.read_text(encoding="utf-8")
        self.assertTrue(
            "out/models" in plan or "MODEL_DIR" in plan,
            "plan_pipeline doc should mention out/models or MODEL_DIR (STATUS §7 review §1)",
        )


class TestReviewerS7ReadmeConditionalUploadWording(unittest.TestCase):
    """§2: Deploy/MLflow bullets: bundle/ + conditional / best-effort wording."""

    def _assert_block(self, block: str, lang: str) -> None:
        low = block.lower()
        self.assertIn("bundle/", block, lang)
        self.assertIn("best-effort", low, lang)
        cond = (
            "when present" in low
            or "有該檔" in block
            or "有该档" in block
            or "来源目录" in block
            or "來源目錄" in block
        )
        self.assertTrue(cond, f"{lang}: expected conditional copy (when present / 有該檔 / 来源目录)")

    def test_zh_tw_deploy_mlflow_bullet(self):
        text = _readme_text()
        block = _slice_between(text, "### 產物（trainer 輸出）", "### 注意事項")
        self.assertIn("部署／MLflow", block)
        self._assert_block(block, "zh-TW product section")

    def test_zh_cn_deploy_mlflow_bullet(self):
        text = _readme_text()
        block = _slice_between(text, "### 产物（trainer 输出）", "### 注意事项")
        self.assertIn("部署", block)
        self._assert_block(block, "zh-CN product section")

    def test_en_deploy_mlflow_paragraph(self):
        text = _readme_text()
        block = _slice_between(text, "## Artifacts (trainer output)", "## Notes")
        self.assertIn("Deploy / MLflow", block)
        self._assert_block(block, "en Artifacts section")


class TestReviewerS7ProvenanceRunbookParamsVsArtifacts(unittest.TestCase):
    """§3: Runbook explicitly names Parameters vs Artifacts (UI disambiguation)."""

    def test_runbook_lists_parameters_and_artifacts_headings(self):
        rb = _RUNBOOK.read_text(encoding="utf-8")
        self.assertIn("**Parameters**", rb)
        self.assertIn("**Artifacts**", rb)


class TestReviewerS7TrilingualPipelineDiagnosticsParity(unittest.TestCase):
    """§5: Each locale product block mentions pipeline_diagnostics.json and bundle/ same count."""

    def test_three_locales_same_keyword_counts(self):
        text = _readme_text()
        tw = _slice_between(text, "### 產物（trainer 輸出）", "### 注意事項")
        cn = _slice_between(text, "### 产物（trainer 输出）", "### 注意事项")
        en = _slice_between(text, "## Artifacts (trainer output)", "## Notes")
        for name, block in (("zh-TW", tw), ("zh-CN", cn), ("en", en)):
            self.assertGreaterEqual(block.count("pipeline_diagnostics.json"), 1, name)
            self.assertGreaterEqual(block.count("bundle/"), 1, name)
        self.assertEqual(
            tw.count("pipeline_diagnostics.json"),
            cn.count("pipeline_diagnostics.json"),
            "zh-TW vs zh-CN pipeline_diagnostics.json count",
        )
        self.assertEqual(
            tw.count("bundle/"),
            cn.count("bundle/"),
            "zh-TW vs zh-CN bundle/ count",
        )
        self.assertEqual(
            en.count("pipeline_diagnostics.json"),
            tw.count("pipeline_diagnostics.json"),
            "en vs zh-TW pipeline_diagnostics.json count",
        )
        self.assertEqual(
            en.count("bundle/"),
            tw.count("bundle/"),
            "en vs zh-TW bundle/ count",
        )


class TestReviewerS7MlflowEnvExampleUiCommentExists(unittest.TestCase):
    """Optional sanity: example file documents where to see system/* (no ** assertion per review §4)."""

    def test_mlflow_env_example_has_ui_comment_line(self):
        p = _REPO_ROOT / "credential" / "mlflow.env.example"
        text = p.read_text(encoding="utf-8")
        self.assertIn("# UI:", text)
        self.assertIn("system/", text)
