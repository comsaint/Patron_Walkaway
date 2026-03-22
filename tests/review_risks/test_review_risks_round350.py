"""Round 103 reviewer risks -> minimal reproducible tests (tests-only).

This round intentionally does NOT modify production code.
The tests encode desired guardrails for current review findings.
"""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone
import importlib
import pathlib
import sys

import pandas as pd

def _import_module(name: str):
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module(name)


features_mod = _import_module("trainer.features")
scorer_mod = _import_module("trainer.scorer")
trainer_mod = _import_module("trainer.trainer")


class TestR3500TrackLlmHistoryParity(unittest.TestCase):
    """R3500: trainer should compute Track LLM before label window filtering."""

    def test_process_chunk_should_compute_track_llm_before_compute_labels(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        idx_llm = src.find("compute_track_llm_features(")
        idx_labels = src.find("compute_labels(")
        self.assertNotEqual(idx_llm, -1, "process_chunk should call compute_track_llm_features.")
        self.assertNotEqual(idx_labels, -1, "process_chunk should call compute_labels.")
        self.assertLess(
            idx_llm,
            idx_labels,
            "Track LLM should be computed on full bets history before compute_labels to keep train-serve parity.",
        )


class TestR3501ArtifactSpecFreeze(unittest.TestCase):
    """R3501: artifact bundle should freeze feature spec + spec hash.

    Covers PLAN 特徵整合計畫 Step 7 (Artifact 產出) and R3501/R3507: save_artifact_bundle
    persists feature_spec.yaml and spec_hash in training_metrics; load_dual_artifacts
    prefers model_dir/feature_spec.yaml. See STATUS Round 147 Review P2."""

    def test_save_artifact_bundle_should_persist_feature_spec_snapshot(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertIn(
            "feature_spec.yaml",
            src,
            "save_artifact_bundle should persist frozen feature spec into model artifacts.",
        )

    def test_training_metrics_should_include_spec_hash(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertIn(
            "spec_hash",
            src,
            "training_metrics artifact should record spec_hash for train-serve reproducibility.",
        )


class TestR3502NoSilentTrackLlmFailure(unittest.TestCase):
    """R3502: Track LLM failures should not silently degrade pipeline behavior."""

    def test_trainer_track_llm_failure_should_not_be_warning_only(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertNotIn(
            "Track LLM skipped",
            src,
            "trainer should avoid warning-only Track LLM failure path; raise or hard-flag it.",
        )

    def test_process_chunk_dec031_track_llm_exceptions_not_swallowed(self):
        """DEC-031 / T-DEC031 step 1: no try/except that logs and continues past Track LLM."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertNotIn(
            "Track LLM full traceback",
            src,
            "process_chunk must not catch Track LLM failures; exceptions must propagate.",
        )
        self.assertNotIn(
            "Track LLM failed —",
            src,
            "process_chunk must not use swallowed Track LLM error logging path.",
        )

    def test_scorer_track_llm_failure_should_not_be_warning_only(self):
        src = inspect.getsource(scorer_mod.score_once)
        self.assertNotIn(
            "Track LLM features skipped",
            src,
            "scorer should avoid warning-only Track LLM failure path; raise or hard-flag it.",
        )


class TestR3503ScorerCutoffRowLossGuard(unittest.TestCase):
    """R3503: scorer Track LLM path should protect against cutoff-based row drops."""

    def test_score_once_should_have_track_llm_row_loss_guard(self):
        src = inspect.getsource(scorer_mod.score_once)
        self.assertTrue(
            ("Track LLM dropped" in src) or ("timedelta(seconds=30)" in src),
            "score_once should either add cutoff buffer or log row-loss guard after Track LLM compute.",
        )


class TestR3504CandidateDedup(unittest.TestCase):
    """R3504: run_pipeline should deduplicate merged candidate columns."""

    def test_run_pipeline_should_deduplicate_all_candidate_cols(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "dict.fromkeys(active_feature_cols + _track_llm_cols)",
            src,
            "run_pipeline should deduplicate merged candidate columns before screen_features.",
        )


class TestR3505UtcCutoffNormalization(unittest.TestCase):
    """R3505: scorer cutoff strip should timezone-convert before tz removal."""

    def test_build_features_for_scoring_should_tz_convert_cutoff_time(self):
        src = inspect.getsource(scorer_mod.build_features_for_scoring)
        self.assertIn(
            "tz_convert",
            src,
            "build_features_for_scoring should use tz_convert before removing tzinfo from cutoff_time.",
        )


class TestR3506FeatureSpecDuckdbFileAccessGuard(unittest.TestCase):
    """R3506: feature spec validator should block DuckDB file-access functions."""

    def test_validate_feature_spec_should_block_read_parquet_expression(self):
        spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "f_read_file",
                        "type": "derived",
                        "expression": "read_parquet('/tmp/secret.parquet')",
                    }
                ]
            }
        }
        with self.assertRaises(ValueError):
            features_mod._validate_feature_spec(spec)


class TestR3507ScorerLoadsFrozenArtifactSpec(unittest.TestCase):
    """R3507: scorer should prefer model artifact feature_spec over global template.

    Covers PLAN 特徵整合計畫 Step 7 (Feature Spec 凍結) with R3501/R3507: scorer
    load_dual_artifacts uses model_dir/feature_spec.yaml for train-serve consistency.
    See STATUS Round 147 Review P2."""

    def test_load_dual_artifacts_should_reference_model_local_feature_spec(self):
        src = inspect.getsource(scorer_mod.load_dual_artifacts)
        self.assertIn(
            "model_dir",
            src,
            "load_dual_artifacts should support loading frozen feature spec from provided model_dir.",
        )
        self.assertIn(
            "feature_spec.yaml",
            src,
            "load_dual_artifacts should prefer model_dir/feature_spec.yaml for train-serve consistency.",
        )


class TestR3508TrackLlmCutoffBehaviorMre(unittest.TestCase):
    """R3508: minimal repro that current cutoff can drop rows near scorer now_hk."""

    def test_compute_track_llm_features_should_not_drop_rows_just_after_cutoff(self):
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        bets_df = pd.DataFrame(
            {
                "canonical_id": ["c1", "c1"],
                "bet_id": ["b1", "b2"],
                "payout_complete_dtm": [now_utc - timedelta(seconds=1), now_utc + timedelta(seconds=5)],
                "wager": [10.0, 20.0],
            }
        )
        feature_spec = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "wager_lag1",
                        "type": "lag",
                        "expression": "lag(wager, 1)",
                        "window_frame": "",
                        "postprocess": {"fill": {"strategy": "zero"}},
                    }
                ]
            }
        }
        out = features_mod.compute_track_llm_features(
            bets_df,
            feature_spec=feature_spec,
            cutoff_time=now_utc,
        )
        self.assertEqual(
            len(out),
            len(bets_df),
            "Rows slightly after cutoff should not be silently dropped in scorer-like usage.",
        )


if __name__ == "__main__":
    unittest.main()
