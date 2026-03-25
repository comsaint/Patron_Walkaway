"""Incident remediation follow-up review risks -> minimal reproducible contracts.

Tests-only guardrails; intentionally document current risk surfaces
without changing production behavior.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPO_ROOT / "trainer" / "serving" / "validator.py"


def _validator_text() -> str:
    return VALIDATOR_PATH.read_text(encoding="utf-8")


def _func_block(src: str, func_name: str) -> str:
    m = re.search(rf"def {re.escape(func_name)}\(", src)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"\n\ndef [A-Za-z_]\w*\(", src[start + 1 :])
    end = (start + 1 + nxt.start()) if nxt else len(src)
    return src[start:end]


class TestRisk1RetentionFieldAlignedContract(unittest.TestCase):
    """Risk #1 (remediated): DB and cache use validated_at primary, alert_ts fallback."""

    def test_db_prune_matches_cache_semantics(self) -> None:
        block = _func_block(_validator_text(), "prune_validator_retention")
        self.assertIn("validated_at IS NOT NULL", block)
        self.assertIn("alert_ts < ?", block)

    def test_cache_prune_uses_retention_row_ts_helper(self) -> None:
        block = _func_block(_validator_text(), "_prune_existing_results_cache")
        self.assertIn("_retention_row_ts_hk(row)", block)


class TestRisk2CachePruneThrottledContract(unittest.TestCase):
    """Risk #2 (remediated): full-map prune gated by interval, not necessarily every cycle."""

    def test_validate_once_gates_prune_with_should_run(self) -> None:
        block = _func_block(_validator_text(), "validate_once")
        self.assertIn("_should_run_cache_prune()", block)
        self.assertIn("_prune_existing_results_cache(existing_results, now_hk=now_hk)", block)

    def test_prune_iterates_over_all_keys(self) -> None:
        block = _func_block(_validator_text(), "_prune_existing_results_cache")
        self.assertIn("for key, row in list(existing_results.items()):", block)


class TestRisk3WatermarkDriftResetContract(unittest.TestCase):
    """Risk #3 (remediated): max(rowid) < persisted watermark resets meta and bootstrap."""

    def test_incremental_loader_resets_on_drift(self) -> None:
        block = _func_block(_validator_text(), "load_existing_results_incremental")
        self.assertIn("current_max_rowid", block)
        self.assertIn("current_max_rowid < last_loaded_rowid", block)


class TestRisk4IdentifierLeakageInWarningContract(unittest.TestCase):
    """Risk #4: no-bet warning still logs full identifiers."""

    def test_warning_template_contains_full_identifiers(self) -> None:
        block = _func_block(_validator_text(), "validate_alert_row")
        self.assertIn("No bet data for casino_player_id=%s player_id=%s bet_id=%s", block)
        self.assertNotIn("mask", block.lower())


class TestRisk5ReviewRiskStringContractFragility(unittest.TestCase):
    """Risk #5: source-string contract tests remain in repo (fragile by nature)."""

    def test_existing_tests_assert_literal_source_strings(self) -> None:
        text = (REPO_ROOT / "tests" / "review_risks" / "test_validator_phase2_incremental_review_risks_mre.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("self.assertIn(", text)
        self.assertIn("_validator_text()", text)
        # Risk4 contract literals (update when validate_once merge semantics change).
        self.assertIn("warm_cache=existing_results_cache", text)


if __name__ == "__main__":
    unittest.main()

