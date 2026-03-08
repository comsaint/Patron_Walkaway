"""Parity test for PLAN 方案 B §九 第 7 項：in-memory 與從檔案訓練之指標比對.

同一組 train/valid/test 下，run_optuna=False 時，train_from_file=False 與 train_from_file=True
應產出相同或極接近之 threshold、val_ap、val_f1、test_ap、test_f1，以驗證從檔案訓練與
in-memory 訓練結果一致（PLAN 方案 B 實作狀態 第 7 項）。
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


def _make_rated_dfs(n_train: int, n_valid: int, n_test: int, feature_cols: list[str], seed: int = 42):
    """Train/valid/test DataFrames with label 0/1 mix and is_rated=True; canonical_id/run_id for train."""
    rng = np.random.default_rng(seed)
    train_df = pd.DataFrame(
        {c: rng.random(n_train).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    train_df["label"] = (rng.random(n_train) > 0.5).astype(int)
    train_df["is_rated"] = True
    train_df["canonical_id"] = ["C0"] * n_train
    train_df["run_id"] = list(range(n_train))

    valid_df = pd.DataFrame(
        {c: rng.random(n_valid).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    valid_df["label"] = (rng.random(n_valid) > 0.5).astype(int)
    valid_df["is_rated"] = True

    test_df = pd.DataFrame(
        {c: rng.random(n_test).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    test_df["label"] = (rng.random(n_test) > 0.5).astype(int)
    test_df["is_rated"] = True

    return train_df, valid_df, test_df


class TestPlanBInmemoryVsFromFileParity(unittest.TestCase):
    """PLAN 方案 B §九 第 7 項：同資料、run_optuna=False 時，in-memory 與 from-file 指標一致."""

    def test_same_data_inmemory_vs_fromfile_metrics_close(self):
        """Same train/valid/test; run_optuna=False → threshold, val_ap, val_f1, test_ap, test_f1 應一致或極接近."""
        feature_cols = ["f1", "f2"]
        n_train, n_valid, n_test = 80, 40, 30
        train_df, valid_df, test_df = _make_rated_dfs(n_train, n_valid, n_test, feature_cols, seed=42)

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

        # 比對關鍵指標（PLAN §九 第 7 項：threshold、test AP/F1；含 validation）
        rtol, atol = 1e-4, 1e-5
        self.assertIn("threshold", m_inmem)
        self.assertIn("threshold", m_file)
        np.testing.assert_allclose(
            m_file["threshold"],
            m_inmem["threshold"],
            rtol=rtol,
            atol=atol,
            err_msg="threshold: from-file vs in-memory (PLAN B §9.7)",
        )
        self.assertIn("val_ap", m_inmem)
        self.assertIn("val_ap", m_file)
        np.testing.assert_allclose(
            m_file["val_ap"],
            m_inmem["val_ap"],
            rtol=rtol,
            atol=atol,
            err_msg="val_ap: from-file vs in-memory (PLAN B §9.7)",
        )
        self.assertIn("val_f1", m_inmem)
        self.assertIn("val_f1", m_file)
        np.testing.assert_allclose(
            m_file["val_f1"],
            m_inmem["val_f1"],
            rtol=rtol,
            atol=atol,
            err_msg="val_f1: from-file vs in-memory (PLAN B §9.7)",
        )
        if "test_ap" in m_inmem and "test_ap" in m_file:
            np.testing.assert_allclose(
                m_file["test_ap"],
                m_inmem["test_ap"],
                rtol=rtol,
                atol=atol,
                err_msg="test_ap: from-file vs in-memory (PLAN B §9.7)",
            )
        if "test_f1" in m_inmem and "test_f1" in m_file:
            np.testing.assert_allclose(
                m_file["test_f1"],
                m_inmem["test_f1"],
                rtol=rtol,
                atol=atol,
                err_msg="test_f1: from-file vs in-memory (PLAN B §9.7)",
            )


if __name__ == "__main__":
    unittest.main()
