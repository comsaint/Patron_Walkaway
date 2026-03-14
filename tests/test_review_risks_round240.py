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
_SCORER_PATH = _REPO_ROOT / "trainer" / "serving" / "scorer.py"
_BACKTESTER_SRC = _BACKTESTER_PATH.read_text(encoding="utf-8")
_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")


class TestR1200BacktesterUnratedOnlyRemoved(unittest.TestCase):
    """R1200 (updated): backtester --unrated-only was removed in v10 DEC-021 (single rated model)."""

    def test_backtest_signature_should_not_include_unrated_only(self):
        sig = inspect.signature(backtester_mod.backtest)
        self.assertNotIn(
            "unrated_only",
            sig.parameters,
            "v10 backtest() should not accept unrated_only; single rated model only.",
        )

    def test_backtester_cli_should_not_expose_unrated_only_flag(self):
        self.assertNotIn(
            "--unrated-only",
            _BACKTESTER_SRC,
            "v10 backtester CLI should not expose --unrated-only",
        )


class TestR1201BacktesterEmptySubsetGuard(unittest.TestCase):
    """R1201: per-track metric helpers should handle empty subset robustly."""

    def test_compute_micro_metrics_empty_df_should_return_empty_dict(self):
        # R224: empty df returns trainer-aligned flat keys with zeros (not {}) to avoid KeyError.
        df = pd.DataFrame(columns=["score", "label", "is_rated"])
        out = backtester_mod.compute_micro_metrics(
            df,
            threshold=0.5,
            window_hours=1.0,
        )
        self.assertIsInstance(out, dict)
        self.assertEqual(out["test_ap"], 0.0)
        self.assertEqual(out["threshold"], 0.5)
        self.assertEqual(out["test_samples"], 0)
        self.assertEqual(out["test_positives"], 0)
        self.assertEqual(out["alerts"], 0)
        self.assertEqual(out["alerts_per_hour"], 0.0)
        self.assertIn("test_precision", out)
        self.assertIn("test_recall", out)
        self.assertIn("test_f1", out)
        self.assertIn("test_fbeta_05", out)
        self.assertIn("test_random_ap", out)

    def test_compute_macro_metrics_empty_df_should_return_empty_dict(self):
        df = pd.DataFrame(columns=["canonical_id", "gaming_day", "score", "label", "is_rated"])
        out = backtester_mod.compute_macro_by_gaming_day_metrics(
            df,
            threshold=0.5,
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


class TestR1205ScorerUnratedOnlyRemoved(unittest.TestCase):
    """R1205 (updated): scorer --unrated-only was removed in v10 DEC-021 (single rated model).

    Profile join is now always performed for rated patrons; no conditional bypass needed.
    """

    def test_score_once_signature_should_not_include_unrated_only(self):
        sig = inspect.signature(scorer_mod.score_once)
        self.assertNotIn(
            "unrated_only",
            sig.parameters,
            "v10 score_once() should not accept unrated_only parameter.",
        )


if __name__ == "__main__":
    unittest.main()
