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
    """R901: process_chunk should preserve canonical_id fallback before feature calc."""

    def test_process_chunk_should_fill_missing_canonical_id(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn(
            'bets["canonical_id"] = bets["canonical_id"].fillna',
            src,
            "process_chunk should keep fallback canonical_id when mapping misses.",
        )


class TestR902Step3dDummyFilter(unittest.TestCase):
    """R902: process_chunk should exclude FND-12 dummy ids before feature engineering."""

    def test_process_chunk_should_filter_dummy_player_ids_before_feature_calc(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn(
            "dummy_player_ids",
            src,
            "process_chunk should filter dummy player ids before feature computation.",
        )


class TestR903StaleFeatureDefsCleanup(unittest.TestCase):
    """R903: run_pipeline should load Track LLM feature spec before chunk loop."""

    def test_run_pipeline_should_load_feature_spec(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "load_feature_spec(",
            src,
            "run_pipeline should load Track LLM feature spec once before chunk processing.",
        )


class TestR904ChunkCacheKeyIncludesFeatureSpecHash(unittest.TestCase):
    """R904: chunk cache key should include feature spec hash dimension."""

    def test_chunk_cache_key_should_include_no_afg_flag(self):
        src = inspect.getsource(trainer_mod._chunk_cache_key)
        self.assertIn(
            "feature_spec_hash",
            src,
            "_chunk_cache_key should include feature_spec_hash in key material.",
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
    """R906: no legacy run_afg plumbing should remain after Track LLM migration."""

    def test_run_pipeline_should_not_pass_run_afg(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertNotIn(
            "run_afg=",
            src,
            "run_pipeline should not use legacy run_afg after removing Track A DFS path.",
        )


class TestR907DfsSampleCap(unittest.TestCase):
    """R907: legacy Track A DFS entrypoint should be removed."""

    def test_run_track_a_dfs_should_not_exist(self):
        self.assertFalse(
            hasattr(trainer_mod, "run_track_a_dfs"),
            "run_track_a_dfs should be removed after Track LLM migration.",
        )


if __name__ == "__main__":
    unittest.main()

