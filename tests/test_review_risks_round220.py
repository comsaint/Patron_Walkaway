"""Minimal reproducible tests for Round 64 review risks (R1000-R1006).

Tests-only round: we intentionally do not change production code here.
Unfixed risks are encoded as expected failures so they stay visible.
"""

from __future__ import annotations

import inspect
import unittest

import trainer.trainer as trainer_mod


class TestR1000TrackADetection(unittest.TestCase):
    """R1000: Track A detection should not rely on raw numeric column heuristics."""

    def test_track_a_detection_should_use_feature_defs_not_numeric_heuristic(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "load_feature_defs(",
            src,
            "Track A candidate columns should come from feature_defs.json, not numeric heuristics.",
        )


class TestR1001ScreeningSanity(unittest.TestCase):
    """R1001: screening should preserve minimum Track-B coverage."""

    def test_screening_should_keep_at_least_one_track_b_feature(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertTrue(
            "TRACK_B_FEATURE_COLS" in src and "intersection" in src,
            "run_pipeline should enforce a post-screening Track-B sanity check.",
        )


class TestR1002DfsLeakageGuard(unittest.TestCase):
    """R1002: DFS exploration should exclude extended-zone bets."""

    def test_dfs_should_filter_to_core_window_before_run_track_a_dfs(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertTrue(
            'run_track_a_dfs(_dfs_bets' in src or 'run_track_a_dfs(dfs_bets' in src,
            "process_chunk should pass window-filtered bets to run_track_a_dfs.",
        )


class TestR1003CacheKeyConsistency(unittest.TestCase):
    """R1003: cache key should include AFG/defs-state dimensions."""

    def test_chunk_cache_key_should_include_no_afg_or_defs_state(self):
        src = inspect.getsource(trainer_mod._chunk_cache_key)
        self.assertTrue(
            ("no_afg" in src) or ("feature_defs" in src),
            "_chunk_cache_key should include no_afg or feature_defs state to avoid stale reuse.",
        )


class TestR1004ScreeningSkipFallback(unittest.TestCase):
    """R1004: screening-skip path should filter active_feature_cols to present columns."""

    def test_screening_skip_should_filter_active_feature_cols(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "active_feature_cols = [c for c in active_feature_cols if c in train_df.columns]",
            src,
            "When screening is skipped, active_feature_cols should be restricted to train_df columns.",
        )


class TestR1006SessionsCanonicalId(unittest.TestCase):
    """R1006: DFS call path should construct canonical_id for sessions."""

    def test_process_chunk_dfs_should_prepare_sessions_canonical_id(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn(
            '_dfs_sessions["canonical_id"] =',
            src,
            "process_chunk DFS path should ensure sessions has canonical_id before run_track_a_dfs.",
        )


if __name__ == "__main__":
    unittest.main()
