"""Minimal reproducible tests for Round 78 review risks (R1402-R1405).

Tests-only round: do NOT modify production code here.
Unfixed production risks are encoded as expected failures to keep them visible.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import re
import sys
import unittest


def _import_module(name: str):
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module(name)


trainer_mod = _import_module("trainer.trainer")
backtester_mod = _import_module("trainer.backtester")


class TestR1402TrainerSessionFnd01Coverage(unittest.TestCase):
    """R1402: trainer load_clickhouse_data should dedup t_session via FND-01 CTE."""

    def test_trainer_session_query_uses_fnd01_row_number_cte(self):
        src = inspect.getsource(trainer_mod.load_clickhouse_data)
        self.assertIn(
            "ROW_NUMBER() OVER",
            src,
            "trainer.load_clickhouse_data session_query should use FND-01 CTE dedup.",
        )


class TestR1403TrainerSessionGuardrailsCoverage(unittest.TestCase):
    """R1403: test_dq_guardrails should cover trainer session-query guardrails."""

    def test_dq_guardrails_has_trainer_session_cte_check(self):
        dq_test_path = pathlib.Path(__file__).resolve().parents[1] / "tests" / "test_dq_guardrails.py"
        src = dq_test_path.read_text(encoding="utf-8")
        m = re.search(
            r"class TestDQGuardrailsTrainer\(unittest\.TestCase\):([\s\S]*?)(?:\nclass |\nif __name__ ==)",
            src,
        )
        self.assertIsNotNone(m, "TestDQGuardrailsTrainer class block not found")
        trainer_block = m.group(1)
        self.assertIn(
            "test_session_query_uses_fnd01_row_number_cte",
            trainer_block,
            "TestDQGuardrailsTrainer should include session CTE coverage.",
        )

    def test_dq_guardrails_has_trainer_session_no_final_check(self):
        dq_test_path = pathlib.Path(__file__).resolve().parents[1] / "tests" / "test_dq_guardrails.py"
        src = dq_test_path.read_text(encoding="utf-8")
        m = re.search(
            r"class TestDQGuardrailsTrainer\(unittest\.TestCase\):([\s\S]*?)(?:\nclass |\nif __name__ ==)",
            src,
        )
        self.assertIsNotNone(m, "TestDQGuardrailsTrainer class block not found")
        trainer_block = m.group(1)
        self.assertRegex(
            trainer_block,
            r"test_session_query_no_final",
            "TestDQGuardrailsTrainer should include session no-FINAL guard.",
        )


class TestR1404FragileQueryExtractor(unittest.TestCase):
    """R1404: demonstrate the current marker-based extractor is refactor-sensitive."""

    @staticmethod
    def _extract_with_current_logic(func_src: str) -> str:
        open_marker = 'bets_query = f"""'
        close_marker = '"""'
        idx_open = func_src.find(open_marker)
        if idx_open < 0:
            raise AssertionError('bets_query = f""" not found')
        idx_close = func_src.find(close_marker, idx_open + len(open_marker))
        if idx_close <= idx_open:
            raise AssertionError('bets_query closing """ not found')
        return func_src[idx_open + len(open_marker) : idx_close]

    def test_extractor_should_handle_non_f_string_variant(self):
        func_src = '''
def load_clickhouse_data():
    bets_query = """
        SELECT * FROM x
    """
'''
        # The old marker-based logic fails because it looks for 'bets_query = f"""'
        # but the string is 'bets_query = """' (non-f-string)
        with self.assertRaises(AssertionError):
            self._extract_with_current_logic(func_src)

    def test_regex_extractor_handles_f_and_non_f(self):
        for func_src in (
            'bets_query = f"""SELECT 1"""',
            'bets_query = """SELECT 2"""',
        ):
            m = re.search(r'bets_query\s*=\s*f?"""(.*?)"""', func_src, re.DOTALL)
            self.assertIsNotNone(m)


class TestR1405BacktesterSingleThresholdAlignment(unittest.TestCase):
    """R1405: Step-6 target is single-threshold rated-only; source is still 2D."""

    def test_backtester_optuna_search_no_nonrated_threshold(self):
        src = inspect.getsource(backtester_mod.run_optuna_threshold_search)
        self.assertNotIn(
            "nonrated_threshold",
            src,
            "Backtester threshold search should be single-dimension in v10 Step 6.",
        )


if __name__ == "__main__":
    unittest.main()
