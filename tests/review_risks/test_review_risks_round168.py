"""Minimal reproducible tests for Round 168 Review (Feature Screening screen_method).

Tests-only: no production code changes. Covers invalid screen_method,
single-feature lgbm path, MI not called when screen_method='lgbm', and
deterministic regression for lgbm path.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.features as features_mod


class TestRound168ReviewRisks(unittest.TestCase):
    """Round 168 Review: screen_method validation, lgbm path behavior, MI not invoked."""

    def test_screen_method_invalid_raises_value_error(self):
        """R168-1: Invalid screen_method (e.g. 'LGBM', 'Mi', 'x') raises ValueError (Round 168 Review §2)."""
        rng = np.random.default_rng(42)
        X = pd.DataFrame(rng.normal(size=(60, 3)), columns=list("abc"))
        y = pd.Series(([0, 1] * 30), dtype="int64")
        for invalid in ("LGBM", "Mi", "mi_then_LGBM", "x", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError) as ctx:
                    features_mod.screen_features(
                        feature_matrix=X,
                        labels=y,
                        feature_names=list(X.columns),
                        screen_method=invalid,
                    )
                self.assertIn(
                    "screen_method must be one of",
                    str(ctx.exception),
                    "Error message should document allowed values",
                )
                self.assertIn(
                    invalid if invalid else "''",
                    str(ctx.exception),
                    "Error message should include the invalid value received",
                )

    def test_screen_method_lgbm_single_feature_returns_one(self):
        """R168-2: Single non-zero-variance feature with screen_method='lgbm' returns list of length 1 (Round 168 Review §2)."""
        X = pd.DataFrame({"f1": np.arange(50, dtype=float)})  # non-zero variance
        y = pd.Series(([0, 1] * 25), dtype="int64")
        out = features_mod.screen_features(
            feature_matrix=X,
            labels=y,
            feature_names=["f1"],
            screen_method="lgbm",
            random_state=42,
        )
        self.assertEqual(len(out), 1, "Single feature must yield one selected feature")
        self.assertEqual(out[0], "f1", "Selected feature must be the only candidate")

    def test_screen_method_lgbm_does_not_call_mutual_info(self):
        """R168-3: screen_method='lgbm' must not call mutual_info_classif (Round 168 Review §4)."""
        rng = np.random.default_rng(7)
        X = pd.DataFrame(rng.normal(size=(80, 4)), columns=list("abcd"))
        y = pd.Series(([0, 1] * 40), dtype="int64")
        with patch("sklearn.feature_selection.mutual_info_classif") as mock_mi:
            features_mod.screen_features(
                feature_matrix=X,
                labels=y,
                feature_names=list(X.columns),
                screen_method="lgbm",
            )
            mock_mi.assert_not_called()

    def test_screen_method_lgbm_deterministic_for_fixed_seed(self):
        """R168-4: screen_method='lgbm' is deterministic for fixed random_state (Round 168 Review §1 regression)."""
        rng = np.random.default_rng(99)
        X = pd.DataFrame(rng.normal(size=(100, 4)), columns=[f"f{i}" for i in range(4)])
        y = pd.Series(([0, 1] * 50), dtype="int64")
        out1 = features_mod.screen_features(
            feature_matrix=X,
            labels=y,
            feature_names=list(X.columns),
            screen_method="lgbm",
            random_state=42,
        )
        out2 = features_mod.screen_features(
            feature_matrix=X,
            labels=y,
            feature_names=list(X.columns),
            screen_method="lgbm",
            random_state=42,
        )
        self.assertEqual(out1, out2, "Same inputs and random_state must yield identical result")
        self.assertGreaterEqual(len(out1), 1)
        self.assertLessEqual(set(out1), set(X.columns))

    def test_use_lgbm_true_with_lgbm_method_treated_as_mi_then_lgbm(self):
        """R168-5: use_lgbm=True and screen_method='lgbm' runs mi_then_lgbm path (backward compat)."""
        rng = np.random.default_rng(11)
        X = pd.DataFrame(rng.normal(size=(70, 3)), columns=list("xyz"))
        y = pd.Series(([0, 1] * 35), dtype="int64")
        with patch("sklearn.feature_selection.mutual_info_classif") as mock_mi:
            mock_mi.return_value = np.array([0.1, 0.2, 0.15])  # MI scores
            features_mod.screen_features(
                feature_matrix=X,
                labels=y,
                feature_names=list(X.columns),
                use_lgbm=True,
                screen_method="lgbm",
            )
            mock_mi.assert_called_once()


if __name__ == "__main__":
    unittest.main()
