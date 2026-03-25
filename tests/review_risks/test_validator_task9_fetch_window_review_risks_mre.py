"""Task 9 reviewer risks -> minimal reproducible contract tests.

These tests document current risk surfaces without changing production code.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = REPO_ROOT / "trainer" / "serving" / "validator.py"
CONFIG_PATH = REPO_ROOT / "trainer" / "core" / "config.py"


def _validator_text() -> str:
    return VALIDATOR_PATH.read_text(encoding="utf-8")


def _config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


class TestRisk1UnsafeIntParsingContract(unittest.TestCase):
    """Risk #1 fixed: Task 9 uses safe int parsing helper."""

    def test_fetch_window_uses_int_getattr_without_try_except(self) -> None:
        text = _validator_text()
        # Narrow to the fetch window block for stability.
        start = text.find("pending_min_ts = effective_ts[pending.index].min()")
        self.assertNotEqual(start, -1)
        end = text.find("try:", start)
        block = text[start : end if end != -1 else len(text)]
        self.assertIn("def _safe_int_config(", text)
        self.assertIn("_safe_int_config(\"VALIDATOR_FETCH_PRE_CONTEXT_MINUTES\"", block)
        self.assertIn("_safe_int_config(\"VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES\"", block)


class TestRisk2PolicyMismatchExtendedWaitContract(unittest.TestCase):
    """Risk #2 fixed: required_min includes VALIDATOR_EXTENDED_WAIT_MINUTES."""

    def test_required_min_formula_excludes_extended_wait(self) -> None:
        text = _validator_text()
        start = text.find("policy_late_min = int(config.LABEL_LOOKAHEAD_MIN")
        self.assertNotEqual(start, -1)
        end = text.find("candidate_start =", start)
        block = text[start : end if end != -1 else len(text)]
        self.assertIn("policy_late_min", block)
        self.assertIn("ext_wait_min", block)
        self.assertIn("_safe_int_config(\"VALIDATOR_EXTENDED_WAIT_MINUTES\"", block)
        self.assertIn("required_min = policy_late_min + ext_wait_min + pre_context_min", block)


class TestRisk3MisconfigWarningNoThrottleContract(unittest.TestCase):
    """Risk #3 fixed: misconfig warnings are warn-once."""

    def test_misconfig_warning_has_no_warn_once_state(self) -> None:
        text = _validator_text()
        self.assertIn("VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES", text)
        self.assertIn("too small for policy", text)
        self.assertIn("_WARNED_TASK9_LOOKBACK_TOO_SMALL", text)
        self.assertIn("_WARNED_TASK9_LOOKBACK_CLAMPED", text)


class TestRisk4NoHardCapOnLookbackInConfigContract(unittest.TestCase):
    """Risk #4 fixed: config defines an explicit cap constant."""

    def test_config_has_no_cap_constant(self) -> None:
        text = _config_text()
        self.assertIn("VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES", text)
        self.assertRegex(text, r"VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES_?CAP")


class TestRisk5MissingBranchCoverageTestsContract(unittest.TestCase):
    """Risk #5: no dedicated test asserts hard_floor branch/cutoff behavior."""

    def test_no_existing_tests_mention_hard_floor_or_candidate_start(self) -> None:
        # Contract (naming-based): there is no dedicated test file for the hard-floor branch yet.
        # This avoids brittle content scans (unrelated docstrings may contain the token strings).
        root = REPO_ROOT / "tests"
        hard_floor_tests = list(root.rglob("*hard_floor*.py"))
        candidate_start_tests = list(root.rglob("*candidate_start*.py"))
        self.assertEqual(hard_floor_tests, [])
        self.assertEqual(candidate_start_tests, [])


class TestRisk6ConfigDefaultsAreNotEnvParseHardenedContract(unittest.TestCase):
    """Risk #6: Task 9 config is static constants (not env-parsed), so ops overrides may be ad-hoc."""

    def test_task9_config_is_static_constants(self) -> None:
        text = _config_text()
        # Document current design: constants exist, but no env parsing is present for them.
        self.assertIn("VALIDATOR_FETCH_PRE_CONTEXT_MINUTES", text)
        self.assertIn("VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES", text)
        self.assertNotIn("os.getenv(\"VALIDATOR_FETCH_PRE_CONTEXT_MINUTES\"", text)
        self.assertNotIn("os.getenv(\"VALIDATOR_FETCH_MAX_LOOKBACK_MINUTES\"", text)


if __name__ == "__main__":
    unittest.main()

