"""Phase 0 + 項目 6 Code Review — 風險點轉成最小可重現契約測試。

STATUS.md « Code Review：Phase 0 + 項目 6 變更 »：將審查風險點轉為 PROJECT.md / README 契約測試。
僅新增測試，不修改 production code。

Reference: .cursor/plans/STATUS.md § Code Review Phase 0 + 項目 6；PLAN.md § Phase 2 前結構整理。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_MD = _REPO_ROOT / "PROJECT.md"
_README_MD = _REPO_ROOT / "README.md"


def _project_text() -> str:
    return _PROJECT_MD.read_text(encoding="utf-8")


def _readme_text() -> str:
    return _README_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Review #1 — data/ 下 out/ 語意歧義：產出約定須明確寫「不採用 data/out/」
# ---------------------------------------------------------------------------


class TestPhase0Review1_OutputConventionExplicit(unittest.TestCase):
    """Review #1: PROJECT.md must state output convention clearly (root out/, not data/out/) to avoid ambiguity."""

    def test_project_md_states_no_data_out(self):
        """PROJECT.md must state that data/out/ is not adopted (convention explicit); may use backticks."""
        text = _project_text()
        has_no_adopt = "不採用" in text
        has_data_out = "data/out" in text
        self.assertTrue(
            has_no_adopt and has_data_out,
            msg="PROJECT.md should state '不採用' and 'data/out' (e.g. 不採用 data/out/) to avoid ambiguity (STATUS Code Review #1). "
            "Found: 不採用=%s, data/out=%s" % (has_no_adopt, has_data_out),
        )

    def test_project_md_states_root_out_convention(self):
        """PROJECT.md must mention root 'out/' as the output convention."""
        text = _project_text()
        self.assertTrue(
            "out/" in text and ("根目錄" in text or "root" in text.lower()),
            msg="PROJECT.md should state that output uses root out/ (STATUS Code Review #1).",
        )


# ---------------------------------------------------------------------------
# Review #2 — 項目 2 實施前 trainer 為扁平結構應說明
# ---------------------------------------------------------------------------


class TestPhase0Review2_TrainerFlatStructureMentioned(unittest.TestCase):
    """Review #2: PROJECT.md should mention that before 項目 2, trainer/ is flat (no core/features/... yet)."""

    def test_project_md_mentions_flat_structure_before_item2(self):
        """PROJECT.md must contain both '項目 2' and ('扁平' or '實施前') in trainer/structure context."""
        text = _project_text()
        has_item2 = "項目 2" in text
        has_flat_or_before = "扁平" in text or "實施前" in text
        self.assertTrue(
            has_item2 and has_flat_or_before,
            msg="PROJECT.md should state that before 項目 2, trainer/ is flat (STATUS Code Review #2). "
            "Current: item2=%s, flat_or_before=%s" % (has_item2, has_flat_or_before),
        )


# ---------------------------------------------------------------------------
# Review #3 — 重要入口須提視窗參數（start/end/days）
# ---------------------------------------------------------------------------


class TestPhase0Review3_EntryWindowParamsMentioned(unittest.TestCase):
    """Review #3: Important-entrance section must mention window params (start/end/days) for trainer/backtester."""

    def test_project_md_important_entrance_section_has_window_keywords(self):
        """In PROJECT.md, the 重要入口 section must mention 'start' or 'end' or 'days' (window params)."""
        text = _project_text()
        # 重要入口 is followed by a table; training row contains trainer.trainer
        if "重要入口" not in text or "trainer.trainer" not in text:
            self.skipTest("PROJECT.md structure changed (no 重要入口 / trainer.trainer)")
        # Section between "## 重要入口" and next "##"
        match = re.search(r"## 重要入口\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        self.assertTrue(match, msg="PROJECT.md must have ## 重要入口 section")
        section = match.group(1)
        has_window = "start" in section or "end" in section or "days" in section
        self.assertTrue(
            has_window,
            msg="PROJECT.md 重要入口 section should mention window params (start/end/days) for trainer (STATUS Code Review #3).",
        )

    def test_project_md_important_entrance_table_has_at_least_eight_rows(self):
        """Important-entrance table must have at least 8 rows (avoid accidental deletion)."""
        text = _project_text()
        match = re.search(r"## 重要入口\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        self.assertTrue(match, msg="PROJECT.md must have ## 重要入口 section")
        section = match.group(1)
        # Count table rows (lines starting with | and containing more than one |)
        rows = [line for line in section.splitlines() if line.strip().startswith("|") and line.count("|") >= 2]
        self.assertGreaterEqual(
            len(rows),
            8,
            msg="PROJECT.md 重要入口 table should have at least 8 rows (STATUS Code Review #3). Got %s." % len(rows),
        )


# ---------------------------------------------------------------------------
# Review #4 — 產出約定須提 .gitignore（out/ 勿進版控）
# ---------------------------------------------------------------------------


class TestPhase0Review4_OutputSectionMentionsGitignore(unittest.TestCase):
    """Review #4: Output convention section should mention .gitignore or 版控 so out/ is not committed."""

    def test_project_md_output_section_mentions_gitignore_or_version_control(self):
        """In PROJECT.md, 產出 section (or paragraph containing 'out/') must mention .gitignore or 版控."""
        text = _project_text()
        if "產出" not in text or "out/" not in text:
            self.skipTest("PROJECT.md structure changed (no 產出/out/)")
        match = re.search(r"產出與可執行腳本約定\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        self.assertTrue(match, msg="PROJECT.md must have 產出與可執行腳本約定 section")
        section = match.group(1)
        has_gitignore = ".gitignore" in section or "版控" in section or "gitignore" in section.lower()
        self.assertTrue(
            has_gitignore,
            msg="PROJECT.md 產出約定 should mention .gitignore or 版控 for out/ (STATUS Code Review #4).",
        )


# ---------------------------------------------------------------------------
# Review #5 — 文件索引須同時含 doc/、schema/、ssot/
# ---------------------------------------------------------------------------


class TestPhase0Review5_FileIndexHasDocSchemaSsot(unittest.TestCase):
    """Review #5: File-index table must list doc/, schema/, ssot/ so spec sources are not narrowed to doc/ only."""

    def test_project_md_file_index_table_has_doc_schema_ssot(self):
        """PROJECT.md 文件索引 section must contain doc/, schema/, ssot/ in the table."""
        text = _project_text()
        match = re.search(r"## 文件索引\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        self.assertTrue(match, msg="PROJECT.md must have ## 文件索引 section")
        section = match.group(1)
        for required in ("doc/", "schema/", "ssot/"):
            self.assertIn(
                required,
                section,
                msg="PROJECT.md 文件索引 table should list %s (STATUS Code Review #5)." % required,
            )


# ---------------------------------------------------------------------------
# README 與 PROJECT 對齊：三處文件表皆有 PROJECT.md 列
# ---------------------------------------------------------------------------


class TestPhase0ReadmeReferencesProjectMd(unittest.TestCase):
    """README must reference PROJECT.md in all three language file tables (per 項目 6.2)."""

    def test_readme_has_project_md_in_doc_table_three_times(self):
        """README.md must contain 'PROJECT.md' in file/doc table at least three times (zh-TW, zh-CN, en)."""
        text = _readme_text()
        count = text.count("PROJECT.md")
        self.assertGreaterEqual(
            count,
            3,
            msg="README should list PROJECT.md in doc table for all three language sections (項目 6.2). Got %s." % count,
        )


if __name__ == "__main__":
    unittest.main()
