"""Round 384 Review — 步驟 9 文件化風險點轉成最小可重現測試。

STATUS.md « Round 384 Review »：將審查風險點轉為文件/契約測試或靜態檢查。
僅新增測試，不修改 production code。

Reference: PLAN § 二、寫出與載入、三、強制重建；STATUS Round 384 Review；DECISION_LOG.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README_PATH = _REPO_ROOT / "README.md"


def _readme_text() -> str:
    return _README_PATH.read_text(encoding="utf-8")


def _paragraphs(text: str) -> list[str]:
    """Split into paragraphs (blocks separated by blank lines or ##/###)."""
    blocks = re.split(r"\n\s*\n", text)
    return [b.strip() for b in blocks if b.strip()]


# ---------------------------------------------------------------------------
# R384 Review #1 — README 應提及 Scorer 與 canonical / rebuild-canonical
# ---------------------------------------------------------------------------


class TestR384_1_ReadmeMentionsScorerAndCanonical(unittest.TestCase):
    """Review #1: README must document that scorer also uses canonical mapping artifact / rebuild flag."""

    def test_readme_has_scorer_and_canonical_in_same_context(self):
        """At least one paragraph in README must contain both 'scorer' and ('canonical' or 'rebuild-canonical' or 'canonical_mapping')."""
        text = _readme_text()
        paragraphs = _paragraphs(text)
        canonical_keywords = ("canonical", "rebuild-canonical", "canonical_mapping")
        found = False
        for para in paragraphs:
            lower = para.lower()
            if "scorer" in lower:
                if any(kw in lower for kw in canonical_keywords):
                    found = True
                    break
        self.assertTrue(
            found,
            "README should have at least one paragraph mentioning both scorer and canonical mapping / rebuild-canonical (R384 Review #1)",
        )


# ---------------------------------------------------------------------------
# R384 Review #2 — README 應提及載入失敗時會重建／fallback
# ---------------------------------------------------------------------------


class TestR384_2_ReadmeMentionsRebuildOrFallbackOnCanonicalLoadFailure(unittest.TestCase):
    """Review #2: README or doc should mention that canonical load failure (e.g. missing columns) triggers rebuild/fallback."""

    def test_readme_mentions_rebuild_or_fallback_for_canonical_load(self):
        """README canonical artifact section should mention rebuild/fallback when load fails (e.g. missing columns)."""
        text = _readme_text()
        # Look for canonical/載入/artifact context and rebuild/fallback/從頭建表
        rebuild_keywords = ("重建", "fallback", "從頭建表", "missing columns", "必要欄位", "required columns")
        canonical_section = re.search(
            r"Canonical mapping 共用 artifact.*?(?=###|\Z)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if not canonical_section:
            canonical_section = re.search(
                r"Canonical mapping shared artifact.*?(?=##|\Z)",
                text,
                re.DOTALL | re.IGNORECASE,
            )
        self.assertIsNotNone(canonical_section, "README should have Canonical mapping artifact section")
        section = (canonical_section.group(0) if canonical_section else "").lower()
        has_rebuild_mention = any(kw in section for kw in rebuild_keywords)
        self.assertTrue(
            has_rebuild_mention,
            "Canonical mapping section should mention rebuild/fallback when load fails (e.g. missing columns) (R384 Review #2)",
        )


# ---------------------------------------------------------------------------
# R384 Review #3 — README 所述 PLAN 路徑存在
# ---------------------------------------------------------------------------


class TestR384_3_ReadmePlanPathExists(unittest.TestCase):
    """Review #3: Path referenced in README for PLAN (.cursor/plans/PLAN.md) must exist as file."""

    def test_cursor_plans_plan_md_exists(self):
        """README references .cursor/plans/PLAN.md; that path must exist under repo root."""
        plan_path = _REPO_ROOT / ".cursor" / "plans" / "PLAN.md"
        self.assertTrue(
            plan_path.is_file(),
            "README references .cursor/plans/PLAN.md; path must exist (R384 Review #3)",
        )


# ---------------------------------------------------------------------------
# R384 Review #4 — README 可選：data/ 信任邊界提醒
# ---------------------------------------------------------------------------


class TestR384_4_ReadmeDataTrustBoundaryOptional(unittest.TestCase):
    """Review #4 (optional): README may mention data/ trust boundary (受控/信任/權限 or controlled/trust/permission)."""

    def test_readme_mentions_data_trust_or_controlled(self):
        """README should mention that data/ is a trust boundary (controlled deployment / permissions)."""
        text = _readme_text()
        # Check for data/ or "data" in context of trust/controlled/permission
        trust_keywords_zh = ("受控", "信任", "權限", "勿讓未信任")
        trust_keywords_en = ("controlled", "trust", "permission", "untrusted")
        lower = text.lower()
        has_trust_zh = any(kw in text for kw in trust_keywords_zh)
        has_trust_en = any(kw in lower for kw in trust_keywords_en)
        has_data = "data" in lower and ("data/" in text or "`data/`" in text or "data directory" in lower)
        self.assertTrue(
            has_data and (has_trust_zh or has_trust_en),
            "README may mention data/ trust boundary (R384 Review #4 optional)",
        )


if __name__ == "__main__":
    unittest.main()
