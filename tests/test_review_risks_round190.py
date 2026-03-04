"""Minimal reproducible guardrail tests for Round 53 review risks (R700-R706).

Tests-only round: no production code changes.
Known unfixed risks are encoded as expected failures so they remain visible
without blocking the full suite.
"""

from __future__ import annotations

import inspect
import unittest

import numpy as np
import pandas as pd

import trainer.trainer as trainer_mod


class TestR700TrainEndSemanticDrift(unittest.TestCase):
    """R700: chunk-level train_end may drift from row-level split boundary."""

    def test_run_pipeline_should_compare_chunk_vs_row_train_end(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "_actual_train_end",
            src,
            "run_pipeline should compute row-level actual train end and compare to chunk-level train_end.",
        )


class TestR701RunBoundarySplitLeakage(unittest.TestCase):
    """R701: row-level split may cut a single run across train/valid/test."""

    def test_split_logic_should_include_run_boundary_guard(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertTrue(
            ("run_id" in src and "group" in src.lower()) or ("same run" in src.lower()),
            "row-level split should include a run-boundary guard to avoid splitting the same run.",
        )


class TestR702NaNValidationLabels(unittest.TestCase):
    """R702: _train_one_model should not crash when y_val is all NaN."""

    def test_train_one_model_all_nan_labels_no_crash(self):
        rng = np.random.default_rng(42)
        X_tr = pd.DataFrame(rng.normal(size=(80, 5)), columns=[f"f{i}" for i in range(5)])
        y_tr = pd.Series(([0, 1] * 40), dtype="int64")
        sw_tr = pd.Series(np.ones(len(X_tr)), dtype="float64")

        # Ensure fallback path: len(y_val) >= MIN_VALID_TEST_ROWS but sum -> 0 due to all NaN.
        X_vl = pd.DataFrame(rng.normal(size=(60, 5)), columns=X_tr.columns)
        y_vl = pd.Series([np.nan] * 60, dtype="float64")

        model, metrics = trainer_mod._train_one_model(
            X_train=X_tr,
            y_train=y_tr,
            X_val=X_vl,
            y_val=y_vl,
            sw_train=sw_tr,
            hyperparams={},
            label="nan-val-guard",
        )
        self.assertIsNotNone(model)
        self.assertEqual(metrics.get("val_prauc"), 0.0)
        self.assertEqual(metrics.get("threshold"), 0.5)


class TestR703UncalibratedThresholdMetadata(unittest.TestCase):
    """R703: uncalibrated threshold (0.5 fallback) should be explicitly marked."""

    def test_save_artifact_bundle_should_mark_uncalibrated_threshold(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertIn(
            "uncalibrated_threshold",
            src,
            "artifact metadata should mark uncalibrated threshold fallback.",
        )


class TestR704SplitSortMemoryPattern(unittest.TestCase):
    """R704: split sort currently creates multiple intermediate DataFrame copies."""

    def test_run_pipeline_split_sort_should_prefer_inplace_operations(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        # Desired future pattern: inplace sort/drop/reset to reduce memory peaks.
        self.assertIn("inplace=True", src)


class TestR705OptunaEmptyValGuard(unittest.TestCase):
    """R705: run_optuna_search should be safe on empty validation input."""

    def test_run_optuna_search_empty_val_should_not_raise(self):
        rng = np.random.default_rng(123)
        X_tr = pd.DataFrame(rng.normal(size=(50, 4)), columns=list("abcd"))
        y_tr = pd.Series(([0, 1] * 25), dtype="int64")
        sw_tr = pd.Series(np.ones(len(X_tr)), dtype="float64")
        X_vl = X_tr.head(0).copy()
        y_vl = y_tr.head(0).copy()

        _ = trainer_mod.run_optuna_search(
            X_train=X_tr,
            y_train=y_tr,
            X_val=X_vl,
            y_val=y_vl,
            sw_train=sw_tr,
            n_trials=1,
            label="empty-val-guard",
        )


class TestR706DefensiveTzStripGuardrail(unittest.TestCase):
    """R706: keep defensive tz strip in run_pipeline split path."""

    def test_run_pipeline_keeps_defensive_tz_strip(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "if _payout_ts.dt.tz is not None:",
            src,
            "run_pipeline should keep defensive tz strip before row-level split sorting.",
        )


if __name__ == "__main__":
    unittest.main()

