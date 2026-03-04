"""Minimal reproducible guardrail tests for Round 58 review risks (R800-R806).

All risks have been addressed in Round 60 production-code fixes; @expectedFailure
decorators have been removed and these tests now run as active guardrails.
"""

from __future__ import annotations

import inspect
import unittest

import numpy as np
import pandas as pd

import trainer.features as features_mod
import trainer.trainer as trainer_mod


class TestR800JoinProfileNaTRowAlignment(unittest.TestCase):
    """R800: dropna in join path must not break row-aligned assignment."""

    def test_join_player_profile_daily_nat_bet_time_should_not_crash(self):
        bets = pd.DataFrame(
            {
                "canonical_id": ["c1", "c1"],
                "payout_complete_dtm": [pd.NaT, pd.Timestamp("2026-02-10 10:00:00")],
            }
        )
        profile = pd.DataFrame(
            {
                "canonical_id": ["c1"],
                "snapshot_dtm": [pd.Timestamp("2026-02-10 09:00:00")],
                "turnover_7d": [123.0],
            }
        )

        out = features_mod.join_player_profile_daily(
            bets_df=bets,
            profile_df=profile,
            feature_cols=["turnover_7d"],
        )
        self.assertEqual(len(out), 2)
        self.assertTrue(pd.isna(out.loc[0, "turnover_7d"]))
        self.assertAlmostEqual(float(out.loc[1, "turnover_7d"]), 123.0, places=6)


class TestR801PartialNaNValidationLabels(unittest.TestCase):
    """R801: _has_val guard must reject y_val containing NaN labels."""

    def test_train_one_model_partial_nan_labels_should_not_raise(self):
        rng = np.random.default_rng(42)
        X_tr = pd.DataFrame(rng.normal(size=(120, 5)), columns=[f"f{i}" for i in range(5)])
        y_tr = pd.Series(([0, 1] * 60), dtype="int64")
        sw_tr = pd.Series(np.ones(len(X_tr)), dtype="float64")

        X_vl = pd.DataFrame(rng.normal(size=(60, 5)), columns=X_tr.columns)
        y_vl = pd.Series(([1] + [np.nan] * 30 + [0] * 29), dtype="float64")

        model, metrics = trainer_mod._train_one_model(
            X_train=X_tr,
            y_train=y_tr,
            X_val=X_vl,
            y_val=y_vl,
            sw_train=sw_tr,
            hyperparams={},
            label="partial-nan-val",
        )
        self.assertIsNotNone(model)
        self.assertIn("threshold", metrics)


class TestR802FullDfReleaseGuard(unittest.TestCase):
    """R802: split path should release full_df to reduce RAM peak."""

    def test_run_pipeline_should_release_full_df_after_split(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "del full_df",
            src,
            "run_pipeline should explicitly release full_df after train/valid/test copies.",
        )


class TestR803SplitFracValidation(unittest.TestCase):
    """R803: split fractions should be validated (< 1.0 total)."""

    def test_run_pipeline_should_validate_split_fraction_sum(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "TRAIN_SPLIT_FRAC + VALID_SPLIT_FRAC < 1.0",
            src,
            "run_pipeline should validate train+valid split fractions leave room for test.",
        )


class TestR804UncalibratedFlagSource(unittest.TestCase):
    """R804: uncalibrated flag should track fallback code-path, not threshold value."""

    def test_save_artifact_bundle_should_not_detect_uncalibrated_by_eq_05(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertNotIn(
            'get("threshold")    == 0.5',
            src,
            "uncalibrated flag should not rely on threshold == 0.5 value matching.",
        )


class TestR805SplitTimerLabeling(unittest.TestCase):
    """R805: timing label should reflect load/sort/split scope."""

    def test_run_pipeline_split_log_should_label_load_sort_split(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "load+sort+split",
            src,
            "timing log should explicitly say load+sort+split, not just split.",
        )


class TestR806R700TimestampConstruction(unittest.TestCase):
    """R806: avoid string round-trip when constructing Timestamp from train_end."""

    def test_run_pipeline_should_avoid_timestamp_string_roundtrip(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertNotIn(
            "pd.Timestamp(str(train_end))",
            src,
            "run_pipeline should use pd.Timestamp(train_end) directly.",
        )


if __name__ == "__main__":
    unittest.main()
