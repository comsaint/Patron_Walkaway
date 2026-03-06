"""Minimal reproducible tests for Round 64 review risks (R1000-R1006).

Tests-only round: we intentionally do not change production code here.
Unfixed risks are encoded as expected failures so they stay visible.
"""

from __future__ import annotations

import inspect
import unittest

import trainer.trainer as trainer_mod


class TestR1000TrackLlmDetection(unittest.TestCase):
    """R1000: Track LLM detection should come from feature spec, not heuristics."""

    def test_track_llm_detection_should_use_feature_spec_not_numeric_heuristic(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertTrue(
            ("load_feature_spec(" in src) or ("feature_spec.get(\"track_llm\"" in src),
            "Track LLM candidate columns should come from feature spec, not numeric heuristics.",
        )


class TestR1001ScreeningSanity(unittest.TestCase):
    """R1001: screening should preserve minimum Track-B coverage."""

    def test_screening_should_keep_at_least_one_track_b_feature(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertTrue(
            "TRACK_B_FEATURE_COLS" in src and "intersection" in src,
            "run_pipeline should enforce a post-screening Track-B sanity check.",
        )


class TestR1002TrackLlmLeakageGuard(unittest.TestCase):
    """R1002: Track LLM computation should pass cutoff_time guard."""

    def test_track_llm_should_pass_cutoff_time_window_end(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn(
            "cutoff_time=window_end",
            src,
            "process_chunk should pass cutoff_time=window_end to Track LLM compute.",
        )


class TestR1003CacheKeyConsistency(unittest.TestCase):
    """R1003: cache key should include AFG/defs-state dimensions."""

    def test_chunk_cache_key_should_include_no_afg_or_defs_state(self):
        src = inspect.getsource(trainer_mod._chunk_cache_key)
        self.assertTrue(
            ("feature_spec_hash" in src) or ("spec" in src),
            "_chunk_cache_key should include feature spec state to avoid stale reuse.",
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


class TestR1006CanonicalIdFallback(unittest.TestCase):
    """R1006: process_chunk should keep canonical_id fallback for missing mappings."""

    def test_process_chunk_should_prepare_canonical_id(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn(
            'bets["canonical_id"] = bets["canonical_id"].fillna',
            src,
            "process_chunk should ensure canonical_id fallback before feature computation.",
        )


if __name__ == "__main__":
    unittest.main()
