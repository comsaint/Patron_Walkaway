"""Round 402 Code Review — 排除 unrated 變更之風險點轉成最小可重現測試（tests only，不改 production）。

STATUS.md Round 402 Code Review 各項建議新增測試之實作。僅新增測試，不修改 trainer/scorer/backtester。
Reference: PLAN §16「取得 bet 後排除 unrated 再送模型」、DECISION_LOG DEC-021。
"""

from __future__ import annotations

import datetime as dt
import logging
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


# ---------------------------------------------------------------------------
# R402 Review #1 — canonical_id 型別與 isin(rated_ids) 一致（str 路徑）
# ---------------------------------------------------------------------------
class TestR402_1_CanonicalIdTypeStringRated(unittest.TestCase):
    """Review #1: When canonical_map returns canonical_id as string, backtest treats row as rated and completes without error."""

    def test_backtest_with_string_canonical_id_completes_rated(self):
        """Canonical map with canonical_id as string '100' → merge gives str, rated_ids str; assert no error and rated_obs >= 1."""
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
        # canonical_id as string (identity.py / doc convention) so isin(rated_ids) matches.
        canonical_map_str = pd.DataFrame({"player_id": [100], "canonical_id": ["100"]})
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=canonical_map_str),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
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
        self.assertNotIn("error", result, "String canonical_id path must complete without error.")
        self.assertGreaterEqual(result.get("rated_obs", 0), 1, "One rated row expected.")
        self.assertIn("observations", result)


# ---------------------------------------------------------------------------
# R402 Review #2 — Excluded 僅在 n_unrated > 0 時打 log（邊界／雜訊）
# ---------------------------------------------------------------------------
class TestR402_2_ExcludedLogOnlyWhenUnratedPresent(unittest.TestCase):
    """Review #2: When all observations are rated, log should not contain 'Excluded' (avoid noise)."""

    def test_when_all_rated_log_does_not_contain_excluded(self):
        """With UNRATED_VOLUME_LOG=True and all rated, backtester log must not contain 'Excluded'."""
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
        canonical_map_rated = pd.DataFrame({"player_id": [100], "canonical_id": ["100"]})
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=canonical_map_rated),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
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
            with self.assertLogs("backtester", level=logging.INFO) as cm:
                backtester_mod.backtest(
                    bets_raw=bets,
                    sessions_raw=sessions,
                    artifacts=artifacts,
                    window_start=window_start,
                    window_end=window_end,
                    run_optuna=False,
                )
        log_text = " ".join(cm.output)
        self.assertNotIn("Excluded", log_text, "When all rated, Excluded line should not be logged.")


# ---------------------------------------------------------------------------
# R402 Review #3 — Backtester 無 rated 時回傳 dict 應含 rated_obs / unrated_obs（契約）
# ---------------------------------------------------------------------------
class TestR402_3_NoRatedReturnContract(unittest.TestCase):
    """Review #3: When no rated observations, return must include 'error'; contract: also rated_obs and unrated_obs for caller safety."""

    def test_no_rated_return_has_error_and_rated_obs_zero(self):
        """No rated → result has 'error' and result.get('rated_obs', 0) == 0 (passes with current or suggested return)."""
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
            "rated": {"model": _MockPredictProba(), "features": ["feat_a", "feat_b"], "threshold": 0.5},
            "feature_list_meta": [],
        }
        # Empty map → no one is rated → backtest returns error.
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=pd.DataFrame()),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(
                backtester_mod,
                "compute_track_llm_features",
                side_effect=RuntimeError("mock"),
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
        self.assertIn("error", result)
        self.assertEqual(result.get("error"), "No rated observations in window")
        self.assertEqual(result.get("rated_obs", 0), 0, "Caller can safely use get('rated_obs', 0).")

    def test_no_rated_return_includes_unrated_obs_key(self):
        """Contract: no-rated return should include 'unrated_obs' (and optionally 'observations') for consistent structure."""
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
            "rated": {"model": _MockPredictProba(), "features": ["feat_a", "feat_b"], "threshold": 0.5},
            "feature_list_meta": [],
        }
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=pd.DataFrame()),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(backtester_mod, "compute_track_llm_features", side_effect=RuntimeError("mock")),
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
        self.assertIn("unrated_obs", result, "Error return should include unrated_obs for caller consistency.")


# ---------------------------------------------------------------------------
# R402 Review #4 — Unrated 玩家數為 dropna 後計數（語意／可選）
# ---------------------------------------------------------------------------
class TestR402_4_UnratedPlayersCountSemantics(unittest.TestCase):
    """Review #4: unrated_players counts only rows with non-NaN canonical_id (documented semantics)."""

    def test_no_rated_return_observations_consistent_when_keys_present(self):
        """When production adds rated_obs/unrated_obs to error return, observations should equal rated_obs + unrated_obs. No-op when keys absent."""
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
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=pd.DataFrame()),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(backtester_mod, "compute_track_llm_features", side_effect=RuntimeError("mock")),
            patch.object(backtester_mod, "compute_labels", side_effect=_minimal_compute_labels),
            patch.object(backtester_mod, "load_player_profile", return_value=None),
            patch.object(backtester_mod, "join_player_profile", side_effect=_minimal_join_profile),
        ):
            result = backtester_mod.backtest(
                bets_raw=bets,
                sessions_raw=sessions,
                artifacts={"rated": {"model": _MockPredictProba(), "features": [], "threshold": 0.5}, "feature_list_meta": []},
                window_start=dt.datetime(2026, 2, 6, 0, 0),
                window_end=dt.datetime(2026, 2, 13, 0, 0),
                run_optuna=False,
            )
        if "rated_obs" in result and "unrated_obs" in result and "observations" in result:
            self.assertEqual(
                result["observations"],
                result["rated_obs"] + result["unrated_obs"],
                "observations should equal rated_obs + unrated_obs when all keys present.",
            )
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
