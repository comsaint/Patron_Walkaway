"""
Task 3 Phase 2 review risks -> minimal reproducible tests/contracts.

Tests only; no production changes.
Focuses on validator incremental rowid-watermark path.
"""

from __future__ import annotations

import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_VALIDATOR = _REPO_ROOT / "trainer" / "serving" / "validator.py"


def _validator_text() -> str:
    return _VALIDATOR.read_text(encoding="utf-8")


class TestRisk1ExceptionSwallowingContract(unittest.TestCase):
    """Risk #1: broad exception swallowing may hide incremental load issues."""

    def test_incremental_loader_has_broad_except_paths(self) -> None:
        text = _validator_text()
        start = text.find("def load_existing_results_incremental(")
        self.assertNotEqual(start, -1)
        end = text.find("\ndef ", start + 1)
        block = text[start:end if end != -1 else len(text)]
        self.assertIn("except Exception:", block)


class TestRisk2WatermarkDriftResetContract(unittest.TestCase):
    """Risk #2 (remediated): reset when persisted watermark > max(rowid) (restore/trim)."""

    def test_drift_reset_condition_and_meta_delete(self) -> None:
        text = _validator_text()
        start = text.find("def load_existing_results_incremental(")
        end = text.find("\ndef ", start + 1)
        block = text[start:end if end != -1 else len(text)]
        self.assertIn("current_max_rowid", block)
        self.assertIn("current_max_rowid < last_loaded_rowid", block)
        self.assertIn("DELETE FROM validator_runtime_meta WHERE key = ?", block)


class TestRisk3MetaWriteTransactionBoundaryContract(unittest.TestCase):
    """Risk #3: watermark persistence is in helper, not explicit same transaction block."""

    def test_watermark_setter_commits_independently(self) -> None:
        text = _validator_text()
        start = text.find("def _set_validation_results_last_loaded_rowid(")
        self.assertNotEqual(start, -1)
        end = text.find("\ndef ", start + 1)
        block = text[start:end if end != -1 else len(text)]
        self.assertIn("conn.commit()", block)


class TestRisk4PerCycleDictRebuildContract(unittest.TestCase):
    """Risk #4 mitigated: validate_once loads DB-first, then fills missing keys from cache."""

    def test_validate_once_loads_incremental_db_first_with_warm_cache(self) -> None:
        text = _validator_text()
        self.assertIn("DB-first", text)
        self.assertIn("warm_cache=existing_results_cache", text)
        self.assertIn(
            "existing_results = load_existing_results_incremental(",
            text,
        )
        self.assertIn("if _k not in existing_results:", text)


class TestRisk5WatermarkTamperHardeningContract(unittest.TestCase):
    """Risk #5: watermark table has no extra integrity checks/constraints beyond key/value."""

    def test_runtime_meta_is_key_value_without_numeric_constraint(self) -> None:
        text = _validator_text()
        start = text.find("CREATE TABLE IF NOT EXISTS validator_runtime_meta")
        self.assertNotEqual(start, -1)
        snippet = text[start : start + 220]
        self.assertIn("key TEXT PRIMARY KEY", snippet)
        self.assertIn("value TEXT", snippet)
        self.assertNotIn("CHECK", snippet)


class TestRisk6LegacyCsvFallbackContract(unittest.TestCase):
    """Risk #6: bootstrap still includes legacy CSV fallback path."""

    def test_incremental_loader_keeps_csv_fallback_on_bootstrap(self) -> None:
        text = _validator_text()
        start = text.find("def load_existing_results_incremental(")
        end = text.find("\ndef ", start + 1)
        block = text[start:end if end != -1 else len(text)]
        self.assertIn("RESULTS_PATH.exists()", block)
        self.assertIn("last_loaded_rowid <= 0", block)


if __name__ == "__main__":
    unittest.main()

