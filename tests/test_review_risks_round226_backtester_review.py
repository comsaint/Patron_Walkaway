"""Round 226 Code Review — Backtester risk points as minimal reproducible tests.

STATUS.md Round 226 Code Review: convert reviewer risk points to tests only.
No production code changes. Reference: PLAN § Backtester 評估輸出格式對齊 trainer.
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.backtester as backtester_mod


def _minimal_compute_labels(bets_df, window_end, extended_end):
    out = bets_df.copy()
    out["label"] = 0
    out["censored"] = False
    return out


def _minimal_join_profile(labeled, profile_df):
    return labeled.copy()


class _MockPredictProba:
    def predict_proba(self, X):
        return np.array([[0.2, 0.8]] * len(X))


class TestR226Review1BacktestReturnModelDefaultFlat(unittest.TestCase):
    """Round 226 Review #1: backtest() return value must have model_default flat (no micro nest)."""

    def test_backtest_return_model_default_is_flat_no_micro(self):
        """Full path: backtest() returns results with model_default flat; no 'micro' key."""
        bets = pd.DataFrame({
            "bet_id": [1],
            "session_id": [10],
            "player_id": [100],
            "table_id": [7],
            "payout_complete_dtm": [pd.Timestamp("2026-02-06 00:01:00")],
            "wager": [100.0],
            "status": ["LOSE"],
        })
        sessions = pd.DataFrame({"session_id": [10], "player_id": [100]})
        window_start = dt.datetime(2026, 2, 6, 0, 0)
        window_end = dt.datetime(2026, 2, 13, 0, 0)
        artifacts = {
            "rated": {
                "model": _MockPredictProba(),
                "features": ["feat_a", "feat_b"],
                "threshold": 0.5,
            },
            "feature_list_meta": [],
        }

        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=pd.DataFrame()),
            patch.object(backtester_mod, "add_track_b_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(
                backtester_mod,
                "compute_track_llm_features",
                side_effect=RuntimeError("mock Track LLM failure"),
            ),
            patch.object(backtester_mod, "compute_labels", side_effect=_minimal_compute_labels),
            patch.object(backtester_mod, "load_player_profile", return_value=None),
            patch.object(backtester_mod, "join_player_profile", side_effect=_minimal_join_profile),
        ):
            result = backtester_mod.backtest(
                bets_raw=bets,
                sessions_raw=sessions,
                artifacts=artifacts,
                window_start=window_start,
                window_end=window_end,
                run_optuna=False,
            )

        self.assertNotIn("error", result, "backtest must complete without error key.")
        self.assertIn("model_default", result, "backtest must return model_default.")
        section = result["model_default"]
        self.assertNotIn("micro", section, "Round 226: model_default must be flat (no micro nest).")
        self.assertIn("test_ap", section, "model_default must contain trainer-style key test_ap.")
        self.assertIsInstance(
            section["test_ap"],
            (int, float),
            "test_ap must be numeric.",
        )


if __name__ == "__main__":
    unittest.main()
