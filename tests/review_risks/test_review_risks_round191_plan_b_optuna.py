"""Minimal reproducible tests for Round 191 Review — LibSVM path Optuna hp defaults."""

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

FROM_FILE_HP_KEYS = ("learning_rate", "num_leaves", "max_depth", "min_child_samples", "n_estimators")


def _libsvm_train_valid(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    feature_cols: list[str],
    root: Path,
) -> tuple[Path, Path]:
    export_dir = root / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    tmp = root / "_pq"
    tmp.mkdir(exist_ok=True)
    trp, vlp = tmp / "train.parquet", tmp / "valid.parquet"
    train_df.to_parquet(trp, index=False)
    valid_df.to_parquet(vlp, index=False)
    tr_l, va_l, _ = trainer_mod._export_parquet_to_libsvm(
        trp, vlp, feature_cols, export_dir, test_path=None
    )
    shutil.rmtree(tmp, ignore_errors=True)
    return tr_l, va_l


def _make_rated_dfs(n_train: int, n_valid: int, train_cols: list[str], valid_cols: list[str], seed: int = 42):
    rng = np.random.default_rng(seed)
    train_df = pd.DataFrame(
        {c: rng.random(n_train).astype(np.float64) for c in train_cols},
        columns=train_cols,
    )
    train_df["label"] = (rng.random(n_train) > 0.5).astype(int)
    train_df["is_rated"] = True
    train_df["canonical_id"] = ["C0"] * n_train
    train_df["run_id"] = list(range(n_train))

    valid_df = pd.DataFrame(
        {c: rng.random(n_valid).astype(np.float64) for c in valid_cols},
        columns=valid_cols,
    )
    valid_df["label"] = (rng.random(n_valid) > 0.5).astype(int)
    valid_df["is_rated"] = True
    return train_df, valid_df


def _default_hp():
    return {
        "n_estimators": 400,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": 8,
        "min_child_samples": 20,
    }


class TestR191LibSvmEmptyHpNoKeyError(unittest.TestCase):
    """Round 191 #1: When run_optuna_search returns {}, LibSVM path must not raise."""

    def test_libsvm_with_empty_hp_completes_without_key_error(self):
        n = 80
        train_df, valid_df = _make_rated_dfs(n, n, ["f1", "f2"], ["f1", "f2"])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l = _libsvm_train_valid(train_df, valid_df, ["f1", "f2"], root)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                with unittest.mock.patch.object(trainer_mod, "run_optuna_search", return_value={}):
                    rated_art, _, _ = train_single_rated_model(
                        train_df,
                        valid_df,
                        ["f1", "f2"],
                        run_optuna=True,
                        test_df=None,
                        train_libsvm_paths=(tr_l, va_l),
                    )
        self.assertIsNotNone(rated_art)
        hp = rated_art["metrics"]["best_hyperparams"]
        for key in FROM_FILE_HP_KEYS:
            self.assertIn(key, hp, f"best_hyperparams should contain {key} (Round 191 #1).")


class TestR191LibSvmBestHyperparamsFiveKeys(unittest.TestCase):
    """Round 191 #2: LibSVM path best_hyperparams includes five keys from Optuna."""

    def test_libsvm_best_hyperparams_contains_five_keys_from_optuna(self):
        full_hp = {
            **_default_hp(),
            "colsample_bytree": 0.8,
            "subsample": 0.9,
            "reg_alpha": 0.1,
            "reg_lambda": 0.1,
        }
        n = 80
        train_df, valid_df = _make_rated_dfs(n, n, ["f1", "f2"], ["f1", "f2"])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l = _libsvm_train_valid(train_df, valid_df, ["f1", "f2"], root)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                with unittest.mock.patch.object(
                    trainer_mod, "run_optuna_search", return_value=full_hp.copy()
                ):
                    rated_art, _, _ = train_single_rated_model(
                        train_df,
                        valid_df,
                        ["f1", "f2"],
                        run_optuna=True,
                        test_df=None,
                        train_libsvm_paths=(tr_l, va_l),
                    )
        self.assertIsNotNone(rated_art)
        best = rated_art["metrics"]["best_hyperparams"]
        for key in FROM_FILE_HP_KEYS:
            self.assertIn(key, best, f"LibSVM best_hyperparams must include {key} (Round 191 #2).")
            self.assertEqual(best[key], full_hp[key], f"best_hyperparams[{key}] should match Optuna (Round 191 #2).")


class TestR191LibSvmNumBoostRoundAtLeastOne(unittest.TestCase):
    """Round 191 #3: n_estimators 0 must not crash LibSVM path."""

    def test_libsvm_with_n_estimators_zero_completes_without_error(self):
        hp_zero = {**_default_hp(), "n_estimators": 0}
        n = 80
        train_df, valid_df = _make_rated_dfs(n, n, ["f1", "f2"], ["f1", "f2"])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l = _libsvm_train_valid(train_df, valid_df, ["f1", "f2"], root)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                with unittest.mock.patch.object(
                    trainer_mod, "run_optuna_search", return_value=hp_zero.copy()
                ):
                    rated_art, _, _ = train_single_rated_model(
                        train_df,
                        valid_df,
                        ["f1", "f2"],
                        run_optuna=True,
                        test_df=None,
                        train_libsvm_paths=(tr_l, va_l),
                    )
        self.assertIsNotNone(rated_art, "Round 191 #3: n_estimators=0 should still produce a model.")


if __name__ == "__main__":
    unittest.main()
