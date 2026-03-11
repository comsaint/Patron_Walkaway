"""Minimal reproducible tests for Round 222 Review — Train–Serve Parity 變更審查風險點.

Round 222 Review (STATUS.md) risk points are turned into contract/source or behavioral tests.
Tests-only: no production code changes.
"""

from __future__ import annotations

import datetime as dt
import inspect
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

try:
    import trainer.backtester as backtester_mod
except ImportError:
    import backtester as backtester_mod  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# R222 Review #1 — Track LLM 失敗時靜默降級（正確性／可觀測性）
# ---------------------------------------------------------------------------

class TestR222TrackLlmFailureSilentDegradation(unittest.TestCase):
    """R222 #1: When compute_track_llm_features raises, backtest only logs error; no warning about zero-fill/unreliable."""

    def test_backtest_except_block_contains_track_llm_failed_log(self):
        """Contract: backtest() except block logs 'Track LLM failed' (current). When production adds warning about zero-fill/unreliable, add assert for it."""
        source = inspect.getsource(backtester_mod.backtest)
        self.assertIn(
            "Track LLM failed",
            source,
            "R222 #1: backtest() except block must log 'Track LLM failed'.",
        )
        # Production now logs warning that scores may be unreliable when Track LLM fails.
        self.assertIn(
            "zero-filled",
            source,
            "R222 #1: backtest() except block must log warning containing 'zero-filled' (artifact LLM features zero-filled).",
        )

    def test_backtest_returns_dict_when_track_llm_raises(self):
        """Behavioral: When compute_track_llm_features raises, backtest still returns a dict (no crash); LLM cols zero-filled."""
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

        # One-row canonical map so the single bet is rated (PLAN § exclude unrated → need rated obs).
        _canonical_map_rated = pd.DataFrame({"player_id": [100], "canonical_id": [100]})
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=_canonical_map_rated),
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
        self.assertIsInstance(result, dict, "R222 #1: backtest must return dict when Track LLM raises.")
        self.assertNotIn("error", result, "R222 #1: backtest should complete without error key when mocks provide valid path.")
        self.assertIs(
            result.get("track_llm_degraded"),
            True,
            "R222 #1 (PLAN): when Track LLM raises, result must include track_llm_degraded=True.",
        )


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
# R222 Review #2 — canonical_map 為空時 load_player_profile(canonical_ids=None)
# ---------------------------------------------------------------------------

class TestR222CanonicalMapEmptyLoadProfileFullTable(unittest.TestCase):
    """R222 #2: When canonical_map is empty, backtest passes canonical_ids=None → load_player_profile may load full table."""

    def test_backtest_source_passes_canonical_ids_to_load_player_profile(self):
        """Contract: backtest() calls load_player_profile with canonical_ids=_rated_cids; current code uses None when map empty.
        When production fixes to [], update to assert canonical_ids=[] or _rated_cids when empty."""
        source = inspect.getsource(backtester_mod.backtest)
        self.assertIn(
            "load_player_profile",
            source,
            "R222 #2: backtest must call load_player_profile.",
        )
        self.assertIn(
            "canonical_ids=_rated_cids",
            source,
            "R222 #2: backtest must pass canonical_ids to load_player_profile.",
        )
        # R222 #2 fixed: empty map → else [] so load_player_profile gets [] and does not load full table.
        self.assertIn(
            "else []",
            source,
            "R222 #2: backtest must pass canonical_ids=[] when canonical_map empty (no full-table load).",
        )


# ---------------------------------------------------------------------------
# R222 Review #3 — use_local_parquet 未從 CLI 傳入（Parity）
# ---------------------------------------------------------------------------

class TestR222UseLocalParquetNotPassed(unittest.TestCase):
    """R222 #3: backtest() hardcodes use_local_parquet=False when calling load_player_profile."""

    def test_backtest_source_calls_load_player_profile_with_use_local_parquet_false(self):
        """Contract: backtest() accepts use_local_parquet and passes it to load_player_profile(..., use_local_parquet=use_local_parquet)."""
        source = inspect.getsource(backtester_mod.backtest)
        self.assertIn(
            "use_local_parquet=use_local_parquet",
            source,
            "R222 #3: backtest() must pass use_local_parquet parameter through to load_player_profile.",
        )
        self.assertIn(
            "use_local_parquet: bool = False",
            source,
            "R222 #3: backtest() must accept use_local_parquet parameter (default False).",
        )


# ---------------------------------------------------------------------------
# R222 Review #4 — feature_spec track_llm.candidates 非 list（健壯性）
# ---------------------------------------------------------------------------

class TestR222FeatureSpecCandidatesNonList(unittest.TestCase):
    """R222 #4: If track_llm.candidates is not a list (e.g. dict), iteration may be wrong; no isinstance guard."""

    def test_backtest_source_gets_candidates_with_default_list(self):
        """Contract: backtest gets track_llm candidates with type guard (isinstance) so non-list (e.g. dict) is treated as no candidates."""
        source = inspect.getsource(backtester_mod.backtest)
        self.assertIn(
            "isinstance(_raw_candidates, list)",
            source,
            "R222 #4: backtest must guard candidates with isinstance(_raw_candidates, list) else [].",
        )
        self.assertIn(
            '.get("candidates")',
            source,
            "R222 #4: backtest gets candidates from feature_spec track_llm.",
        )

    def test_backtest_does_not_crash_when_candidates_is_dict(self):
        """Behavioral: When load_feature_spec returns track_llm.candidates = dict, backtest does not raise."""
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
        # candidates is a dict (non-list); iteration yields keys. One-row canonical map so bet is rated.
        bad_spec = {"track_llm": {"candidates": {"key1": 1, "key2": 2}}}
        _canonical_map_rated = pd.DataFrame({"player_id": [100], "canonical_id": [100]})
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=_canonical_map_rated),
            patch.object(backtester_mod, "add_track_human_features", side_effect=lambda df, *_, **__: df),
            patch.object(backtester_mod, "load_feature_spec", return_value=bad_spec),
            patch.object(backtester_mod, "compute_track_llm_features", return_value=pd.DataFrame({"bet_id": [1]})),
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


if __name__ == "__main__":
    unittest.main()
