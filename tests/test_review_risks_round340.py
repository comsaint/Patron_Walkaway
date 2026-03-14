"""Round 92 reviewer risks -> minimal reproducible tests (tests-only).

This file intentionally does NOT modify production code.
Unfixed risks are tracked as expected failures so they stay visible.
"""

from __future__ import annotations

import pytest

import importlib
import inspect
import pathlib
import sys
import unittest


def _import_module(name: str):
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module(name)


def _read_text(rel_path: str) -> str:
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    return (repo_root / rel_path).read_text(encoding="utf-8")


features_mod = _import_module("trainer.features")
trainer_mod = _import_module("trainer.trainer")
scorer_mod = _import_module("trainer.scorer")


class TestR2106FeatureSpecKeywordGuard(unittest.TestCase):
    """R2106: Track-LLM expression validator should block DDL/DML keywords."""

    def test_validate_feature_spec_should_block_drop_keyword(self):
        src = inspect.getsource(features_mod._validate_feature_spec)
        self.assertIn(
            "DROP",
            src,
            "_validate_feature_spec should explicitly block DROP keyword.",
        )


class TestR2111WindowFrameInjectionGuard(unittest.TestCase):
    """R2111: window_frame should also reject semicolons/injection patterns."""

    def test_validate_feature_spec_should_check_window_frame_semicolon(self):
        src = inspect.getsource(features_mod._validate_feature_spec)
        self.assertIn(
            '";" in wf',
            src,
            "_validate_feature_spec should reject semicolon in window_frame.",
        )


class TestR2300ScorerLegacyParityFields(unittest.TestCase):
    """R2300: scorer must compute session_duration_min and bets_per_minute."""

    def test_build_features_for_scoring_should_compute_session_duration(self):
        src = inspect.getsource(scorer_mod.build_features_for_scoring)
        self.assertIn(
            'bets_df["session_duration_min"] =',
            src,
            "build_features_for_scoring should compute session_duration_min.",
        )

    def test_build_features_for_scoring_should_compute_bets_per_minute(self):
        src = inspect.getsource(scorer_mod.build_features_for_scoring)
        self.assertIn(
            'bets_df["bets_per_minute"] =',
            src,
            "build_features_for_scoring should compute bets_per_minute.",
        )


class TestR2206FastModeArtifactSafety(unittest.TestCase):
    """R2206: legacy fast_mode artifact guard should be removed."""

    def test_load_dual_artifacts_should_check_fast_mode_flag(self):
        src = inspect.getsource(scorer_mod.load_dual_artifacts)
        self.assertNotIn(
            "fast_mode",
            src,
            "load_dual_artifacts should not contain legacy fast_mode guard.",
        )


class TestR2207UncalibratedFlagPropagation(unittest.TestCase):
    """R2207: save_artifact_bundle should read _uncalibrated from metrics."""

    def test_save_artifact_bundle_should_read_uncalibrated_from_metrics(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertIn(
            'rated["metrics"].get("_uncalibrated"',
            src,
            "save_artifact_bundle should propagate _uncalibrated from metrics.",
        )


@pytest.mark.skip(reason="api_server reverted to DB-only; model API removed")
class TestR2320ApiScoreNumericValidation(unittest.TestCase):
    """R2320: /score should validate numeric feature value types before inference."""

    def test_api_score_should_contain_numeric_type_validation(self):
        src = _read_text("trainer/serving/api_server.py")
        self.assertIn(
            "isinstance(v, (int, float, bool))",
            src,
            "/score should validate feature value types and reject non-numeric inputs.",
        )


@pytest.mark.skip(reason="api_server reverted to DB-only; model API removed")
class TestR2323PathTraversalGuard(unittest.TestCase):
    """R2323: frontend module route should use safe path join."""

    def test_frontend_module_should_use_safe_join(self):
        src = _read_text("trainer/serving/api_server.py")
        self.assertIn(
            "safe_join",
            src,
            "frontend_module should use safe_join to prevent path traversal.",
        )


if __name__ == "__main__":
    unittest.main()
