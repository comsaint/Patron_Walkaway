"""Phase 2 T7 alert runbook + message format — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：Phase 2 T7 變更（alert runbook + message format）» §1–§5。
§1：Runbook 內 .md 連結須統一 doc/ 前綴（如 phase2_evidently_usage.md → doc/phase2_evidently_usage.md）。
§2：Message format 文件須含 detail 欄位之脫敏／勿放敏感資訊說明。
§3：Runbook 在 Triage 情境區須有 Scorer 載入失敗之查證指引（Scorer + MODEL_DIR 或 rollback）。
§4：Runbook 須有 Validator DB 之明確說明（共用 state.db 或專用 DB 路徑見 config）。
§5：Message format 文件須含嚴重度對應建議（如 無法寫入為 error、precision 為 warning）。

執行方式（repo 根目錄）：
  pytest tests/review_risks/test_review_risks_phase2_alert_runbook_message_format.py -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = REPO_ROOT / "doc" / "phase2_alert_runbook.md"
MESSAGE_FORMAT = REPO_ROOT / "doc" / "phase2_alert_message_format.md"


def _runbook_text() -> str:
    if not RUNBOOK.exists():
        raise FileNotFoundError("doc/phase2_alert_runbook.md not found")
    return RUNBOOK.read_text(encoding="utf-8")


def _message_format_text() -> str:
    if not MESSAGE_FORMAT.exists():
        raise FileNotFoundError("doc/phase2_alert_message_format.md not found")
    return MESSAGE_FORMAT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §1 — Runbook 內 Evidently 等 .md 連結須以 doc/ 前綴書寫（維護性／連結）
# ---------------------------------------------------------------------------


class TestRunbookDocLinksUseDocPrefix(unittest.TestCase):
    """Review §1: Internal doc links in runbook must use doc/ prefix so links work from any cwd."""

    def test_runbook_evidently_doc_uses_doc_prefix(self):
        """Evidently usage doc reference must be doc/phase2_evidently_usage.md, not bare phase2_evidently_usage.md."""
        text = _runbook_text()
        # 若出現 phase2_evidently_usage.md，必須以 doc/ 前綴出現（不得僅有 phase2_evidently_usage.md）
        if "phase2_evidently_usage.md" in text:
            self.assertIn(
                "doc/phase2_evidently_usage.md",
                text,
                msg="Runbook must reference doc/phase2_evidently_usage.md, not bare phase2_evidently_usage.md "
                "(STATUS T7 Code Review §1).",
            )
            # 不應出現未加 doc/ 的單獨檔名（替換掉 doc/... 後不應還有裸檔名）
            without_doc_prefix = text.replace("doc/phase2_evidently_usage.md", "")
            self.assertNotIn(
                "phase2_evidently_usage.md",
                without_doc_prefix,
                msg="Runbook table or body must not use bare phase2_evidently_usage.md; use doc/ prefix (T7 §1).",
            )


# ---------------------------------------------------------------------------
# §2 — Message format 之 detail 欄位須有脫敏／勿放敏感資訊說明（安全性）
# ---------------------------------------------------------------------------


class TestMessageFormatDetailSensitiveGuidance(unittest.TestCase):
    """Review §2: Message format doc must state that detail field should be sanitized / no secrets or PII."""

    def test_message_format_doc_contains_detail_sanitization_guidance(self):
        """Doc must contain at least one of 脫敏, 勿放, 敏感, PII, 密碼, token (for detail field)."""
        text = _message_format_text()
        keywords = ("脫敏", "勿放", "敏感", "PII", "密碼", "API token", "token")
        self.assertTrue(
            any(k in text for k in keywords),
            msg="Message format doc must state that detail field is sanitized / no secrets or PII "
            "(STATUS T7 Code Review §2). Add a sentence in 建議欄位 or 原則.",
        )


# ---------------------------------------------------------------------------
# §3 — Runbook 須在 Triage 情境區有 Scorer 載入失敗之查證指引（完整性）
# ---------------------------------------------------------------------------


class TestRunbookScorerLoadFailureTriage(unittest.TestCase):
    """Review §3: Runbook must have actionable guidance for Scorer load/artifact failure in triage section."""

    def test_runbook_triage_section_mentions_scorer_and_model_dir_or_rollback(self):
        """After 'Triage 情境與步驟', there must be a scenario or paragraph with Scorer and MODEL_DIR or rollback."""
        text = _runbook_text()
        idx = text.find("## Triage 情境與步驟")
        self.assertGreaterEqual(idx, 0, msg="Runbook must have section '## Triage 情境與步驟'")
        after_triage = text[idx:]
        # 至少有一個情境標題（###）或段落同時提到 Scorer 與 MODEL_DIR 或 rollback
        has_scorer_model_dir = "Scorer" in after_triage and ("MODEL_DIR" in after_triage or "rollback" in after_triage)
        self.assertTrue(
            has_scorer_model_dir,
            msg="Runbook Triage section must mention Scorer and (MODEL_DIR or rollback) for load/artifact failure "
            "(STATUS T7 Code Review §3). Add 情境四 or a paragraph under 常見異常.",
        )


# ---------------------------------------------------------------------------
# §4 — Runbook 須明確說明 Validator 使用之 DB（state.db 或專用 DB 路徑）
# ---------------------------------------------------------------------------


class TestRunbookValidatorDbClarification(unittest.TestCase):
    """Review §4: Runbook must disambiguate Validator DB (e.g. 共用 state.db or 專用 DB path in config)."""

    def test_runbook_clarifies_validator_db(self):
        """Runbook must contain a clarifying phrase: 共用 state.db, 相同之 state.db, or 專用 DB with 路徑/config."""
        text = _runbook_text()
        has_shared = "共用" in text and "state.db" in text
        has_same_db = "相同之 state.db" in text
        has_dedicated_with_path = "專用 DB" in text and ("路徑" in text or "config" in text or "路徑見" in text)
        self.assertTrue(
            has_shared or has_same_db or has_dedicated_with_path,
            msg="Runbook must state whether Validator uses shared state.db or dedicated DB and where "
            "(STATUS T7 Code Review §4). E.g. '本專案 Validator 使用與 Scorer 相同之 state.db' or 'Validator DB 路徑見 config'.",
        )


# ---------------------------------------------------------------------------
# §5 — Message format 須含嚴重度對應建議（邊界／實務）
# ---------------------------------------------------------------------------


class TestMessageFormatSeverityMapping(unittest.TestCase):
    """Review §5: Message format doc must contain severity mapping guidance (e.g. when to use error vs warning)."""

    def test_message_format_doc_contains_severity_mapping_guidance(self):
        """Doc must contain 嚴重度建議 or explicit mapping (e.g. 無法寫入為 error, precision 為 warning)."""
        text = _message_format_text()
        has_section = "嚴重度建議" in text
        has_mapping = ("為 error" in text or "為 warning" in text or "無法寫入為" in text) and "嚴重度" in text
        self.assertTrue(
            has_section or has_mapping,
            msg="Message format doc must add 嚴重度建議 or mapping (e.g. scorer/export 無法寫入為 error, "
            "validator precision 為 warning) (STATUS T7 Code Review §5).",
        )
