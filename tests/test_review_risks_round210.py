"""Minimal reproducible tests for Round 58 review risks (R900-R907).

Tests-only round: we intentionally do not change production code here.
Unfixed risks are encoded as expected failures so they stay visible.
"""

from __future__ import annotations

import inspect
import unittest

import numpy as np
import pandas as pd

import trainer.features as features_mod
import trainer.trainer as trainer_mod


class TestR900ScreenFeaturesSignature(unittest.TestCase):
    """R900: legacy mi_top_k kwarg should no longer be used."""

    def test_screen_features_accepts_top_k(self):
        rng = np.random.default_rng(42)
        X = pd.DataFrame(rng.normal(size=(120, 4)), columns=list("abcd"))
        y = pd.Series(([0, 1] * 60), dtype="int64")
        selected = features_mod.screen_features(
            feature_matrix=X,
            labels=y,
            feature_names=list(X.columns),
            top_k=None,
            use_lgbm=False,
        )
        self.assertGreaterEqual(len(selected), 1)

    def test_screen_features_rejects_legacy_mi_top_k_kwarg(self):
        rng = np.random.default_rng(7)
        X = pd.DataFrame(rng.normal(size=(80, 3)), columns=list("xyz"))
        y = pd.Series(([0, 1] * 40), dtype="int64")
        with self.assertRaises(TypeError):
            features_mod.screen_features(  # type: ignore[call-arg]
                feature_matrix=X,
                labels=y,
                feature_names=list(X.columns),
                mi_top_k=None,
                use_lgbm=False,
            )


class TestR901Step3dCanonicalIdJoin(unittest.TestCase):
    """R901: process_chunk DFS path should join canonical_id onto sessions."""

    def test_process_chunk_should_join_canonical_id_on_sessions_before_dfs(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn(
            '_dfs_sessions = _dfs_sessions.merge(',
            src,
            "process_chunk should merge canonical_id into sessions before run_track_a_dfs.",
        )


class TestR902Step3dDummyFilter(unittest.TestCase):
    """R902: process_chunk DFS path should exclude FND-12 dummy ids."""

    def test_process_chunk_should_filter_dummy_player_ids_before_dfs(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        dfs_start = src.find("# --- Track A: DFS exploration (DEC-020, first-chunk only via run_afg) ---")
        self.assertGreaterEqual(dfs_start, 0, "process_chunk should contain DFS block")
        dfs_src = src[dfs_start:]
        self.assertIn(
            "dummy_player_ids",
            dfs_src,
            "process_chunk should filter dummy player ids before DFS exploration.",
        )


class TestR903StaleFeatureDefsCleanup(unittest.TestCase):
    """R903: stale feature_defs should be removed before DFS retry."""

    def test_step3d_should_remove_stale_feature_defs_before_dfs(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertTrue(
            ("feature_defs.json" in src and ".unlink(" in src),
            "Step 3d should delete stale feature_defs.json before running DFS.",
        )


class TestR904ChunkCacheKeyNoAfg(unittest.TestCase):
    """R904: chunk cache key should include no_afg dimension."""

    def test_chunk_cache_key_should_include_no_afg_flag(self):
        src = inspect.getsource(trainer_mod._chunk_cache_key)
        self.assertIn(
            "no_afg",
            src,
            "_chunk_cache_key should include no_afg in key material.",
        )


class TestR905TopKValidation(unittest.TestCase):
    """R905: top_k=0 should fail fast instead of silently returning []."""

    def test_screen_features_top_k_zero_should_raise(self):
        rng = np.random.default_rng(99)
        X = pd.DataFrame(rng.normal(size=(100, 5)), columns=[f"f{i}" for i in range(5)])
        y = pd.Series(([0, 1] * 50), dtype="int64")
        with self.assertRaises(ValueError):
            features_mod.screen_features(
                feature_matrix=X,
                labels=y,
                feature_names=list(X.columns),
                top_k=0,
                use_lgbm=False,
            )


class TestR906FirstChunkDoubleLoad(unittest.TestCase):
    """R906: Step 3d and first process_chunk currently double-load first chunk."""

    def test_run_pipeline_should_document_or_guard_against_first_chunk_double_load(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertTrue(
            ("reuse" in src.lower() and "first chunk" in src.lower())
            or ("double-load" in src.lower()),
            "run_pipeline should include explicit guard/comment for first-chunk double-load.",
        )


class TestR907DfsSampleCap(unittest.TestCase):
    """R907: DFS exploration should have an absolute sample-size cap."""

    def test_run_track_a_dfs_should_have_absolute_sample_cap(self):
        src = inspect.getsource(trainer_mod.run_track_a_dfs)
        self.assertTrue(
            ("_max_sample" in src) or ("sample(n=" in src and "min(" in src),
            "run_track_a_dfs should cap sampled rows by an absolute max size.",
        )


if __name__ == "__main__":
    unittest.main()

