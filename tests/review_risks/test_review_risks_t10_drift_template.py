"""T10 drift 模板／範例／runbook — Code Review 風險點轉成最小可重現契約測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：T10 變更（drift 模板、範例、runbook 指向）» §1–§4。
§1：模板與範例內提及 phase2_model_rollback_runbook / provenance_query_runbook 時須有 doc/ 前綴。
§2：模板內含「另存新檔」或「勿覆蓋」使用說明。
§3：模板或 alert runbook 情境三內含敏感資訊提醒（脫敏／勿 commit）。
§4（可選）：範例若提及 skew 檢查，須含 trainer.scripts.check_training_serving_skew 或 phase2_skew_check_runbook。

執行方式（repo 根目錄）：
  pytest tests/review_risks/test_review_risks_t10_drift_template.py -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_DIR = REPO_ROOT / "doc"
DRIFT_TEMPLATE = DOC_DIR / "drift_investigation_template.md"
DRIFT_EXAMPLE = DOC_DIR / "phase2_drift_investigation_example.md"
ALERT_RUNBOOK = DOC_DIR / "phase2_alert_runbook.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §1 — 文件引用路徑須有 doc/ 前綴
# ---------------------------------------------------------------------------


class TestT10DocPathPrefix(unittest.TestCase):
    """Review §1: references to phase2_model_rollback_runbook / provenance_query_runbook must use doc/ prefix."""

    def test_template_rollback_runbook_has_doc_prefix(self):
        """drift_investigation_template.md: any mention of phase2_model_rollback_runbook must be with doc/ prefix."""
        if not DRIFT_TEMPLATE.exists():
            self.skipTest(f"{DRIFT_TEMPLATE} not found")
        text = _read(DRIFT_TEMPLATE)
        # After removing doc/ prefixed form, no bare reference should remain
        without_prefixed = text.replace("doc/phase2_model_rollback_runbook", "")
        self.assertNotIn(
            "phase2_model_rollback_runbook",
            without_prefixed,
            "template should reference phase2_model_rollback_runbook only with doc/ prefix (T10 §1)",
        )

    def test_template_provenance_runbook_has_doc_prefix(self):
        """drift_investigation_template.md: any mention of provenance_query_runbook must be with doc/ prefix."""
        if not DRIFT_TEMPLATE.exists():
            self.skipTest(f"{DRIFT_TEMPLATE} not found")
        text = _read(DRIFT_TEMPLATE)
        without_prefixed = text.replace("doc/phase2_provenance_query_runbook", "")
        self.assertNotIn(
            "provenance_query_runbook",
            without_prefixed,
            "template should reference provenance_query_runbook only with doc/ prefix (T10 §1)",
        )

    def test_example_rollback_runbook_has_doc_prefix(self):
        """phase2_drift_investigation_example.md: any mention of phase2_model_rollback_runbook must be with doc/ prefix."""
        if not DRIFT_EXAMPLE.exists():
            self.skipTest(f"{DRIFT_EXAMPLE} not found")
        text = _read(DRIFT_EXAMPLE)
        without_prefixed = text.replace("doc/phase2_model_rollback_runbook", "")
        self.assertNotIn(
            "phase2_model_rollback_runbook",
            without_prefixed,
            "example should reference phase2_model_rollback_runbook only with doc/ prefix (T10 §1)",
        )


# ---------------------------------------------------------------------------
# §2 — 模板須說明另存新檔／勿覆蓋
# ---------------------------------------------------------------------------


class TestT10TemplateSaveAsWarning(unittest.TestCase):
    """Review §2: template must instruct users to save-as and not overwrite."""

    def test_template_mentions_save_as_or_do_not_overwrite(self):
        """drift_investigation_template.md must contain 另存新檔 or 勿覆蓋."""
        if not DRIFT_TEMPLATE.exists():
            self.skipTest(f"{DRIFT_TEMPLATE} not found")
        text = _read(DRIFT_TEMPLATE)
        self.assertTrue(
            "另存新檔" in text or "勿覆蓋" in text,
            "template should instruct 另存新檔 or 勿覆蓋 (T10 §2)",
        )


# ---------------------------------------------------------------------------
# §3 — 敏感資訊提醒（模板或 runbook 情境三）
# ---------------------------------------------------------------------------


class TestT10SensitiveInfoReminder(unittest.TestCase):
    """Review §3: template or alert runbook scenario 3 must mention 脫敏 or 勿 commit."""

    def test_template_or_runbook_scenario3_mentions_desensitize_or_do_not_commit(self):
        """Either template or runbook 情境三 must contain 脫敏 or 勿 commit."""
        if not DRIFT_TEMPLATE.exists():
            self.skipTest(f"{DRIFT_TEMPLATE} not found")
        template_text = _read(DRIFT_TEMPLATE)
        template_ok = any(k in template_text for k in ("敏感", "脫敏", "勿 commit"))

        if ALERT_RUNBOOK.exists():
            runbook_text = _read(ALERT_RUNBOOK)
            # Extract 情境三 block (from ### 情境三 to next ## or ### or end)
            match = re.search(r"### 情境三[：:].*?(?=## |### |\Z)", runbook_text, re.DOTALL)
            scenario3 = match.group(0) if match else ""
            runbook_ok = "脫敏" in scenario3 or "勿 commit" in scenario3
        else:
            runbook_ok = False

        self.assertTrue(
            template_ok or runbook_ok,
            "template or alert runbook 情境三 should mention 脫敏 or 勿 commit (T10 §3)",
        )


# ---------------------------------------------------------------------------
# §4 — 範例若提及 skew 檢查須含正確腳本名或 runbook
# ---------------------------------------------------------------------------


class TestT10ExampleSkewCheckReference(unittest.TestCase):
    """Review §4 (optional): if example mentions skew check, it must reference script or runbook."""

    def test_example_skew_check_mentions_script_or_runbook(self):
        """If example mentions skew check, it must contain trainer.scripts.check_training_serving_skew or phase2_skew_check_runbook."""
        if not DRIFT_EXAMPLE.exists():
            self.skipTest(f"{DRIFT_EXAMPLE} not found")
        text = _read(DRIFT_EXAMPLE)
        mentions_skew = "check_training_serving_skew" in text or "skew" in text
        if not mentions_skew:
            self.skipTest("example does not mention skew check")
        has_script = "trainer.scripts.check_training_serving_skew" in text
        has_runbook = "phase2_skew_check_runbook" in text
        self.assertTrue(
            has_script or has_runbook,
            "example mentions skew check but should reference trainer.scripts.check_training_serving_skew or phase2_skew_check_runbook (T10 §4)",
        )


if __name__ == "__main__":
    unittest.main()
