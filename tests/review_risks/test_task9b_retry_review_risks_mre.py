"""Task 9B reviewer mitigations -> minimal reproducible contract tests (tests-only).

Documents implemented behavior: per-player query isolation, retry window cap,
round-robin selection when > max alerts, DB-first existing_results merge.
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


def _func_block(src: str, func_name: str) -> str:
    pattern = rf"def {re.escape(func_name)}\("
    m = re.search(pattern, src)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"\n\ndef [A-Za-z_]\w*\(", src[start + 1 :])
    end = (start + 1 + nxt.start()) if nxt else len(src)
    return src[start:end]


class TestTask9BMitigation1RetryQueryFailureIsolation(unittest.TestCase):
    """Mitigation #1: per-player try/except around retry query_df — failure counts, cycle continues."""

    def test_retry_helper_wraps_query_df_in_try_except(self) -> None:
        text = _validator_text()
        block = _func_block(text, "_fetch_bets_for_no_bet_rows")
        self.assertIn("client.query_df(query, parameters=params)", block)
        self.assertIn("failed_queries", block)
        query_pos = block.find("client.query_df(query, parameters=params)")
        self.assertNotEqual(query_pos, -1)
        around_query = block[max(0, query_pos - 120) : min(len(block), query_pos + 200)]
        self.assertIn("try:", around_query)
        self.assertIn("except", around_query)


class TestTask9BMitigation2RetryWindowHardCap(unittest.TestCase):
    """Mitigation #2: config cap + clamp on per-row and aggregated retry windows."""

    def test_config_has_retry_window_cap_constant(self) -> None:
        cfg = _config_text()
        self.assertIn("VALIDATOR_NO_BET_RETRY_MAX_ALERTS", cfg)
        self.assertIn("VALIDATOR_NO_BET_RETRY_MAX_WINDOW_MINUTES", cfg)

    def test_retry_helper_has_window_clamp_logic(self) -> None:
        block = _func_block(_validator_text(), "_fetch_bets_for_no_bet_rows")
        self.assertIn("retry_start = bt - timedelta", block)
        self.assertIn("retry_end = bt + timedelta", block)
        self.assertIn("clamp", block.lower())


class TestTask9BMitigation3RetryFairnessRoundRobin(unittest.TestCase):
    """Mitigation #3: when many no-bet rows, rotate starting index each cycle (not fixed first-N)."""

    def test_retry_selection_uses_rotated_slice(self) -> None:
        text = _validator_text()
        self.assertIn("retry_slice = (no_bet_pending_rows[rot_start:]", text)
        self.assertIn("_NO_BET_RETRY_ROT_OFFSET", text)


class TestTask9BRisk4RetryTimezoneAssumptionContract(unittest.TestCase):
    """Risk #4: retry path assumes naive payout_complete_dtm is HK local."""

    def test_retry_naive_timestamp_localized_as_hk(self) -> None:
        block = _func_block(_validator_text(), "_fetch_bets_for_no_bet_rows")
        self.assertIn("ts.dt.tz_localize(HK_TZ", block)
        self.assertNotIn("UTC_TZ", block)


class TestTask9BMitigation5ExistingResultsCacheDbFirstMerge(unittest.TestCase):
    """Mitigation #5: DB rows win; in-process cache only fills keys missing from DB."""

    def test_validate_once_db_first_then_fill_missing_cache_keys(self) -> None:
        text = _validator_text()
        self.assertIn("warm_cache=existing_results_cache", text)
        self.assertIn("if _k not in existing_results:", text)
        self.assertNotIn("existing_results.update(existing_results_cache)", text)


if __name__ == "__main__":
    unittest.main()
