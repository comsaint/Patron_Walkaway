"""tests/test_backtester_review_risks_round18.py
=================================================
Guardrail tests for Round 18 review findings (R28-R30).

Constraint in this round: tests-only (no production code changes).  Therefore
known issues are captured with `unittest.expectedFailure` and should be
converted to normal passing assertions once implementation fixes are applied.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_BACKTESTER_PATH = _REPO_ROOT / "trainer" / "backtester.py"

_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_BACKTESTER_SRC = _BACKTESTER_PATH.read_text(encoding="utf-8")

_TRAINER_TREE = ast.parse(_TRAINER_SRC)
_BACKTESTER_TREE = ast.parse(_BACKTESTER_SRC)


def _get_func_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


class TestBacktesterReviewRisksRound18(unittest.TestCase):
    def test_r28_load_local_parquet_strips_timezone_in_filters(self):
        """R28: trainer.load_local_parquet should strip timezone in parquet filters."""
        func = _get_func_node(_TRAINER_TREE, "load_local_parquet")
        src = ast.get_source_segment(_TRAINER_SRC, func) or ""

        # Structural guardrail:
        # parquet filters should convert timezone-aware bounds to tz-naive,
        # e.g. pd.Timestamp(...).tz_localize(None)
        self.assertIn(
            "tz_localize(None)",
            src,
            msg="load_local_parquet filters should strip timezone (tz_localize(None)).",
        )

    def test_r29_backtester_label_filtering_uses_tz_naive_boundaries(self):
        """R29: backtest label filtering should normalize window_start/window_end timezone."""
        func = _get_func_node(_BACKTESTER_TREE, "backtest")
        src = ast.get_source_segment(_BACKTESTER_SRC, func) or ""

        # Guardrail: before comparing with labeled["payout_complete_dtm"],
        # code should normalize boundary datetimes via replace(tzinfo=None)
        # or equivalent explicit tz stripping.
        has_tz_strip = (
            "window_start.replace(tzinfo=None)" in src
            or "window_end.replace(tzinfo=None)" in src
            or "tz_localize(None)" in src
        )
        self.assertTrue(
            has_tz_strip,
            msg="backtest should strip timezone from window boundaries before label filtering.",
        )

    def test_r30_backtest_saves_predictions_as_parquet_not_csv(self):
        """R30: full prediction output should be parquet (not csv) for large windows."""
        func = _get_func_node(_BACKTESTER_TREE, "backtest")
        src = ast.get_source_segment(_BACKTESTER_SRC, func) or ""

        # Guardrail:
        # - prediction file should be backtest_predictions.parquet
        # - labeled output should use to_parquet for full dataset
        self.assertIn(
            "backtest_predictions.parquet",
            src,
            msg="backtest should write full predictions as parquet.",
        )
        self.assertIn(
            ".to_parquet(",
            src,
            msg="backtest should call to_parquet for full prediction output.",
        )


if __name__ == "__main__":
    unittest.main()

