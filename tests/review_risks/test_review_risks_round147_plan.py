"""Round 147 Review — plan/lint checks (tests-only).

P3: If the 特徵整合計畫（已實作）section is extended with Step 9 or higher,
this test fails to remind updating top-level todos or feat-consolidation.
No production or PLAN edits in this file.
"""

from __future__ import annotations

import re
import unittest
import pathlib


def _plan_path() -> pathlib.Path:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    return repo_root / ".cursor" / "plans" / "PLAN.md"


def _extract_feat_consolidation_section(content: str) -> str | None:
    """Extract the 特徵整合計畫（已實作）section body (between ## and next ##)."""
    start_marker = "## 特徵整合計畫"
    if start_marker not in content:
        return None
    start = content.find(start_marker)
    # start of next ## at same level (##, not ###)
    rest = content[start + len(start_marker) :]
    match = re.search(r"\n## [^#]", rest)
    end = match.start() + 1 if match else len(rest)
    return rest[:end]


class TestRound147PlanFeatConsolidationNoStep9WithoutTodoSync(unittest.TestCase):
    """Round 147 Review P3: 已實作 section extended with Step 9+ should trigger todo sync.

    If the 特徵整合計畫（已實作）section gains '### Step 9' or higher, update
    top-level todos or feat-consolidation sub-items (see STATUS Round 147 P3)."""

    def test_feat_consolidation_section_has_no_step9_or_higher(self) -> None:
        path = _plan_path()
        self.assertTrue(path.exists(), f"PLAN.md not found: {path}")
        content = path.read_text(encoding="utf-8")
        section = _extract_feat_consolidation_section(content)
        self.assertIsNotNone(section, "PLAN.md should contain 特徵整合計畫 section")
        # Match ### Step N where N >= 9
        step9_plus = re.findall(r"###\s*Step\s+(\d+)", section)
        over = [n for n in step9_plus if int(n) >= 9]
        self.assertEqual(
            over,
            [],
            "特徵整合計畫（已實作）章節內出現 Step 9 以上時，請同步更新頂部 todos 或 "
            "feat-consolidation 子項（STATUS Round 147 Review P3）。",
        )
