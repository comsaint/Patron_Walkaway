"""Minimal reproducible tests for Round 199 Review — 方案 B 從檔案分支邊界條件.

Round 199 Review risk points (STATUS.md) are turned into contract/behavior tests.
Tests that assert desired behaviour not yet in production use @unittest.expectedFailure.
Tests-only: no production code changes.
"""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

import trainer.trainer as trainer_mod
from trainer.trainer import _export_train_valid_to_csv, train_single_rated_model


def _make_train_rated(n: int, feature_cols: list[str], seed: int = 42) -> pd.DataFrame:
    """Train DataFrame with label 0/1 mix, is_rated, canonical_id, run_id."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {c: rng.random(n).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    df["label"] = (rng.random(n) > 0.5).astype(int)
    df.loc[0, "label"] = 0
    df.loc[min(1, n - 1), "label"] = 1
    df["is_rated"] = True
    df["canonical_id"] = ["C0"] * n
    df["run_id"] = list(range(n))
    return df


# ---------------------------------------------------------------------------
# R199 Review #1 — 邊界：val_rated 缺 _train_feature_cols 時不應 KeyError（中）
# ---------------------------------------------------------------------------

class TestR199FromFileValidMissingFeatureColsNoKeyError(unittest.TestCase):
    """Round 199 #1: When CSV has f1,f2 but valid_df only has f1, from-file path must not raise KeyError."""

    def test_from_file_when_valid_has_fewer_columns_than_csv_completes_without_key_error(self):
        """CSV exported with f1,f2; call with valid_df that has only f1 → train_single_rated_model(..., train_from_file=True) must not raise KeyError."""
        train_df = _make_train_rated(60, ["f1", "f2"], seed=42)
        valid_full = pd.DataFrame(
            {"f1": np.random.default_rng(43).random(30), "f2": np.random.default_rng(44).random(30)}
        )
        valid_full["label"] = (np.random.default_rng(45).random(30) > 0.5).astype(int)
        valid_full.loc[0, "label"] = 0
        valid_full.loc[1, "label"] = 1
        valid_full["is_rated"] = True

        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_full, ["f1", "f2"], export_dir)
            valid_one_col = valid_full[["f1", "label", "is_rated"]].copy()

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                rated_art, _, _ = train_single_rated_model(
                    train_df,
                    valid_one_col,
                    ["f1", "f2"],
                    run_optuna=False,
                    test_df=None,
                    train_from_file=True,
                )
        self.assertIsNotNone(rated_art, "R199 #1: from-file with valid missing cols should complete without KeyError.")


# ---------------------------------------------------------------------------
# R199 Review #2 — 邊界：common_cols 為空時 export 應拋出或明確拒絕（低）
# ---------------------------------------------------------------------------

class TestR199ExportEmptyCommonColsRaisesOrRejects(unittest.TestCase):
    """Round 199 #2: When train/valid have no common feature columns, export should raise or not produce invalid CSV."""

    def test_export_with_empty_common_cols_raises_value_error(self):
        """Train has only f1, valid has only f2 → _export_train_valid_to_csv should raise ValueError (no invalid CSV)."""
        train_df = pd.DataFrame({"f1": [0.1, 0.2], "label": [0, 1], "is_rated": [True, True]})
        train_df["canonical_id"] = ["C0", "C0"]
        train_df["run_id"] = [0, 1]
        valid_df = pd.DataFrame({"f2": [0.3, 0.4], "label": [0, 1], "is_rated": [True, True]})
        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            with self.assertRaises(ValueError):
                _export_train_valid_to_csv(train_df, valid_df, ["f1", "f2"], export_dir)


if __name__ == "__main__":
    unittest.main()
