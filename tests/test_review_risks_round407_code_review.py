"""Minimal reproducible tests for Round 407 Code Review — Round 406 變更風險點.

STATUS.md Round 407 Review 所列風險轉成契約／行為測試。Tests-only: 不修改 production。
"""

from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

try:
    import trainer.backtester as backtester_mod
except ImportError:
    import backtester as backtester_mod  # type: ignore[no-redef]


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
# R407 Review #1 — 錯誤回傳應含 track_llm_degraded（可觀測性）
# ---------------------------------------------------------------------------

def _compute_labels_empty_after_filter(bets_df, window_end, extended_end):
    """Return labeled so that after (payout_complete_dtm in [ws, we)) filter we get empty."""
    out = bets_df.copy()
    out["label"] = 0
    out["censored"] = False
    # Put payout_complete_dtm after window_end so filter yields no rows.
    out["payout_complete_dtm"] = pd.Timestamp(window_end) + pd.Timedelta(days=1)
    return out


class TestR407ErrorReturnIncludesTrackLlmDegraded(unittest.TestCase):
    """R407 #1: When Track LLM failed and backtest returns error (e.g. No rows after label filtering),
    result should include track_llm_degraded=True for observability."""

    def test_error_no_rows_after_label_filtering_includes_track_llm_degraded(self):
        """Contract: If Track LLM raises and backtest returns error 'No rows after label filtering',
        result must contain track_llm_degraded=True."""
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
        _canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": [100]})
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=_canonical_map),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(
                backtester_mod,
                "compute_track_llm_features",
                side_effect=RuntimeError("mock Track LLM failure"),
            ),
            patch.object(backtester_mod, "compute_labels", side_effect=_compute_labels_empty_after_filter),
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
        self.assertIn("error", result, "Test setup: must hit error return path.")
        self.assertEqual(result.get("error"), "No rows after label filtering")
        self.assertIs(
            result.get("track_llm_degraded"),
            True,
            "R407 #1: error return after Track LLM failed must include track_llm_degraded=True.",
        )


def _compute_labels_inside_window_but_all_unrated(bets_df, window_end, extended_end):
    """Return labeled inside window; canonical_map will be empty so all unrated → No rated observations."""
    out = bets_df.copy()
    out["label"] = 0
    out["censored"] = False
    # Keep payout inside window (e.g. window_end - 1 day)
    out["payout_complete_dtm"] = pd.Timestamp(window_end) - pd.Timedelta(hours=12)
    return out


class TestR407ErrorReturnNoRatedObservationsIncludesTrackLlmDegraded(unittest.TestCase):
    """R407 #1: When Track LLM failed and backtest returns 'No rated observations in window',
    result should include track_llm_degraded=True."""

    def test_error_no_rated_observations_includes_track_llm_degraded(self):
        """Contract: If Track LLM raises and backtest returns error 'No rated observations in window',
        result must contain track_llm_degraded=True."""
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
        # Empty canonical map → all rows unrated → after excluding unrated, labeled.empty → error return.
        _canonical_map_empty = pd.DataFrame(columns=["player_id", "canonical_id"])
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=_canonical_map_empty),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(
                backtester_mod,
                "compute_track_llm_features",
                side_effect=RuntimeError("mock Track LLM failure"),
            ),
            patch.object(backtester_mod, "compute_labels", side_effect=_compute_labels_inside_window_but_all_unrated),
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
        self.assertIs(
            result.get("track_llm_degraded"),
            True,
            "R407 #1: error return after Track LLM failed must include track_llm_degraded=True.",
        )


# ---------------------------------------------------------------------------
# R407 Review #2 — track_llm.candidates 元素非 dict 時不 crash（健壯性）
# ---------------------------------------------------------------------------

class TestR407CandidatesNonDictElements(unittest.TestCase):
    """R407 #2: When track_llm.candidates contains non-dict elements, backtest must not crash."""

    def test_backtest_does_not_crash_when_candidates_has_non_dict_elements(self):
        """Behavioral: feature_spec.track_llm.candidates = [{"feature_id": "f1"}, 123, "x"] does not raise;
        current production catches AttributeError and sets track_llm_degraded=True."""
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
        mixed_candidates_spec = {"track_llm": {"candidates": [{"feature_id": "f1"}, 123, "x"]}}
        _canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": [100]})
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=_canonical_map),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value=mixed_candidates_spec),
            patch.object(
                backtester_mod,
                "compute_track_llm_features",
                return_value=pd.DataFrame({"bet_id": [1]}),
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
        self.assertIsInstance(result, dict, "R407 #2: backtest must not crash with mixed candidates.")
        self.assertNotIn("error", result, "Mocks provide valid path; expect success dict.")
        # Current production catches AttributeError in try → track_llm_degraded=True.
        self.assertIn("track_llm_degraded", result)


# ---------------------------------------------------------------------------
# R407 Review #4 — use_local_parquet 傳遞至 load_player_profile（行為）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# R407 Review #3 補充 — 成功路徑 track_llm_degraded=False（契約）
# ---------------------------------------------------------------------------

class TestR407SuccessPathTrackLlmDegradedFalse(unittest.TestCase):
    """R407 #3 optional: When Track LLM succeeds, result must contain track_llm_degraded=False."""

    def test_backtest_result_has_track_llm_degraded_false_when_track_llm_succeeds(self):
        """Contract: When compute_track_llm_features does not raise, result must include track_llm_degraded=False."""
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
        _canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": [100]})
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=_canonical_map),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(
                backtester_mod,
                "compute_track_llm_features",
                return_value=pd.DataFrame({"bet_id": [1]}),
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
        self.assertNotIn("error", result)
        self.assertIs(
            result.get("track_llm_degraded"),
            False,
            "R407 #3: when Track LLM succeeds, result must include track_llm_degraded=False.",
        )


# ---------------------------------------------------------------------------
# R407 Review #4 — use_local_parquet 傳遞至 load_player_profile（行為）
# ---------------------------------------------------------------------------

class TestR407UseLocalParquetPassedToLoadPlayerProfile(unittest.TestCase):
    """R407 #4: backtest(..., use_local_parquet=True) must call load_player_profile with use_local_parquet=True."""

    def test_backtest_use_local_parquet_true_calls_load_player_profile_with_true(self):
        """Behavioral: When backtest(..., use_local_parquet=True), load_player_profile is invoked with use_local_parquet=True."""
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
        _canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": [100]})
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=_canonical_map),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(backtester_mod, "compute_track_llm_features", return_value=pd.DataFrame({"bet_id": [1]})),
            patch.object(backtester_mod, "compute_labels", side_effect=_minimal_compute_labels),
            patch.object(backtester_mod, "load_player_profile", return_value=None) as mock_load_profile,
            patch.object(backtester_mod, "join_player_profile", side_effect=_minimal_join_profile),
        ):
            backtester_mod.backtest(
                bets_raw=bets,
                sessions_raw=sessions,
                artifacts=artifacts,
                window_start=window_start,
                window_end=window_end,
                run_optuna=False,
                use_local_parquet=True,
            )
        mock_load_profile.assert_called_once()
        call_kwargs = mock_load_profile.call_args[1]
        self.assertIn("use_local_parquet", call_kwargs)
        self.assertIs(
            call_kwargs["use_local_parquet"],
            True,
            "R407 #4: backtest(use_local_parquet=True) must pass use_local_parquet=True to load_player_profile.",
        )


if __name__ == "__main__":
    unittest.main()
