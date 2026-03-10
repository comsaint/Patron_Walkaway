"""Round 404 Code Review — 風險點轉成最小可重現測試（tests only，不改 production）。

STATUS.md Round 404 Code Review 各項建議新增測試之實作。僅新增測試，不修改 production code。
Reference: PLAN.md § Round 222 Review production 補強、DECISION_LOG DEC-011 / DEC-022.
"""

from __future__ import annotations

import datetime as dt
import inspect
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.backtester as backtester_mod
import trainer.features as features_mod
import trainer.trainer as trainer_mod


# ---------------------------------------------------------------------------
# R404 Review #1 — Trainer 仍使用 else None 當 canonical_map 為空
# ---------------------------------------------------------------------------
class TestR404_1_TrainerRatedCidsEmptyMap(unittest.TestCase):
    """R404 Review #1: When canonical_map is empty, trainer should pass canonical_ids=[] to load_player_profile."""

    def test_run_pipeline_uses_else_list_when_canonical_map_empty(self):
        """Contract: run_pipeline _rated_cids block uses else [] when canonical_map empty (train-serve parity with backtester)."""
        source = inspect.getsource(trainer_mod.run_pipeline)
        idx = source.find("_rated_cids")
        load_idx = source.find("load_player_profile", idx)
        self.assertGreater(load_idx, idx, "run_pipeline must contain load_player_profile after _rated_cids")
        block = source[idx:load_idx]
        self.assertIn(
            "else []",
            block,
            "R404 #1: trainer _rated_cids should use else [] when canonical_map empty (train-serve parity with backtester).",
        )


# ---------------------------------------------------------------------------
# R404 Review #2 — backtester _llm_cand_ids 候選非 dict 時可能 AttributeError
# ---------------------------------------------------------------------------
class TestR404_2_BacktesterCandidatesNonDictElement(unittest.TestCase):
    """R404 Review #2: When candidates list contains non-dict elements, backtest should not crash."""

    def test_backtest_does_not_crash_when_candidates_has_non_dict_elements(self):
        """Behavioral: track_llm.candidates = [{\"feature_id\": \"x\"}, \"invalid\", {\"feature_id\": \"y\"}] → backtest completes without AttributeError."""
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
        spec_with_invalid = {"track_llm": {"candidates": [{"feature_id": "x"}, "invalid", {"feature_id": "y"}]}}
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["100"]})

        def _minimal_compute_labels(bets_df, window_end, extended_end):
            out = bets_df.copy()
            out["label"] = 0
            out["censored"] = False
            return out

        def _minimal_join_profile(labeled, profile_df):
            return labeled.copy()

        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=canonical_map),
            patch.object(backtester_mod, "add_track_b_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value=spec_with_invalid),
            patch.object(
                backtester_mod,
                "compute_track_llm_features",
                side_effect=lambda bets, feature_spec, cutoff_time: pd.DataFrame({"bet_id": [1], "x": [0.1], "y": [0.2]}),
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
        self.assertIsInstance(result, dict)
        self.assertNotIn("error", result)


class _MockPredictProba:
    def predict_proba(self, X):
        return np.array([[0.2, 0.8]] * len(X))


# ---------------------------------------------------------------------------
# R404 Review #3 — load_player_profile canonical_ids=[] early return
# ---------------------------------------------------------------------------
class TestR404_3_LoadPlayerProfileEmptyCanonicalIds(unittest.TestCase):
    """R404 Review #3: load_player_profile(canonical_ids=[]) returns None and does not read Parquet."""

    def test_load_player_profile_returns_none_when_canonical_ids_empty(self):
        """canonical_ids=[] → return None (R222 #2 early return)."""
        from trainer.trainer import load_player_profile

        result = load_player_profile(
            dt.datetime(2026, 1, 1, 0, 0, 0),
            dt.datetime(2026, 1, 2, 0, 0, 0),
            canonical_ids=[],
        )
        self.assertIsNone(result, "R404 #3: load_player_profile must return None when canonical_ids=[].")

    def test_load_player_profile_does_not_read_parquet_when_canonical_ids_empty(self):
        """canonical_ids=[] → pd.read_parquet not called (early return before Primary path)."""
        with patch("trainer.trainer.pd.read_parquet") as mock_read:
            trainer_mod.load_player_profile(
                dt.datetime(2026, 1, 1, 0, 0, 0),
                dt.datetime(2026, 1, 2, 0, 0, 0),
                canonical_ids=[],
            )
            mock_read.assert_not_called()


# ---------------------------------------------------------------------------
# R404 Review #4 — features.get_candidate_feature_ids candidates 非 list
# ---------------------------------------------------------------------------
class TestR404_4_GetCandidateFeatureIdsCandidatesNonList(unittest.TestCase):
    """R404 Review #4: get_candidate_feature_ids when candidates is dict should not raise."""

    def test_get_candidate_feature_ids_handles_dict_candidates_gracefully(self):
        """When track_llm.candidates is dict (not list), get_candidate_feature_ids returns [] without AttributeError."""
        spec = {"track_llm": {"candidates": {"k1": 1, "k2": 2}}}
        result = features_mod.get_candidate_feature_ids(spec, "track_llm")
        self.assertIsInstance(result, list)
        # Dict iteration yields keys; keys have no .get(); production would raise. Desired: return [].
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
