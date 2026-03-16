"""Minimal reproducible tests for Round 186 Review — 方案 B 匯出 CSV/TSV.

Review risks (Round 186 Review in STATUS.md) are turned into contract/behavior tests.
Tests that document desired behaviour not yet in production use @unittest.expectedFailure.
Tests-only: no production code changes.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from trainer.trainer import _export_train_valid_to_csv


def _minimal_train_valid(feature_cols_train: list, feature_cols_valid: list, is_rated_train, is_rated_valid):
    """Build minimal train_df and valid_df with given columns and is_rated masks."""
    n_train = len(is_rated_train)
    n_valid = len(is_rated_valid)
    train_df = pd.DataFrame(
        {c: [0.0] * n_train for c in feature_cols_train},
        columns=feature_cols_train,
    )
    train_df["label"] = [0] * n_train
    train_df["is_rated"] = list(is_rated_train)
    train_df["canonical_id"] = ["C0"] * n_train
    train_df["run_id"] = [1] * n_train

    valid_df = pd.DataFrame(
        {c: [0.0] * n_valid for c in feature_cols_valid},
        columns=feature_cols_valid,
    )
    valid_df["label"] = [0] * n_valid
    valid_df["is_rated"] = list(is_rated_valid)
    return train_df, valid_df


# ---------------------------------------------------------------------------
# §1 — train/valid 欄位不一致導致 Step 9 無法對齊
# ---------------------------------------------------------------------------

class TestR186ExportTrainValidCommonColumnsContract(unittest.TestCase):
    """Round 186 Review §1: Exported train and valid CSV should have same feature set (for Step 9)."""

    def test_exported_train_and_valid_have_same_feature_columns(self):
        """When valid has fewer feature columns than train, export should use common cols only (Round 186 §1)."""
        train_df, valid_df = _minimal_train_valid(
            ["f1", "f2"],
            ["f1"],
            [True, True],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d)
            _export_train_valid_to_csv(train_df, valid_df, ["f1", "f2"], export_dir)
            train_path = export_dir / "train_for_lgb.csv"
            valid_path = export_dir / "valid_for_lgb.csv"
            train_header = train_path.read_text(encoding="utf-8").splitlines()[0].split(",")
            valid_header = valid_path.read_text(encoding="utf-8").splitlines()[0].split(",")
            # Train has ... + label + weight; valid has ... + label. Feature set should match.
            train_features = [h for h in train_header if h not in ("label", "weight")]
            valid_features = [h for h in valid_header if h != "label"]
            self.assertEqual(
                set(train_features),
                set(valid_features),
                "Train and valid CSV should have same feature columns for Step 9 (Round 186 §1).",
            )


class TestR186ExportWhenValidHasFewerColumns(unittest.TestCase):
    """Round 186 Review §1: When valid has fewer feature columns, export uses common cols only."""

    def test_export_succeeds_and_produces_expected_headers(self):
        """With common_cols (P1): train and valid both get only f1; train has weight, valid does not."""
        train_df, valid_df = _minimal_train_valid(
            ["f1", "f2"],
            ["f1"],
            [True, True],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d)
            t_path, v_path = _export_train_valid_to_csv(
                train_df, valid_df, ["f1", "f2"], export_dir
            )
            self.assertTrue(t_path.exists())
            self.assertTrue(v_path.exists())
            train_header = t_path.read_text(encoding="utf-8").splitlines()[0]
            valid_header = v_path.read_text(encoding="utf-8").splitlines()[0]
            self.assertIn("f1", train_header)
            self.assertIn("weight", train_header)
            self.assertNotIn("f2", train_header)
            self.assertIn("f1", valid_header)
            self.assertNotIn("f2", valid_header)
            self.assertNotIn("weight", valid_header)


# ---------------------------------------------------------------------------
# §2 — is_rated 篩選：僅 rated 列匯出
# ---------------------------------------------------------------------------

class TestR186ExportRatedOnly(unittest.TestCase):
    """Round 186 Review §2: Only rows with is_rated truthy are exported."""

    def test_exported_train_row_count_matches_rated_count(self):
        """is_rated [True, False, True] => 2 rows in train CSV (Round 186 §2)."""
        train_df, valid_df = _minimal_train_valid(
            ["f1"],
            ["f1"],
            [True, False, True],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            t_path, _ = _export_train_valid_to_csv(
                train_df, valid_df, ["f1"], Path(d)
            )
            lines = t_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3, "1 header + 2 data rows")
            self.assertEqual(lines[0], "f1,label,weight")


# ---------------------------------------------------------------------------
# §4 — feature_cols 含重複時產出重複欄名
# ---------------------------------------------------------------------------

class TestR186ExportNoDuplicateHeaderColumns(unittest.TestCase):
    """Round 186 Review §4: CSV header should not contain duplicate column names."""

    def test_exported_csv_header_has_no_duplicate_columns(self):
        """feature_cols = ['f1','f1','f2'] => header should have unique names (Round 186 §4)."""
        train_df, valid_df = _minimal_train_valid(
            ["f1", "f2"],
            ["f1", "f2"],
            [True],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            _export_train_valid_to_csv(
                train_df, valid_df, ["f1", "f1", "f2"], Path(d)
            )
            train_path = Path(d) / "train_for_lgb.csv"
            header = train_path.read_text(encoding="utf-8").splitlines()[0].split(",")
            self.assertEqual(
                len(header),
                len(set(header)),
                "CSV header must not contain duplicate column names (Round 186 §4).",
            )


# ---------------------------------------------------------------------------
# §5 — 空 train_rated / valid_rated
# ---------------------------------------------------------------------------

class TestR186ExportEmptyRated(unittest.TestCase):
    """Round 186 Review §5: Empty rated set produces header-only CSV, no crash."""

    def test_empty_train_rated_produces_header_only_train_csv(self):
        """train_df all is_rated False => train_for_lgb.csv has only header (Round 186 §5)."""
        train_df, valid_df = _minimal_train_valid(
            ["f1"],
            ["f1"],
            [False, False],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            t_path, v_path = _export_train_valid_to_csv(
                train_df, valid_df, ["f1"], Path(d)
            )
            train_lines = t_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(train_lines), 1, "Only header line")
            self.assertIn("f1", train_lines[0])
            self.assertIn("weight", train_lines[0])
            valid_lines = v_path.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(valid_lines), 1)

    def test_empty_valid_rated_produces_header_only_valid_csv(self):
        """valid_df all is_rated False => valid_for_lgb.csv has only header (Round 186 §5)."""
        train_df, valid_df = _minimal_train_valid(
            ["f1"],
            ["f1"],
            [True],
            [False, False],
        )
        with tempfile.TemporaryDirectory() as d:
            t_path, v_path = _export_train_valid_to_csv(
                train_df, valid_df, ["f1"], Path(d)
            )
            valid_lines = v_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(valid_lines), 1, "Only header line")
            self.assertIn("f1", valid_lines[0])
            self.assertNotIn("weight", valid_lines[0])


# ---------------------------------------------------------------------------
# Config / return contract
# ---------------------------------------------------------------------------

class TestR186ExportReturnPaths(unittest.TestCase):
    """_export_train_valid_to_csv returns (train_path, valid_path) and files exist."""

    def test_returns_two_paths_and_files_exist(self):
        """Return value is (train_path, valid_path); both files exist."""
        train_df, valid_df = _minimal_train_valid(["f1"], ["f1"], [True], [True])
        with tempfile.TemporaryDirectory() as d:
            t_path, v_path = _export_train_valid_to_csv(
                train_df, valid_df, ["f1"], Path(d)
            )
            self.assertEqual(t_path, Path(d) / "train_for_lgb.csv")
            self.assertEqual(v_path, Path(d) / "valid_for_lgb.csv")
            self.assertTrue(t_path.exists())
            self.assertTrue(v_path.exists())


if __name__ == "__main__":
    unittest.main()
