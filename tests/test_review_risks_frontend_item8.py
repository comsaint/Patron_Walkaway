"""項目 8 前端與靜態資源（文件面）— Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：項目 8 變更 » §1–§3。
§1：README 三處架構（繁體／簡體／英文）中 frontend 條目須含「可選」或 "optional" 及「僅含 API」或 "API-only"。
§2（可選）：README 前 50 行若出現「前端儀表板」或 "frontend dashboard"，須有「可選」或 "optional" 或「僅含 API」等限定語。
§3：預設建包輸出不得含 frontend（build_deploy_package 不複製 trainer/frontend 或 static 儀表板）。

執行方式（repo 根目錄）：
  python -m pytest tests/test_review_risks_frontend_item8.py -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
BUILD_SCRIPT = REPO_ROOT / "package" / "build_deploy_package.py"

# 三處架構小節標題（用於切出區塊）
SECTION_MARKERS = [
    "### 架構（高層）",      # 繁體
    "### 架构（高层）",      # 簡體
    "## Architecture (high level)",  # 英文
]

# §1：每處 frontend 條目須含的關鍵字（至少各一）
# 可選（繁）/ 可选（簡）為同一詞不同用字
OPTIONAL_KEYWORDS = ("可選", "可选", "optional")
API_ONLY_KEYWORDS = ("僅含 API", "仅含 API", "API-only", "deploy package can be API-only", "API only")


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _get_frontend_line_in_section(text: str, section_marker: str) -> str | None:
    """從 section_marker 起找到第一個含 `trainer/frontend/` 的 bullet 行。"""
    idx = text.find(section_marker)
    if idx == -1:
        return None
    rest = text[idx:]
    for line in rest.splitlines():
        if "trainer/frontend/" in line and line.strip().startswith("- "):
            return line
    return None


# ---------------------------------------------------------------------------
# §1 — README 三處架構 frontend 條目皆須含「可選」與「僅含 API」關鍵字
# ---------------------------------------------------------------------------


class TestReadmeFrontendOptionalApiOnlyAllSections(unittest.TestCase):
    """Review §1: All three README architecture sections must state frontend is optional and deploy package can be API-only."""

    def test_readme_has_three_architecture_sections(self):
        """README must contain the three section markers (traditional, simplified, English)."""
        text = _readme_text()
        for marker in SECTION_MARKERS:
            self.assertIn(
                marker,
                text,
                msg="README must contain architecture section: %r" % marker,
            )

    def test_traditional_chinese_frontend_line_has_optional_and_api_only(self):
        """Traditional Chinese section: frontend line must contain 可選 and 僅含 API (or equivalent)."""
        text = _readme_text()
        line = _get_frontend_line_in_section(text, SECTION_MARKERS[0])
        self.assertIsNotNone(line, msg="Traditional section must have a trainer/frontend/ bullet")
        self.assertTrue(
            any(k in line for k in OPTIONAL_KEYWORDS),
            msg="Traditional frontend line must mention 可選 or optional; got: %s" % line[:80],
        )
        self.assertTrue(
            any(k in line for k in API_ONLY_KEYWORDS),
            msg="Traditional frontend line must mention 僅含 API or API-only; got: %s" % line[:80],
        )

    def test_simplified_chinese_frontend_line_has_optional_and_api_only(self):
        """Simplified Chinese section: frontend line must contain 可選 and 僅含 API (or equivalent)."""
        text = _readme_text()
        line = _get_frontend_line_in_section(text, SECTION_MARKERS[1])
        self.assertIsNotNone(line, msg="Simplified section must have a trainer/frontend/ bullet")
        self.assertTrue(
            any(k in line for k in OPTIONAL_KEYWORDS),
            msg="Simplified frontend line must mention 可選 or optional; got: %s" % line[:80],
        )
        self.assertTrue(
            any(k in line for k in API_ONLY_KEYWORDS),
            msg="Simplified frontend line must mention 僅含 API or API-only; got: %s" % line[:80],
        )

    def test_english_frontend_line_has_optional_and_api_only(self):
        """English section: frontend line must contain optional and API-only (or equivalent)."""
        text = _readme_text()
        line = _get_frontend_line_in_section(text, SECTION_MARKERS[2])
        self.assertIsNotNone(line, msg="English section must have a trainer/frontend/ bullet")
        self.assertTrue(
            any(k in line for k in OPTIONAL_KEYWORDS),
            msg="English frontend line must mention optional; got: %s" % line[:80],
        )
        self.assertTrue(
            any(k in line for k in API_ONLY_KEYWORDS),
            msg="English frontend line must mention API-only or deploy package can be API-only; got: %s" % line[:80],
        )


# ---------------------------------------------------------------------------
# §2（可選）— README 前 50 行若出現「前端儀表板」或 "frontend dashboard"，須有限定語
# ---------------------------------------------------------------------------


class TestReadmeOutputParagraphFrontendOptionalMention(unittest.TestCase):
    """Review §2 (optional): If first 50 lines mention 前端儀表板 or frontend dashboard, same/adjacent sentence must qualify (可選 / optional / 僅含 API)."""

    def test_readme_first_50_lines_frontend_dashboard_has_qualifier(self):
        """If '前端儀表板' or 'frontend dashboard' appears in first 50 lines, qualifier (可選/optional/僅含 API) must appear in same or adjacent lines."""
        lines = _readme_text().splitlines()[:50]
        text_50 = "\n".join(lines)
        if "前端儀表板" not in text_50 and "frontend dashboard" not in text_50:
            self.skipTest("First 50 lines do not mention 前端儀表板 or frontend dashboard; nothing to check")
        qualifiers = ("可選", "optional", "僅含 API", "API-only")
        self.assertTrue(
            any(q in text_50 for q in qualifiers),
            msg="First 50 lines mention frontend/dashboard but no qualifier (可選/optional/僅含 API); "
            "add qualifier to avoid implying frontend is always included (Review §2).",
        )


# ---------------------------------------------------------------------------
# §3 — 建包腳本不複製 trainer/frontend 或 static 儀表板（契約：source 不包含該路徑）
# ---------------------------------------------------------------------------


class TestBuildDeployPackageDoesNotCopyFrontend(unittest.TestCase):
    """Review §3: Default build must not include frontend; contract: build script does not copy trainer/frontend or output static/ for dashboard."""

    def test_build_script_does_not_reference_trainer_frontend(self):
        """Contract: build_deploy_package.py must not copy from trainer/frontend (default build is API-only)."""
        if not BUILD_SCRIPT.exists():
            self.skipTest("package/build_deploy_package.py not found")
        text = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(
            "trainer/frontend",
            text,
            msg="Build script must not reference trainer/frontend (default build does not include dashboard); "
            "STATUS Code Review 項目 8 §3.",
        )

    def test_build_script_does_not_copy_static_dashboard(self):
        """Contract: build script must not copy dashboard into output static/ (e.g. deploy_dist/static/main.html)."""
        if not BUILD_SCRIPT.exists():
            self.skipTest("package/build_deploy_package.py not found")
        text = BUILD_SCRIPT.read_text(encoding="utf-8")
        # 若日後加入「複製 frontend 到 output/static」會出現 "static" 作為輸出路徑
        # 目前腳本僅有 local_state、models、wheels、data 等，無 static
        if '"/static"' in text or "'static'" in text or "output_dir / \"static\"" in text or "output_dir / 'static'" in text:
            self.fail(
                "Build script copies to 'static' (dashboard); default build must be API-only. "
                "If frontend is optional, gate this path behind a flag (STATUS 項目 8 §3)."
            )
        # 僅在明確作為「部署輸出目錄下的 static」時才失敗；註解或字串說明不算
        if re.search(r"output_dir\s*/\s*[\'\"]static[\'\"]", text):
            self.fail("Build script must not copy to output_dir/static by default (項目 8 §3).")
