"""Guardrail tests for Round 73 review risks (R1200-R1205).

Production code was fixed in Round 75; @expectedFailure decorators removed so
these tests now run as standard assertions.
"""

from __future__ import annotations

import inspect
import pathlib
import unittest

import pandas as pd

import trainer.backtester as backtester_mod
import trainer.identity as identity_mod
import trainer.scorer as scorer_mod


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BACKTESTER_PATH = _REPO_ROOT / "trainer" / "backtester.py"
_SCORER_PATH = _REPO_ROOT / "trainer" / "scorer.py"
_BACKTESTER_SRC = _BACKTESTER_PATH.read_text(encoding="utf-8")
_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")


class TestR1200BacktesterUnratedOnlyWiring(unittest.TestCase):
    """R1200: backtester should implement --unrated-only end-to-end."""

    def test_backtest_signature_should_include_unrated_only(self):
        sig = inspect.signature(backtester_mod.backtest)
        self.assertIn(
            "unrated_only",
            sig.parameters,
            "backtest() should accept unrated_only: bool = False",
        )

    def test_backtester_cli_should_expose_unrated_only_flag(self):
        self.assertIn(
            "--unrated-only",
            _BACKTESTER_SRC,
            "backtester CLI should expose --unrated-only",
        )


class TestR1201BacktesterEmptySubsetGuard(unittest.TestCase):
    """R1201: per-track metric helpers should handle empty subset robustly."""

    def test_compute_micro_metrics_empty_df_should_return_empty_dict(self):
        df = pd.DataFrame(columns=["score", "label", "is_rated"])
        out = backtester_mod.compute_micro_metrics(
            df,
            rated_threshold=0.5,
            nonrated_threshold=0.5,
            window_hours=1.0,
        )
        self.assertEqual(
            out,
            {},
            "Expected explicit empty-df guard in compute_micro_metrics.",
        )

    def test_compute_macro_metrics_empty_df_should_return_empty_dict(self):
        df = pd.DataFrame(columns=["canonical_id", "gaming_day", "score", "label", "is_rated"])
        out = backtester_mod.compute_macro_by_gaming_day_metrics(
            df,
            rated_threshold=0.5,
            nonrated_threshold=0.5,
        )
        self.assertEqual(
            out,
            {},
            "Expected explicit empty-df guard in compute_macro_by_gaming_day_metrics.",
        )


class TestR1203IdentityBidirectionalTzGuard(unittest.TestCase):
    """R1203: identity cutoff/session timezone alignment should be bidirectional."""

    def test_identity_aware_cutoff_naive_sessions_should_not_raise(self):
        # Minimal required columns for build_canonical_mapping_from_df (incl. turnover for FND-04)
        sessions_df = pd.DataFrame(
            {
                "session_id": [1],
                "lud_dtm": [pd.Timestamp("2026-03-05 10:00:00")],  # tz-naive
                "__etl_insert_Dtm": [pd.Timestamp("2026-03-05 10:05:00")],
                "player_id": [123],
                "casino_player_id": ["ABC123"],
                "session_end_dtm": [pd.Timestamp("2026-03-05 10:30:00")],  # tz-naive
                "is_manual": [0],
                "is_deleted": [0],
                "is_canceled": [0],
                "num_games_with_wager": [2],
                "turnover": [50.0],
            }
        )
        cutoff_aware = pd.Timestamp("2026-03-05 12:00:00", tz="Asia/Hong_Kong").to_pydatetime()
        out = identity_mod.build_canonical_mapping_from_df(sessions_df, cutoff_dtm=cutoff_aware)
        self.assertIsInstance(out, pd.DataFrame)


class TestR1204BacktesterMetricCallOverhead(unittest.TestCase):
    """R1204: avoid excessive duplicate metric helper calls in backtester."""

    def test_backtester_should_limit_metric_helper_calls(self):
        micro_calls = _BACKTESTER_SRC.count("compute_micro_metrics(")
        macro_calls = _BACKTESTER_SRC.count("compute_macro_by_gaming_day_metrics(")
        # Target contract: combined + per-track should be consolidated (<=4 each).
        self.assertLessEqual(micro_calls, 4, f"compute_micro_metrics calls={micro_calls} > 4")
        self.assertLessEqual(
            macro_calls,
            4,
            f"compute_macro_by_gaming_day_metrics calls={macro_calls} > 4",
        )


class TestR1205ScorerUnratedOnlySkipsProfileJoin(unittest.TestCase):
    """R1205: scorer should skip profile join path when unrated_only=True."""

    def test_score_once_profile_join_condition_should_include_not_unrated_only(self):
        src = inspect.getsource(scorer_mod.score_once)
        self.assertIn(
            "not unrated_only",
            src,
            "score_once should guard profile join with 'not unrated_only'.",
        )


if __name__ == "__main__":
    unittest.main()
