"""Task 6 reviewer risk points -> minimal reproducible contract tests.

These tests intentionally document current behavior/risk surfaces without changing
production code.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPO_ROOT / "trainer" / "serving" / "validator.py"
INTEGRATION_TEST_PATH = (
    REPO_ROOT / "tests" / "integration" / "test_four_bet_label_vs_validator_simulation.py"
)
DOC_PATH = REPO_ROOT / "doc" / "validator_gap_started_before_alert_issue.md"


class TestRisk1IgnoredReasonsKpiTransition(unittest.TestCase):
    """Risk #1: old rows can affect KPI after ignore-set change."""

    def test_gap_started_before_alert_removed_from_ignored_reasons(self) -> None:
        src = VALIDATOR_PATH.read_text(encoding="utf-8")
        self.assertIn('IGNORED_REASONS = {"missing_player_id"}', src)
        self.assertNotIn("gap_started_before_alert", src.split("IGNORED_REASONS =")[1].splitlines()[0])

    def test_kpi_uses_ignored_reasons_filter(self) -> None:
        src = VALIDATOR_PATH.read_text(encoding="utf-8")
        self.assertIn('kpi_df = final_df[~final_df["reason"].isin(IGNORED_REASONS)]', src)


class TestRisk2Task6IntegrationContractStrength(unittest.TestCase):
    """Risk #2: current Task 6 integration assertion is only a weak guard."""

    def test_1022_case_checks_not_equal_reason_only(self) -> None:
        src = INTEGRATION_TEST_PATH.read_text(encoding="utf-8")
        self.assertIn("test_1022_label_positive_validator_no_gap_started_before_alert_fp", src)
        self.assertIn("self.assertNotEqual(", src)
        self.assertNotIn('self.assertTrue(res["result"]', src)
        self.assertNotIn('self.assertFalse(res["result"]', src)


class TestRisk3BaseStartBoundaryContract(unittest.TestCase):
    """Risk #3: boundary guard is split across caller/callee."""

    def test_validate_alert_row_still_passes_last_bet_before_as_base_start(self) -> None:
        src = VALIDATOR_PATH.read_text(encoding="utf-8")
        self.assertIn("base_start = last_bet_before or bet_ts", src)
        self.assertIn(
            "is_true, gap_start, gap_minutes = find_gap_within_window(bet_ts, bet_times, base_start=base_start)",
            src,
        )


class TestRisk4DocCurrentVsHistoricalMarkers(unittest.TestCase):
    """Risk #4: document still mostly historical and can be misread."""

    def test_doc_has_update_banner_but_no_explicit_current_behavior_section(self) -> None:
        src = DOC_PATH.read_text(encoding="utf-8")
        self.assertIn("Update (2026-03-24, Task 6)", src)
        self.assertNotIn("Current behavior", src)
        self.assertNotIn("現行行為", src)


class TestRisk5NoPerfGuardrailThreshold(unittest.TestCase):
    """Risk #5: perf summary exists but no explicit regression threshold guard."""

    def test_no_validator_perf_threshold_config_in_validator_module(self) -> None:
        src = VALIDATOR_PATH.read_text(encoding="utf-8")
        self.assertIn("_emit_validator_perf_summary", src)
        self.assertNotIn("VALIDATOR_PERF_P95", src)
        self.assertNotIn("PERF_THRESHOLD", src)


if __name__ == "__main__":
    unittest.main()

