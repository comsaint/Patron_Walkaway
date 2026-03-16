"""Minimal reproducible guards for reviewer risks (Round 372).

Scope:
- Convert self-review risk points in STATUS.md into executable tests.
- Tests only; no production code edits.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import unittest

import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve

def _import_trainer():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.trainer")


trainer_mod = _import_trainer()


class _FixedScoreModel:
    """Tiny test double returning deterministic probabilities."""

    def __init__(self, scores: np.ndarray):
        self._scores = np.asarray(scores, dtype=float)

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = len(x)
        if n != len(self._scores):
            raise AssertionError(f"length mismatch: X={n}, scores={len(self._scores)}")
        return np.column_stack([1.0 - self._scores, self._scores])


def _make_xy(y: np.ndarray, scores: np.ndarray) -> tuple[pd.DataFrame, pd.Series]:
    x = pd.DataFrame({"f1": np.arange(len(y), dtype=float)})
    y_s = pd.Series(y.astype(int), name="label")
    return x, y_s


class TestR372ReviewerRiskGuards(unittest.TestCase):
    """Risk guards derived from STATUS.md self-review items."""

    def test_precision_at_recall_known_curve(self):
        """R372-1: precision@recall keys match PR-curve definition."""
        y = np.array([1, 1, 1, 0, 0, 0, 1, 0, 1, 0] * 10, dtype=int)  # n=100
        scores = np.array([0.9, 0.8, 0.55, 0.52, 0.4, 0.35, 0.7, 0.2, 0.6, 0.1] * 10, dtype=float)
        x, y_s = _make_xy(y, scores)
        model = _FixedScoreModel(scores)

        out = trainer_mod._compute_test_metrics(
            model=model,
            threshold=0.5,
            X_test=x,
            y_test=y_s,
            label="rated",
            log_results=False,
            production_neg_pos_ratio=None,
        )

        p_arr, r_arr, _ = precision_recall_curve(y_s, scores)
        for r in (0.001, 0.01, 0.1, 0.5):  # DEC-026
            mask = r_arr >= r
            expected = float(p_arr[mask].max()) if mask.any() else None
            self.assertAlmostEqual(out[f"test_precision_at_recall_{r}"], expected)

    def test_precision_at_recall_all_positive_returns_none_keys(self):
        """R372-2: all-positive test split uses early-return and None precision@recall."""
        y = np.ones(80, dtype=int)
        scores = np.full(80, 0.8, dtype=float)
        x, y_s = _make_xy(y, scores)
        model = _FixedScoreModel(scores)
        out = trainer_mod._compute_test_metrics(
            model, 0.5, x, y_s, log_results=False, production_neg_pos_ratio=15.0
        )
        self.assertIsNone(out["test_precision_at_recall_0.001"])
        self.assertIsNone(out["test_precision_at_recall_0.01"])
        self.assertIsNone(out["test_precision_at_recall_0.1"])
        self.assertIsNone(out["test_precision_at_recall_0.5"])

    def test_precision_at_recall_too_few_rows_returns_none_keys(self):
        """R372-3: small test split (< MIN_VALID_TEST_ROWS) returns None precision@recall."""
        y = np.array([1, 0, 1, 0, 1, 0, 0, 1, 0, 1], dtype=int)  # n=10
        scores = np.array([0.9, 0.2, 0.8, 0.3, 0.7, 0.1, 0.4, 0.6, 0.5, 0.55], dtype=float)
        x, y_s = _make_xy(y, scores)
        model = _FixedScoreModel(scores)
        out = trainer_mod._compute_test_metrics(
            model, 0.5, x, y_s, log_results=False, production_neg_pos_ratio=15.0
        )
        self.assertEqual(out["test_ap"], 0.0)
        self.assertIsNone(out["test_precision_at_recall_0.001"])
        self.assertIsNone(out["test_precision_at_recall_0.01"])
        self.assertIsNone(out["test_precision_at_recall_0.1"])
        self.assertIsNone(out["test_precision_at_recall_0.5"])

    def test_prod_adjusted_basic_formula(self):
        """R372-4: adjusted precision follows the documented closed-form formula."""
        # Build y/scores so threshold=0.5 yields: TP=20, FP=20, FN=30.
        # => precision=0.5, test neg/pos = 50/50 = 1.
        y = np.array([1] * 50 + [0] * 50, dtype=int)
        scores = np.array(
            [0.9] * 20 + [0.1] * 30 + [0.9] * 20 + [0.1] * 30,
            dtype=float,
        )
        x, y_s = _make_xy(y, scores)
        model = _FixedScoreModel(scores)
        out = trainer_mod._compute_test_metrics(
            model, 0.5, x, y_s, log_results=False, production_neg_pos_ratio=15.0
        )
        self.assertAlmostEqual(out["test_precision"], 0.5)
        self.assertAlmostEqual(out["test_neg_pos_ratio"], 1.0)
        self.assertAlmostEqual(out["test_precision_prod_adjusted"], 1.0 / 16.0)

    def test_prod_adjusted_none_when_ratio_not_set(self):
        """R372-5: ratio=None disables adjusted precision."""
        y = np.array([1, 1, 0, 0] * 25, dtype=int)
        scores = np.array([0.9, 0.8, 0.6, 0.2] * 25, dtype=float)
        x, y_s = _make_xy(y, scores)
        out = trainer_mod._compute_test_metrics(
            _FixedScoreModel(scores), 0.5, x, y_s, log_results=False, production_neg_pos_ratio=None
        )
        self.assertIsNone(out["test_precision_prod_adjusted"])

    def test_prod_adjusted_none_when_ratio_zero_and_logs_warning(self):
        """R372-6: ratio=0 is invalid and should warn + return None."""
        y = np.array([1, 1, 0, 0] * 25, dtype=int)
        scores = np.array([0.9, 0.8, 0.6, 0.2] * 25, dtype=float)
        x, y_s = _make_xy(y, scores)
        with self.assertLogs("trainer", level="WARNING") as cm:
            out = trainer_mod._compute_test_metrics(
                _FixedScoreModel(scores), 0.5, x, y_s, log_results=False, production_neg_pos_ratio=0.0
            )
        self.assertIsNone(out["test_precision_prod_adjusted"])
        self.assertTrue(any("invalid" in m.lower() for m in cm.output))

    def test_prod_adjusted_none_when_ratio_negative_and_logs_warning(self):
        """R372-7: negative ratio is invalid and should warn + return None."""
        y = np.array([1, 1, 0, 0] * 25, dtype=int)
        scores = np.array([0.9, 0.8, 0.6, 0.2] * 25, dtype=float)
        x, y_s = _make_xy(y, scores)
        with self.assertLogs("trainer", level="WARNING") as cm:
            out = trainer_mod._compute_test_metrics(
                _FixedScoreModel(scores), 0.5, x, y_s, log_results=False, production_neg_pos_ratio=-5.0
            )
        self.assertIsNone(out["test_precision_prod_adjusted"])
        self.assertTrue(any("invalid" in m.lower() for m in cm.output))

    def test_prod_adjusted_prec_one_stays_one(self):
        """R372-8: precision=1.0 remains 1.0 after prior-ratio adjustment."""
        y = np.array([1] * 50 + [0] * 50, dtype=int)
        scores = np.array([0.95] * 30 + [0.2] * 20 + [0.1] * 50, dtype=float)
        x, y_s = _make_xy(y, scores)
        out = trainer_mod._compute_test_metrics(
            _FixedScoreModel(scores), 0.5, x, y_s, log_results=False, production_neg_pos_ratio=15.0
        )
        self.assertAlmostEqual(out["test_precision"], 1.0)
        self.assertAlmostEqual(out["test_precision_prod_adjusted"], 1.0)

    def test_prod_adjusted_prec_zero_stays_none(self):
        """R372-9: no predicted positives => precision=0 and adjusted precision stays None."""
        y = np.array([1] * 50 + [0] * 50, dtype=int)
        scores = np.array([0.2] * 100, dtype=float)  # threshold 0.5 => no positive predictions
        x, y_s = _make_xy(y, scores)
        out = trainer_mod._compute_test_metrics(
            _FixedScoreModel(scores), 0.5, x, y_s, log_results=False, production_neg_pos_ratio=15.0
        )
        self.assertEqual(out["test_precision"], 0.0)
        self.assertIsNone(out["test_precision_prod_adjusted"])

    def test_prod_adjusted_works_without_neg_sample_frac_param(self):
        """R372-10: regression guard — function no longer depends on neg_sample_frac argument."""
        sig = inspect.signature(trainer_mod._compute_test_metrics)
        self.assertNotIn("neg_sample_frac", sig.parameters)

        y = np.array([1] * 50 + [0] * 50, dtype=int)
        scores = np.array([0.9] * 20 + [0.1] * 30 + [0.9] * 20 + [0.1] * 30, dtype=float)
        x, y_s = _make_xy(y, scores)
        out = trainer_mod._compute_test_metrics(
            _FixedScoreModel(scores), 0.5, x, y_s, log_results=False, production_neg_pos_ratio=15.0
        )
        self.assertIsNotNone(out["test_precision_prod_adjusted"])

    def test_training_metrics_json_has_production_ratio_key(self):
        """R372-11: artifact metadata should include production_neg_pos_ratio key."""
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertIn(
            '"production_neg_pos_ratio"',
            src,
            "save_artifact_bundle should write production_neg_pos_ratio to training_metrics.json",
        )


if __name__ == "__main__":
    unittest.main()
