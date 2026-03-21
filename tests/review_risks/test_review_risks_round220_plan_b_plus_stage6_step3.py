"""Minimal reproducible tests for Round 220 Review — 方案 B+ 階段 6 第 3 步審查風險點.

Round 220 Review (STATUS.md) risk points are turned into contract/behavior tests.
Tests-only: no production code changes.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np


# Expected keys from _compute_test_metrics / _compute_test_metrics_from_scores (R220 Review #6)
_EXPECTED_TEST_METRICS_KEYS = frozenset({
    "test_ap",
    "test_precision",
    "test_recall",
    "test_f1",
    "test_samples",
    "test_positives",
    "test_random_ap",
    "test_threshold_uncalibrated",
    "test_precision_at_recall_0.001",
    "test_precision_at_recall_0.01",
    "test_precision_at_recall_0.1",
    "test_precision_at_recall_0.5",
    "threshold_at_recall_0.001",
    "threshold_at_recall_0.01",
    "threshold_at_recall_0.1",
    "threshold_at_recall_0.5",
    "n_alerts_at_recall_0.001",
    "n_alerts_at_recall_0.01",
    "n_alerts_at_recall_0.1",
    "n_alerts_at_recall_0.5",
    "alerts_per_minute_at_recall_0.001",
    "alerts_per_minute_at_recall_0.01",
    "alerts_per_minute_at_recall_0.1",
    "alerts_per_minute_at_recall_0.5",
    "test_precision_at_recall_0.001_prod_adjusted",
    "test_precision_at_recall_0.01_prod_adjusted",
    "test_precision_at_recall_0.1_prod_adjusted",
    "test_precision_at_recall_0.5_prod_adjusted",
    "test_precision_prod_adjusted",
    "test_neg_pos_ratio",
    "production_neg_pos_ratio_assumed",
})


def _minimal_parquet(
    dir_path: Path,
    filename: str,
    *,
    label: int | float = 0,
    is_rated: bool = True,
    n_rows: int = 1,
) -> Path:
    """Minimal Parquet with label, is_rated, canonical_id, run_id, f1."""
    import pandas as pd

    df = pd.DataFrame(
        {
            "label": [label] * n_rows,
            "is_rated": [is_rated] * n_rows,
            "canonical_id": ["C0"] * n_rows,
            "run_id": [1] * n_rows,
            "f1": [0.0] * n_rows,
        }
    )
    out = dir_path / filename
    df.to_parquet(out, index=False)
    return out


# ---------------------------------------------------------------------------
# R220 Review #1 — test LibSVM 0 行：test_path 存在但全 is_rated=False
# ---------------------------------------------------------------------------

class TestR220ExportTestZeroRated(unittest.TestCase):
    """R220 Review #1: When test_path exists but test Parquet has 0 is_rated rows, export succeeds and test_for_lgb.libsvm is empty."""

    def test_export_with_test_path_all_unrated_produces_empty_test_libsvm(self):
        """_export_parquet_to_libsvm(..., test_path=path_to_all_unrated) does not raise; test_for_lgb.libsvm exists and has 0 lines."""
        from trainer.trainer import _export_parquet_to_libsvm

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p = _minimal_parquet(root, "train.parquet", n_rows=2)
            valid_p = _minimal_parquet(root, "valid.parquet", n_rows=1)
            test_p = _minimal_parquet(root, "test.parquet", is_rated=False, n_rows=3)
            train_libsvm, valid_libsvm, test_libsvm = _export_parquet_to_libsvm(
                train_p, valid_p, ["f1"], root, test_path=test_p
            )
            self.assertIsNotNone(test_libsvm, "test_path was provided and exists; third return must be Path")
            self.assertTrue(test_libsvm.exists(), "test LibSVM file should exist")
            lines = test_libsvm.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 0, "R220 #1: test Parquet had 0 is_rated rows; test_for_lgb.libsvm must have 0 lines")


# ---------------------------------------------------------------------------
# R220 Review #3 — test_path 不為 None 但 exists() 為 False
# ---------------------------------------------------------------------------

class TestR220ExportTestPathNonexistent(unittest.TestCase):
    """R220 Review #3: When test_path is not None but file does not exist, export does not raise and returns third element None."""

    def test_export_with_nonexistent_test_path_returns_none_no_test_file(self):
        """_export_parquet_to_libsvm(..., test_path=nonexistent_path) does not raise; return[2] is None; test_for_lgb.libsvm not created."""
        from trainer.trainer import _export_parquet_to_libsvm

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            train_p = _minimal_parquet(root, "train.parquet")
            valid_p = _minimal_parquet(root, "valid.parquet")
            nonexistent = root / "nonexistent_test.parquet"
            self.assertFalse(nonexistent.exists(), "sanity: test path must not exist")
            train_libsvm, valid_libsvm, test_libsvm = _export_parquet_to_libsvm(
                train_p, valid_p, ["f1"], root, test_path=nonexistent
            )
            self.assertIsNone(test_libsvm, "R220 #3: when test_path exists() is False, third return must be None")
            test_file = root / "test_for_lgb.libsvm"
            self.assertFalse(test_file.exists(), "R220 #3: should not create test_for_lgb.libsvm when test_path does not exist")


# ---------------------------------------------------------------------------
# R220 Review #6 — _compute_test_metrics_from_scores 與 _compute_test_metrics 鍵一致
# ---------------------------------------------------------------------------

class TestR220ComputeTestMetricsFromScoresKeys(unittest.TestCase):
    """R220 Review #6: _compute_test_metrics_from_scores return keys must match expected test metrics key set."""

    def test_from_scores_return_has_expected_keys(self):
        """_compute_test_metrics_from_scores returns a dict with exactly the same keys as _compute_test_metrics (contract)."""
        from trainer.trainer import _compute_test_metrics_from_scores

        # Use inputs that yield full (non-zeroed) metrics: >= MIN_VALID_TEST_ROWS, both classes present
        np.random.seed(42)
        n = 60
        y = np.array([0] * 30 + [1] * 30, dtype=np.float64)
        scores = np.random.rand(n).astype(np.float64)
        out = _compute_test_metrics_from_scores(
            y, scores, threshold=0.5, label="rated", log_results=False
        )
        self.assertIsInstance(out, dict)
        self.assertEqual(frozenset(out.keys()), _EXPECTED_TEST_METRICS_KEYS,
                         "R220 #6: _compute_test_metrics_from_scores must return same keys as _compute_test_metrics")

    def test_from_scores_zeroed_return_has_expected_keys(self):
        """When test is too small/unbalanced, from_scores zeroed return still has the same key set."""
        from trainer.trainer import _compute_test_metrics_from_scores

        y = np.array([0.0, 1.0])  # too small
        scores = np.array([0.3, 0.7])
        out = _compute_test_metrics_from_scores(
            y, scores, threshold=0.5, label="rated", log_results=False
        )
        self.assertEqual(frozenset(out.keys()), _EXPECTED_TEST_METRICS_KEYS,
                         "R220 #6: zeroed return must have same keys")


# ---------------------------------------------------------------------------
# R220 Review #2 — test label/predict 長度不一致時 trim 且不崩潰
# ---------------------------------------------------------------------------

class TestR220ComputeTestMetricsFromScoresTrimLength(unittest.TestCase):
    """R220 Review #2: When len(y_test) != len(test_scores), _compute_test_metrics_from_scores trims and does not crash."""

    def test_from_scores_length_mismatch_trims_and_returns(self):
        """_compute_test_metrics_from_scores with len(y)=5, len(scores)=7 trims to 5 and returns valid dict."""
        from trainer.trainer import _compute_test_metrics_from_scores

        y = np.array([0, 1, 0, 1, 0], dtype=np.float64)
        scores = np.array([0.1, 0.9, 0.2, 0.8, 0.3, 0.7, 0.4], dtype=np.float64)  # length 7
        out = _compute_test_metrics_from_scores(
            y, scores, threshold=0.5, label="rated", log_results=False
        )
        self.assertEqual(frozenset(out.keys()), _EXPECTED_TEST_METRICS_KEYS)
        self.assertEqual(out["test_samples"], 5, "R220 #2: must use min(5,7)=5 rows after trim")

    def test_from_scores_length_mismatch_scores_shorter_trims(self):
        """_compute_test_metrics_from_scores with len(y)=4, len(scores)=2 trims to 2 and returns (may zero if too small)."""
        from trainer.trainer import _compute_test_metrics_from_scores

        y = np.array([0, 1, 0, 1], dtype=np.float64)
        scores = np.array([0.2, 0.8], dtype=np.float64)
        out = _compute_test_metrics_from_scores(
            y, scores, threshold=0.5, label="rated", log_results=False
        )
        self.assertEqual(frozenset(out.keys()), _EXPECTED_TEST_METRICS_KEYS)
        self.assertEqual(out["test_samples"], 2, "R220 #2: must use min(4,2)=2 rows after trim")


if __name__ == "__main__":
    unittest.main()
