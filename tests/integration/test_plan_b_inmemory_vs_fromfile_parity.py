"""Parity: in-memory vs LibSVM-file training (PLAN B §九 第 7 項, LibSVM-only)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

import trainer.trainer as trainer_mod
from trainer.trainer import train_single_rated_model


def _make_rated_dfs(n_train: int, n_valid: int, n_test: int, feature_cols: list[str], seed: int = 42):
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


def _libsvm_paths(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_cols: list[str],
    root: Path,
) -> tuple[Path, Path, Path]:
    export_dir = root / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    tmp = root / "_pq"
    tmp.mkdir(exist_ok=True)
    trp, vlp, tsp = tmp / "train.parquet", tmp / "valid.parquet", tmp / "test.parquet"
    train_df.to_parquet(trp, index=False)
    valid_df.to_parquet(vlp, index=False)
    test_df.to_parquet(tsp, index=False)
    tr_l, va_l, te_l = trainer_mod._export_parquet_to_libsvm(
        trp, vlp, feature_cols, export_dir, test_path=tsp
    )
    shutil.rmtree(tmp, ignore_errors=True)
    return tr_l, va_l, te_l


class TestPlanBInmemoryVsLibSvmParity(unittest.TestCase):
    """Same data, run_optuna=False: in-memory vs LibSVM metrics should match closely."""

    def test_same_data_inmemory_vs_libsvm_metrics_close(self):
        feature_cols = ["f1", "f2"]
        n_train, n_valid, n_test = 80, 40, 30
        train_df, valid_df, test_df = _make_rated_dfs(n_train, n_valid, n_test, feature_cols, seed=42)

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l, te_l = _libsvm_paths(train_df, valid_df, test_df, feature_cols, root)

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                art_inmem, _, comb_inmem = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=test_df,
                    train_libsvm_paths=None,
                )
                art_libsvm, _, comb_libsvm = train_single_rated_model(
                    train_df,
                    valid_df,
                    feature_cols,
                    run_optuna=False,
                    test_df=test_df,
                    train_libsvm_paths=(tr_l, va_l),
                    test_libsvm_path=te_l,
                )

        self.assertIsNotNone(art_inmem)
        self.assertIsNotNone(art_libsvm)

        m_inmem = comb_inmem.get("rated") or {}
        m_libsvm = comb_libsvm.get("rated") or {}

        rtol, atol = 1e-4, 1e-5
        self.assertIn("threshold", m_inmem)
        self.assertIn("threshold", m_libsvm)
        np.testing.assert_allclose(
            m_libsvm["threshold"],
            m_inmem["threshold"],
            rtol=rtol,
            atol=atol,
            err_msg="threshold: LibSVM vs in-memory",
        )
        self.assertIn("val_ap", m_inmem)
        self.assertIn("val_ap", m_libsvm)
        np.testing.assert_allclose(
            m_libsvm["val_ap"],
            m_inmem["val_ap"],
            rtol=rtol,
            atol=atol,
            err_msg="val_ap: LibSVM vs in-memory",
        )
        self.assertIn("val_f1", m_inmem)
        self.assertIn("val_f1", m_libsvm)
        np.testing.assert_allclose(
            m_libsvm["val_f1"],
            m_inmem["val_f1"],
            rtol=rtol,
            atol=atol,
            err_msg="val_f1: LibSVM vs in-memory",
        )
        if "test_ap" in m_inmem and "test_ap" in m_libsvm:
            np.testing.assert_allclose(
                m_libsvm["test_ap"],
                m_inmem["test_ap"],
                rtol=rtol,
                atol=atol,
                err_msg="test_ap: LibSVM vs in-memory",
            )
        if "test_f1" in m_inmem and "test_f1" in m_libsvm:
            np.testing.assert_allclose(
                m_libsvm["test_f1"],
                m_inmem["test_f1"],
                rtol=rtol,
                atol=atol,
                err_msg="test_f1: LibSVM vs in-memory",
            )


if __name__ == "__main__":
    unittest.main()
