"""Round 186 Review — LibSVM export from Parquet (replaces retired Plan B CSV export tests)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import trainer.trainer as trainer_mod


def _minimal_train_valid(
    feature_cols_train: list[str],
    feature_cols_valid: list[str],
    is_rated_train: list[bool],
    is_rated_valid: list[bool],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    n_train = len(is_rated_train)
    n_valid = len(is_rated_valid)
    train_df = pd.DataFrame(
        {c: [0.0] * n_train for c in feature_cols_train},
        columns=feature_cols_train,
    )
    train_df["label"] = [0] * n_train
    train_df["is_rated"] = list(is_rated_train)
    train_df["canonical_id"] = ["C0"] * n_train
    train_df["run_id"] = list(range(n_train))

    valid_df = pd.DataFrame(
        {c: [0.0] * n_valid for c in feature_cols_valid},
        columns=feature_cols_valid,
    )
    valid_df["label"] = [0] * n_valid
    valid_df["is_rated"] = list(is_rated_valid)
    valid_df["canonical_id"] = ["C0"] * n_valid
    valid_df["run_id"] = list(range(n_valid))
    return train_df, valid_df


def _export_via_parquet(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    feature_cols: list[str],
    root: Path,
) -> tuple[Path, Path, Path]:
    export_dir = root / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    pq = root / "_pq"
    pq.mkdir(exist_ok=True)
    trp, vlp = pq / "train.parquet", pq / "valid.parquet"
    train_df.to_parquet(trp, index=False)
    valid_df.to_parquet(vlp, index=False)
    tr_l, va_l, _ = trainer_mod._export_parquet_to_libsvm(
        trp, vlp, feature_cols, export_dir, test_path=None
    )
    shutil.rmtree(pq, ignore_errors=True)
    return tr_l, va_l, export_dir


def _libsvm_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8").strip()
    return [ln for ln in text.splitlines() if ln.strip()]


class TestR186LibSvmTrainValidSameFeatureDim(unittest.TestCase):
    """Round 186 §1: Subset feature_cols yields aligned train/valid LibSVM dimensions."""

    def test_subset_feature_cols_train_valid_same_sparse_width(self):
        train_df, valid_df = _minimal_train_valid(
            ["f1", "f2"],
            ["f1", "f2"],
            [True, True],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l, _ = _export_via_parquet(train_df, valid_df, ["f1"], root)
            tr_lines = _libsvm_lines(tr_l)
            va_lines = _libsvm_lines(va_l)
            self.assertEqual(len(tr_lines), 2)
            self.assertEqual(len(va_lines), 1)
            # Same highest feature index (0-only) for both files
            for ln in tr_lines + va_lines:
                idxs = [int(p.split(":", 1)[0]) for p in ln.split()[1:]]
                if idxs:
                    self.assertEqual(max(idxs), 0, "single-feature export should use index 0 only")


class TestR186LibSvmRatedOnly(unittest.TestCase):
    """Round 186 §2: Only is_rated rows are written to LibSVM."""

    def test_exported_train_row_count_matches_rated_count(self):
        train_df, valid_df = _minimal_train_valid(
            ["f1"],
            ["f1"],
            [True, False, True],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            tr_l, _, _ = _export_via_parquet(train_df, valid_df, ["f1"], Path(d))
            self.assertEqual(len(_libsvm_lines(tr_l)), 2)


class TestR186LibSvmDuplicateFeatureCols(unittest.TestCase):
    """Round 186 §4: Duplicate names in feature_cols list — export still produces consistent lines."""

    def test_duplicate_feature_cols_export_runs(self):
        train_df, valid_df = _minimal_train_valid(
            ["f1", "f2"],
            ["f1", "f2"],
            [True],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            tr_l, _, _ = _export_via_parquet(train_df, valid_df, ["f1", "f1", "f2"], Path(d))
            lines = _libsvm_lines(tr_l)
            self.assertEqual(len(lines), 1)


class TestR186LibSvmEmptyRatedTrainRaises(unittest.TestCase):
    """Round 186 §5: No rated train rows → RuntimeError (fail-fast)."""

    def test_empty_train_rated_raises(self):
        train_df, valid_df = _minimal_train_valid(
            ["f1"],
            ["f1"],
            [False, False],
            [True],
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(RuntimeError):
                _export_via_parquet(train_df, valid_df, ["f1"], root)


class TestR186LibSvmEmptyValidRatedOk(unittest.TestCase):
    """Round 186 §5: Valid rated empty → train export ok, valid LibSVM file exists (may be empty)."""

    def test_empty_valid_rated_produces_empty_valid_libsvm(self):
        train_df, valid_df = _minimal_train_valid(
            ["f1"],
            ["f1"],
            [True],
            [False, False],
        )
        with tempfile.TemporaryDirectory() as d:
            tr_l, va_l, _ = _export_via_parquet(train_df, valid_df, ["f1"], Path(d))
            self.assertTrue(tr_l.is_file())
            self.assertTrue(va_l.is_file())
            self.assertGreaterEqual(len(_libsvm_lines(tr_l)), 1)
            self.assertEqual(len(_libsvm_lines(va_l)), 0)


class TestR186LibSvmExportReturnPaths(unittest.TestCase):
    """_export_parquet_to_libsvm returns paths under export_dir."""

    def test_returns_paths_and_files_exist(self):
        train_df, valid_df = _minimal_train_valid(["f1"], ["f1"], [True], [True])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l, export_dir = _export_via_parquet(train_df, valid_df, ["f1"], root)
            self.assertEqual(tr_l, export_dir / "train_for_lgb.libsvm")
            self.assertEqual(va_l, export_dir / "valid_for_lgb.libsvm")
            self.assertTrue(tr_l.exists())
            self.assertTrue(va_l.exists())


if __name__ == "__main__":
    unittest.main()
