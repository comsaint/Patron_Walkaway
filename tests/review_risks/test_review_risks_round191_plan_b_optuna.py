"""Minimal reproducible tests for Round 191 Review — 方案 B Step 6 Optuna（從檔案分支 hp 與 num_boost_round）.

Round 191 Review risk points (STATUS.md) are turned into contract/behavior tests.
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

# Five hyperparams used by from-file path for lgb.train (Round 191 Review #2).
FROM_FILE_HP_KEYS = ("learning_rate", "num_leaves", "max_depth", "min_child_samples", "n_estimators")


def _make_rated_dfs(n_train: int, n_valid: int, train_cols: list[str], valid_cols: list[str], seed: int = 42):
    """Minimal train/valid DataFrames with label 0/1 mix and is_rated=True."""
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
    """Default hp dict consistent with trainer when run_optuna is skipped (Round 191 Review #1)."""
    return {
        "n_estimators": 400,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": 8,
        "min_child_samples": 20,
    }


# ---------------------------------------------------------------------------
# R191 Review #1 — 正確性：hp 為空或缺鍵時從檔案分支不應 KeyError（高）
# ---------------------------------------------------------------------------

class TestR191FromFileEmptyHpNoKeyError(unittest.TestCase):
    """Round 191 Review #1: When run_optuna_search returns {} or partial keys, from-file training must not raise."""

    def test_from_file_with_empty_hp_completes_without_key_error(self):
        """run_optuna_search returns {} → train_single_rated_model(..., train_from_file=True) must not raise; use defaults."""
        n = 80
        train_df, valid_df = _make_rated_dfs(n, n, ["f1", "f2"], ["f1", "f2"])
        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, ["f1", "f2"], export_dir)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                with unittest.mock.patch.object(
                    trainer_mod,
                    "run_optuna_search",
                    return_value={},
                ):
                    rated_art, _, _ = train_single_rated_model(
                        train_df,
                        valid_df,
                        ["f1", "f2"],
                        run_optuna=True,
                        test_df=None,
                        train_from_file=True,
                    )
        self.assertIsNotNone(rated_art, "expected a model when hp is empty but defaults should be used (Round 191 #1).")
        hp = rated_art["metrics"]["best_hyperparams"]
        for key in FROM_FILE_HP_KEYS:
            self.assertIn(key, hp, f"best_hyperparams should contain {key} (default or filled) (Round 191 #1).")


# ---------------------------------------------------------------------------
# R191 Review #2 — 一致性：從檔案訓練之 best_hyperparams 至少含 5 鍵且與 Optuna 一致
# ---------------------------------------------------------------------------

class TestR191FromFileBestHyperparamsFiveKeys(unittest.TestCase):
    """Round 191 Review #2: From-file path stores best_hyperparams; assert the 5 lgb.train keys are present and match Optuna."""

    def test_from_file_best_hyperparams_contains_five_keys_from_optuna(self):
        """When run_optuna_search returns full hp (incl. colsample_bytree etc.), best_hyperparams has the 5 keys with same values."""
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
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, ["f1", "f2"], export_dir)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                with unittest.mock.patch.object(
                    trainer_mod,
                    "run_optuna_search",
                    return_value=full_hp.copy(),
                ):
                    rated_art, _, _ = train_single_rated_model(
                        train_df,
                        valid_df,
                        ["f1", "f2"],
                        run_optuna=True,
                        test_df=None,
                        train_from_file=True,
                    )
        self.assertIsNotNone(rated_art)
        best = rated_art["metrics"]["best_hyperparams"]
        for key in FROM_FILE_HP_KEYS:
            self.assertIn(key, best, f"from-file best_hyperparams must include {key} (Round 191 #2).")
            self.assertEqual(
                best[key],
                full_hp[key],
                f"best_hyperparams[{key}] should match Optuna result (Round 191 #2).",
            )


# ---------------------------------------------------------------------------
# R191 Review #3 — 邊界條件：num_boost_round 應至少為 1（低）
# ---------------------------------------------------------------------------

class TestR191FromFileNumBoostRoundAtLeastOne(unittest.TestCase):
    """Round 191 Review #3: When hp has n_estimators 0 or negative, training must not crash; num_boost_round >= 1."""

    def test_from_file_with_n_estimators_zero_completes_without_error(self):
        """hp['n_estimators'] == 0 → from-file training must not raise (production should use max(1, ...))."""
        hp_zero = {**_default_hp(), "n_estimators": 0}
        n = 80
        train_df, valid_df = _make_rated_dfs(n, n, ["f1", "f2"], ["f1", "f2"])
        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, ["f1", "f2"], export_dir)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                with unittest.mock.patch.object(
                    trainer_mod,
                    "run_optuna_search",
                    return_value=hp_zero.copy(),
                ):
                    rated_art, _, _ = train_single_rated_model(
                        train_df,
                        valid_df,
                        ["f1", "f2"],
                        run_optuna=True,
                        test_df=None,
                        train_from_file=True,
                    )
        self.assertIsNotNone(rated_art, "Round 191 #3: n_estimators=0 should still produce a model (num_boost_round >= 1).")


if __name__ == "__main__":
    unittest.main()
