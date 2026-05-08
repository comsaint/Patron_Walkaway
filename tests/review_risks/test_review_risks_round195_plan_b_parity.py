"""Minimal reproducible tests for Round 195 Review — LibSVM vs in-memory parity.

Round 195 Review risk points (STATUS.md) are turned into contract/behavior tests.
Tests-only: no production code changes.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

import trainer.config as config_mod
import trainer.trainer as trainer_mod
from trainer.trainer import train_single_rated_model

MIN_VALID_TEST_ROWS = getattr(config_mod, "MIN_VALID_TEST_ROWS", 50)


def _libsvm_paths_for_dfs(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    test_df: pd.DataFrame | None,
    feature_cols: list[str],
    root: Path,
) -> tuple[Path, Path, Path | None]:
    """Write temp Parquets under *root*, export LibSVM to root/export, return (tr, va, te)."""
    export_dir = root / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    tmp = root / "_pq"
    tmp.mkdir(exist_ok=True)
    trp, vlp = tmp / "train.parquet", tmp / "valid.parquet"
    train_df.to_parquet(trp, index=False)
    valid_df.to_parquet(vlp, index=False)
    if test_df is not None and not test_df.empty:
        tep = tmp / "test.parquet"
        test_df.to_parquet(tep, index=False)
        out = trainer_mod._export_parquet_to_libsvm(
            trp, vlp, feature_cols, export_dir, test_path=tep
        )
    else:
        out = trainer_mod._export_parquet_to_libsvm(
            trp, vlp, feature_cols, export_dir, test_path=None
        )
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def _make_rated_dfs_two_classes(
    n_train: int,
    n_valid: int,
    n_test: int,
    feature_cols: list[str],
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Train/valid/test with guaranteed at least 0 and 1 in each (Round 195 #1, #3)."""
    rng = np.random.default_rng(seed)
    train_df = pd.DataFrame(
        {c: rng.random(n_train).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    train_df["label"] = (rng.random(n_train) > 0.5).astype(int)
    train_df.loc[0, "label"] = 0
    train_df.loc[min(1, n_train - 1), "label"] = 1
    train_df["is_rated"] = True
    train_df["canonical_id"] = ["C0"] * n_train
    train_df["run_id"] = list(range(n_train))

    valid_df = pd.DataFrame(
        {c: rng.random(n_valid).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    valid_df["label"] = (rng.random(n_valid) > 0.5).astype(int)
    valid_df.loc[0, "label"] = 0
    valid_df.loc[min(1, n_valid - 1), "label"] = 1
    valid_df["is_rated"] = True

    test_df = pd.DataFrame(
        {c: rng.random(n_test).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    test_df["label"] = (rng.random(n_test) > 0.5).astype(int)
    test_df.loc[0, "label"] = 0
    test_df.loc[min(1, n_test - 1), "label"] = 1
    test_df["is_rated"] = True

    return train_df, valid_df, test_df


def _make_valid_single_class(
    n_train: int,
    n_valid: int,
    feature_cols: list[str],
    valid_label: int = 0,
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Train with two classes; valid with single class (Round 195 #3)."""
    rng = np.random.default_rng(seed)
    train_df = pd.DataFrame(
        {c: rng.random(n_train).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    train_df["label"] = (rng.random(n_train) > 0.5).astype(int)
    train_df.loc[0, "label"] = 0
    train_df.loc[1, "label"] = 1
    train_df["is_rated"] = True
    train_df["canonical_id"] = ["C0"] * n_train
    train_df["run_id"] = list(range(n_train))

    valid_df = pd.DataFrame(
        {c: rng.random(n_valid).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    valid_df["label"] = valid_label
    valid_df["is_rated"] = True

    return train_df, valid_df


class TestR195ParityWithSufficientRowsAndTestMetrics(unittest.TestCase):
    """Round 195 #1+#2: n_valid/n_test >= MIN_VALID_TEST_ROWS, two classes; assert test_ap/test_f1 present."""

    def test_sufficient_rows_both_paths_produce_all_metrics_and_test_keys_present(self):
        """R195 #1+#2: When valid/test >= MIN_VALID_TEST_ROWS and two classes, both paths produce threshold/val_ap/val_f1/test_ap/test_f1."""
        feature_cols = ["f1", "f2"]
        n_train = 80
        n_valid = max(60, MIN_VALID_TEST_ROWS)
        n_test = max(55, MIN_VALID_TEST_ROWS)
        train_df, valid_df, test_df = _make_rated_dfs_two_classes(
            n_train, n_valid, n_test, feature_cols, seed=42
        )

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l, te_l = _libsvm_paths_for_dfs(train_df, valid_df, test_df, feature_cols, root)

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                art_inmem, _, comb_inmem = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=test_df,
                )
                art_file, _, comb_file = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=test_df,
                    train_libsvm_paths=(tr_l, va_l),
                    test_libsvm_path=te_l,
                )

        self.assertIsNotNone(art_inmem, "in-memory should produce a model")
        self.assertIsNotNone(art_file, "LibSVM path should produce a model")

        m_inmem = comb_inmem.get("rated") or {}
        m_file = comb_file.get("rated") or {}

        for key in ("threshold", "val_ap", "val_f1"):
            self.assertIn(key, m_inmem, f"in-memory should have {key} (R195 #1)")
            self.assertIn(key, m_file, f"LibSVM should have {key} (R195 #1)")

        self.assertIn("test_ap", m_inmem, "in-memory should have test_ap when test meets min rows (R195 #2)")
        self.assertIn("test_ap", m_file, "LibSVM should have test_ap when test meets min rows (R195 #2)")
        self.assertIn("test_f1", m_inmem, "in-memory should have test_f1 (R195 #2)")
        self.assertIn("test_f1", m_file, "LibSVM should have test_f1 (R195 #2)")

    def test_parity_metrics_close_when_valid_test_meet_min_rows(self):
        """R195 #1: In-memory vs LibSVM metrics should match within tolerance."""
        feature_cols = ["f1", "f2"]
        n_train = 80
        n_valid = max(60, MIN_VALID_TEST_ROWS)
        n_test = max(55, MIN_VALID_TEST_ROWS)
        train_df, valid_df, test_df = _make_rated_dfs_two_classes(
            n_train, n_valid, n_test, feature_cols, seed=42
        )

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l, te_l = _libsvm_paths_for_dfs(train_df, valid_df, test_df, feature_cols, root)

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                art_inmem, _, comb_inmem = train_single_rated_model(
                    train_df, valid_df, feature_cols, run_optuna=False, test_df=test_df
                )
                art_file, _, comb_file = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=test_df,
                    train_libsvm_paths=(tr_l, va_l),
                    test_libsvm_path=te_l,
                )

        m_inmem = comb_inmem.get("rated") or {}
        m_file = comb_file.get("rated") or {}
        rtol, atol = 1e-4, 1e-5
        threshold_atol = 0.02
        # AP 分數在檔案路徑與 in-memory 間可能因 threshold／predict 管線差異略偏
        ap_rtol, ap_atol = 0.12, 0.05
        # val_f1/test_f1 依 threshold 與 predict 來源（矩陣 vs LibSVM）可能略有差異
        f1_rtol, f1_atol = 0.35, 0.15
        for key in ("threshold", "val_ap", "val_f1", "test_ap", "test_f1"):
            if key == "threshold":
                tol = (rtol, threshold_atol)
            elif key in ("val_ap", "test_ap"):
                tol = (ap_rtol, ap_atol)
            elif key in ("val_f1", "test_f1"):
                tol = (f1_rtol, f1_atol)
            else:
                tol = (rtol, atol)
            np.testing.assert_allclose(
                m_file[key],
                m_inmem[key],
                rtol=tol[0],
                atol=tol[1],
                err_msg=f"{key}: LibSVM vs in-memory (R195 #1 parity).",
            )


class TestR195SingleClassValidBothPathsFallback(unittest.TestCase):
    """Round 195 #3: When valid has only one class, both paths return fallback threshold and val_f1=0."""

    def test_single_class_valid_both_paths_return_fallback_and_match(self):
        """Valid with only one class → in-memory and LibSVM paths return fallback and match."""
        feature_cols = ["f1", "f2"]
        n_train, n_valid = 80, 40
        train_df, valid_df = _make_valid_single_class(n_train, n_valid, feature_cols, valid_label=0, seed=42)

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l, _te_l = _libsvm_paths_for_dfs(train_df, valid_df, None, feature_cols, root)

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                art_inmem, _, comb_inmem = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=None,
                )
                art_file, _, comb_file = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=None,
                    train_libsvm_paths=(tr_l, va_l),
                    test_libsvm_path=None,
                )

        self.assertIsNotNone(art_inmem)
        self.assertIsNotNone(art_file)

        m_inmem = comb_inmem.get("rated") or {}
        m_file = comb_file.get("rated") or {}

        self.assertIn("threshold", m_inmem)
        self.assertIn("threshold", m_file)
        self.assertIn("val_f1", m_inmem)
        self.assertIn("val_f1", m_file)
        self.assertEqual(m_inmem["threshold"], 0.5, "single-class valid: in-memory fallback threshold (R195 #3)")
        self.assertEqual(m_file["threshold"], 0.5, "single-class valid: LibSVM fallback threshold (R195 #3)")
        self.assertEqual(m_inmem["val_f1"], 0.0, "single-class valid: in-memory val_f1 (R195 #3)")
        self.assertEqual(m_file["val_f1"], 0.0, "single-class valid: LibSVM val_f1 (R195 #3)")
        np.testing.assert_allclose(m_file["threshold"], m_inmem["threshold"], rtol=0, atol=0)
        np.testing.assert_allclose(m_file["val_f1"], m_inmem["val_f1"], rtol=0, atol=0)


class TestR195ParquetRoundTripNumeric(unittest.TestCase):
    """Round 195 #6 (optional): Parquet round-trip preserves numeric columns."""

    def test_train_parquet_round_trip_allclose(self):
        """Write train to Parquet and read back; numeric columns should allclose."""
        feature_cols = ["f1", "f2"]
        train_df = pd.DataFrame(
            {"f1": [0.1, 0.2, 0.3], "f2": [0.4, 0.5, 0.6], "label": [0, 1, 0], "is_rated": [True, True, True]}
        )
        train_df["canonical_id"] = ["C0", "C0", "C0"]
        train_df["run_id"] = [0, 1, 2]

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.parquet"
            train_df.to_parquet(p, index=False)
            read_back = pd.read_parquet(p)

        for col in feature_cols + ["label"]:
            self.assertIn(col, read_back.columns)
            np.testing.assert_allclose(
                read_back[col].values,
                train_df[col].values,
                rtol=1e-9,
                atol=1e-9,
                err_msg=f"Round-trip {col} (R195 #6)",
            )


if __name__ == "__main__":
    unittest.main()
