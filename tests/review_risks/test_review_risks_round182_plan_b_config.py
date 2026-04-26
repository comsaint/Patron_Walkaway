"""Minimal reproducible tests for Round 182 Review — 方案 B Config + Step 9 接線.

Review risks (Round 182 Review in STATUS.md) are turned into contract/config tests.
Tests that document desired behaviour not yet in production use @unittest.expectedFailure.
Tests-only: no production code changes.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import trainer.config as config_mod
import trainer.trainer as trainer_mod


def _minimal_train_valid_dfs():
    """Minimal DataFrames for train_single_rated_model contract tests."""
    train_df = pd.DataFrame(
        {
            "is_rated": [True, True, True, True],
            "label": [1, 0, 0, 1],
            "f0": [0.1, 0.2, 0.3, 0.4],
        }
    )
    valid_df = train_df.copy()
    return train_df, valid_df


class TestR182TrainFromFileReturnStructure(unittest.TestCase):
    """Round 182 Review P1: train_from_file=True must return same structure as False."""

    def test_train_from_file_true_returns_same_structure_as_false(self):
        """train_single_rated_model(..., train_from_file=True) returns (rated_art, None, metrics) with same keys as train_from_file=False."""
        train_df, valid_df = _minimal_train_valid_dfs()
        feature_cols = ["f0"]
        with patch.object(
            trainer_mod,
            "_train_one_model",
            return_value=(object(), {"threshold": 0.5, "val_f1": 0.0, "_uncalibrated": True}),
        ), patch.object(
            trainer_mod,
            "_compute_train_metrics",
            return_value={"train_ap": 0.0, "train_random_ap": 0.0},
        ), patch.object(
            trainer_mod,
            "_compute_feature_importance",
            return_value=[{"rank": 1, "feature": "f0", "importance_gain_pct": 100.0}],
        ), patch.object(
            trainer_mod,
            "_compute_test_metrics",
            return_value={"test_ap": 0.0},
        ), patch.object(trainer_mod, "A4_TWO_STAGE_ENABLE_TRAINING", False):
            out_false = trainer_mod.train_single_rated_model(
                train_df=train_df,
                valid_df=valid_df,
                feature_cols=feature_cols,
                run_optuna=False,
                test_df=None,
                train_from_file=False,
            )
            out_true = trainer_mod.train_single_rated_model(
                train_df=train_df,
                valid_df=valid_df,
                feature_cols=feature_cols,
                run_optuna=False,
                test_df=None,
                train_from_file=True,
            )
        self.assertIsNone(out_false[1])
        self.assertIsNone(out_true[1])
        self.assertEqual(set(out_false[2].keys()), set(out_true[2].keys()))
        if out_false[0] is not None and out_true[0] is not None:
            self.assertEqual(set(out_false[0].keys()), set(out_true[0].keys()))


class TestR182PlanBConfigConstants(unittest.TestCase):
    """Round 182 Review: 方案 B config constants exist and have expected types."""

    def test_step9_train_from_file_exists_and_is_bool(self):
        """STEP9_TRAIN_FROM_FILE must exist and be bool."""
        self.assertTrue(hasattr(config_mod, "STEP9_TRAIN_FROM_FILE"))
        self.assertIsInstance(getattr(config_mod, "STEP9_TRAIN_FROM_FILE"), bool)

    def test_step8_screen_sample_rows_exists_and_is_optional_int(self):
        """STEP8_SCREEN_SAMPLE_ROWS must exist and be None or int."""
        self.assertTrue(hasattr(config_mod, "STEP8_SCREEN_SAMPLE_ROWS"))
        val = getattr(config_mod, "STEP8_SCREEN_SAMPLE_ROWS")
        self.assertTrue(val is None or isinstance(val, int))

    def test_step8_screen_sample_strategy_exists_and_is_str(self):
        """STEP8_SCREEN_SAMPLE_STRATEGY must exist and be a non-empty str."""
        self.assertTrue(hasattr(config_mod, "STEP8_SCREEN_SAMPLE_STRATEGY"))
        val = getattr(config_mod, "STEP8_SCREEN_SAMPLE_STRATEGY")
        self.assertIsInstance(val, str)
        self.assertGreater(len(val.strip()), 0)


class TestR182Step8SampleRowsCommentContract(unittest.TestCase):
    """Round 182 Review P2: config comment should document STEP8_SCREEN_SAMPLE_ROWS > 0."""

    def test_config_comment_mentions_positive_or_gt_zero_for_step8_sample_rows(self):
        """Implementation comment for STEP8_SCREEN_SAMPLE_ROWS should state integer must be > 0."""
        path = Path(__file__).resolve().parents[2] / "trainer" / "core" / "_config_training_memory.py"
        source = path.read_text(encoding="utf-8")
        start = source.find("STEP8_SCREEN_SAMPLE_ROWS")
        self.assertGreater(start, -1, "STEP8_SCREEN_SAMPLE_ROWS not found in trainer/core/_config_training_memory.py")
        # Look at the comment block above the constant (same or previous lines)
        block = source[max(0, start - 800) : start + 200]
        has_positive_guard = (
            "> 0" in block
            or "positive" in block.lower()
            or "須 > 0" in block
            or "must be > 0" in block.lower()
            or ">0" in block
        )
        self.assertTrue(
            has_positive_guard,
            "Comment for STEP8_SCREEN_SAMPLE_ROWS should state integer must be > 0 (Round 182 Review P2).",
        )


if __name__ == "__main__":
    unittest.main()
