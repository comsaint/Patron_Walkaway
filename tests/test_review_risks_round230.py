"""Guardrail tests for Round 67 review risks (R1100-R1105).

Production-code fixes were applied in Round 69; @expectedFailure decorators
have been removed and these tests now run as active guardrails.
"""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.trainer as trainer_mod


class _DummyProbModel:
    """Deterministic predict_proba model for metric reproducibility."""

    def predict_proba(self, X):
        n = len(X)
        scores = np.linspace(0.1, 0.9, n) if n > 0 else np.array([], dtype="float64")
        return np.c_[1.0 - scores, scores]


class TestR1100AllPositiveTestLabels(unittest.TestCase):
    """R1100: _compute_test_metrics should guard against all-positive test labels."""

    def test_compute_test_metrics_all_positive_labels_should_be_guarded(self):
        n = max(100, int(getattr(trainer_mod, "MIN_VALID_TEST_ROWS", 50)))
        X_test = pd.DataFrame({"f0": np.arange(n)})
        y_test = pd.Series(np.ones(n, dtype="int64"))
        out = trainer_mod._compute_test_metrics(
            model=_DummyProbModel(),
            threshold=0.5,
            X_test=X_test,
            y_test=y_test,
            label="r1100-all-positive",
        )
        self.assertEqual(
            out["test_prauc"],
            0.0,
            "All-positive test labels should be treated as invalid eval input, not PR-AUC=1.0.",
        )


class TestR1101UncalibratedThresholdFlag(unittest.TestCase):
    """R1101: uncalibrated threshold usage should be explicitly flagged in test metrics."""

    def test_compute_test_metrics_should_include_uncalibrated_flag_contract(self):
        src = inspect.getsource(trainer_mod._compute_test_metrics)
        self.assertIn(
            "test_threshold_uncalibrated",
            src,
            "Contract missing: _compute_test_metrics should carry uncalibrated-threshold flag.",
        )


class TestR1102FeatureImportanceLengthMismatch(unittest.TestCase):
    """R1102: length mismatch between feature list and importance vector must not be silent."""

    def test_feature_importance_length_mismatch_should_raise(self):
        class _MockModel:
            feature_importances_ = np.array([9.0, 1.0], dtype="float64")

        with self.assertRaises(
            ValueError,
            msg="Length mismatch should raise instead of silently truncating via zip().",
        ):
            trainer_mod._compute_feature_importance(_MockModel(), ["f0", "f1", "f2"])


class TestR1103FeatureImportanceExceptionScope(unittest.TestCase):
    """R1103: unexpected runtime errors should not be swallowed by broad exception handling."""

    def test_feature_importance_unexpected_error_should_propagate(self):
        class _BadBooster:
            def feature_name(self):
                return ["f0", "f1"]

            def feature_importance(self, importance_type="gain"):
                raise RuntimeError("simulated booster failure")

        class _MockModel:
            booster_ = _BadBooster()
            feature_importances_ = np.array([5.0, 4.0], dtype="float64")

        with self.assertRaises(RuntimeError):
            trainer_mod._compute_feature_importance(_MockModel(), ["f0", "f1"])


class TestR1104NoTestDfContract(unittest.TestCase):
    """R1104: train_dual_model(test_df=None) should not run test-metrics evaluation path."""

    def test_train_dual_model_no_test_df_should_not_call_compute_test_metrics(self):
        train_df = pd.DataFrame(
            {
                "is_rated": [True, False, True, False],
                "label": [1, 0, 0, 1],
                "f0": [0.1, 0.2, 0.3, 0.4],
            }
        )
        valid_df = train_df.copy()
        feature_cols = ["f0"]

        with patch.object(
            trainer_mod,
            "_train_one_model",
            return_value=(object(), {"threshold": 0.5, "val_f1": 0.0, "_uncalibrated": True}),
        ), patch.object(
            trainer_mod,
            "_compute_feature_importance",
            return_value=[{"rank": 1, "feature": "f0", "importance_gain": 1.0}],
        ), patch.object(
            trainer_mod,
            "_compute_test_metrics",
            return_value={"test_prauc": 0.0},
        ) as mock_test_eval:
            trainer_mod.train_dual_model(
                train_df=train_df,
                valid_df=valid_df,
                feature_cols=feature_cols,
                run_optuna=False,
                test_df=None,
            )

            mock_test_eval.assert_not_called()


class TestR1105TestIndexAlignment(unittest.TestCase):
    """R1105: test-label/index alignment should be explicit (reset_index or numpy values)."""

    def test_compute_test_metrics_should_explicitly_normalize_index(self):
        src = inspect.getsource(trainer_mod._compute_test_metrics)
        self.assertTrue(
            ("reset_index(drop=True)" in src) or (".values" in src),
            "_compute_test_metrics should normalize y_test index to avoid subtle alignment issues.",
        )


if __name__ == "__main__":
    unittest.main()
