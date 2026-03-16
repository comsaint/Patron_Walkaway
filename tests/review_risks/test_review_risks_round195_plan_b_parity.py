"""Minimal reproducible tests for Round 195 Review — 方案 B parity 測試之邊界與覆蓋率.

Round 195 Review risk points (STATUS.md) are turned into contract/behavior tests.
Tests-only: no production code changes.
"""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

import trainer.config as config_mod
import trainer.trainer as trainer_mod
from trainer.trainer import _export_train_valid_to_csv, train_single_rated_model

MIN_VALID_TEST_ROWS = getattr(config_mod, "MIN_VALID_TEST_ROWS", 50)


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


# ---------------------------------------------------------------------------
# R195 Review #1 — 邊界：valid/test 筆數 ≥ MIN_VALID_TEST_ROWS，且雙類，parity 含完整閾值
# R195 Review #2 — 正確性：test_ap/test_f1 兩路徑皆存在並比對
# ---------------------------------------------------------------------------

class TestR195ParityWithSufficientRowsAndTestMetrics(unittest.TestCase):
    """Round 195 #1+#2: n_valid/n_test >= MIN_VALID_TEST_ROWS, two classes; assert test_ap/test_f1 present (and parity when production aligns)."""

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
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, feature_cols, export_dir)

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                art_inmem, _, comb_inmem = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=test_df,
                    train_from_file=False,
                )
                art_file, _, comb_file = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=test_df,
                    train_from_file=True,
                )

        self.assertIsNotNone(art_inmem, "in-memory should produce a model")
        self.assertIsNotNone(art_file, "from-file should produce a model")

        m_inmem = comb_inmem.get("rated") or {}
        m_file = comb_file.get("rated") or {}

        for key in ("threshold", "val_ap", "val_f1"):
            self.assertIn(key, m_inmem, f"in-memory should have {key} (R195 #1)")
            self.assertIn(key, m_file, f"from-file should have {key} (R195 #1)")

        # R195 #2: when test_df is provided and meets size, both paths must produce test_ap/test_f1
        self.assertIn("test_ap", m_inmem, "in-memory should have test_ap when test meets min rows (R195 #2)")
        self.assertIn("test_ap", m_file, "from-file should have test_ap when test meets min rows (R195 #2)")
        self.assertIn("test_f1", m_inmem, "in-memory should have test_f1 (R195 #2)")
        self.assertIn("test_f1", m_file, "from-file should have test_f1 (R195 #2)")

    def test_parity_metrics_close_when_valid_test_meet_min_rows(self):
        """R195 #1: In-memory vs from-file metrics (threshold, val_ap, val_f1, test_ap, test_f1) should match within tolerance."""
        feature_cols = ["f1", "f2"]
        n_train = 80
        n_valid = max(60, MIN_VALID_TEST_ROWS)
        n_test = max(55, MIN_VALID_TEST_ROWS)
        train_df, valid_df, test_df = _make_rated_dfs_two_classes(
            n_train, n_valid, n_test, feature_cols, seed=42
        )

        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, feature_cols, export_dir)

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                art_inmem, _, comb_inmem = train_single_rated_model(
                    train_df, valid_df, feature_cols, run_optuna=False, test_df=test_df, train_from_file=False
                )
                art_file, _, comb_file = train_single_rated_model(
                    train_df, valid_df, feature_cols, run_optuna=False, test_df=test_df, train_from_file=True
                )

        m_inmem = comb_inmem.get("rated") or {}
        m_file = comb_file.get("rated") or {}
        rtol, atol = 1e-4, 1e-5
        # threshold from PR-curve discrete grid; allow slightly looser atol (R197)
        threshold_atol = 0.02
        for key in ("threshold", "val_ap", "val_f1", "test_ap", "test_f1"):
            tol = (rtol, threshold_atol) if key == "threshold" else (rtol, atol)
            np.testing.assert_allclose(
                m_file[key], m_inmem[key], rtol=tol[0], atol=tol[1],
                err_msg=f"{key}: from-file vs in-memory (R195 #1 parity).",
            )


# ---------------------------------------------------------------------------
# R195 Review #3 — 邊界：valid 僅單類時兩路徑皆 fallback 且一致
# ---------------------------------------------------------------------------

class TestR195SingleClassValidBothPathsFallback(unittest.TestCase):
    """Round 195 #3: When valid has only one class, both paths return fallback threshold and val_f1=0."""

    def test_single_class_valid_both_paths_return_fallback_and_match(self):
        """Valid with only one class → both in-memory and from-file return fallback (e.g. threshold 0.5, val_f1=0) and match."""
        feature_cols = ["f1", "f2"]
        n_train, n_valid = 80, 40
        train_df, valid_df = _make_valid_single_class(n_train, n_valid, feature_cols, valid_label=0, seed=42)

        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, feature_cols, export_dir)

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                art_inmem, _, comb_inmem = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=None,
                    train_from_file=False,
                )
                art_file, _, comb_file = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=None,
                    train_from_file=True,
                )

        self.assertIsNotNone(art_inmem)
        self.assertIsNotNone(art_file)

        m_inmem = comb_inmem.get("rated") or {}
        m_file = comb_file.get("rated") or {}

        self.assertIn("threshold", m_inmem)
        self.assertIn("threshold", m_file)
        self.assertIn("val_f1", m_inmem)
        self.assertIn("val_f1", m_file)
        # Fallback: threshold 0.5, val_f1 0
        self.assertEqual(m_inmem["threshold"], 0.5, "single-class valid: in-memory fallback threshold (R195 #3)")
        self.assertEqual(m_file["threshold"], 0.5, "single-class valid: from-file fallback threshold (R195 #3)")
        self.assertEqual(m_inmem["val_f1"], 0.0, "single-class valid: in-memory val_f1 (R195 #3)")
        self.assertEqual(m_file["val_f1"], 0.0, "single-class valid: from-file val_f1 (R195 #3)")
        np.testing.assert_allclose(m_file["threshold"], m_inmem["threshold"], rtol=0, atol=0)
        np.testing.assert_allclose(m_file["val_f1"], m_inmem["val_f1"], rtol=0, atol=0)


# ---------------------------------------------------------------------------
# R195 Review #6 — 浮點：export 後 read_csv 與原 DataFrame allclose（可選）
# ---------------------------------------------------------------------------

class TestR195ExportReadCsvFloatParity(unittest.TestCase):
    """Round 195 #6 (optional): Fixed float data export then read back matches original within tolerance."""

    def test_export_train_csv_read_back_allclose_to_original(self):
        """Export train DataFrame to CSV and read back; numeric columns should allclose to original."""
        feature_cols = ["f1", "f2"]
        train_df = pd.DataFrame(
            {"f1": [0.1, 0.2, 0.3], "f2": [0.4, 0.5, 0.6], "label": [0, 1, 0], "is_rated": [True, True, True]}
        )
        train_df["canonical_id"] = ["C0", "C0", "C0"]
        train_df["run_id"] = [0, 1, 2]
        valid_df = train_df.iloc[:2].copy()

        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, feature_cols, export_dir)
            path = export_dir / "train_for_lgb.csv"
            read_back = pd.read_csv(path)

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
