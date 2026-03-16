"""Minimal reproducible tests for Round 188 Review — 方案 B Step 4 + Step 5（從檔案訓練 + Booster 包裝）.

Round 188 Review risk points (STATUS.md) are turned into contract/behavior tests.
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
from trainer.trainer import _BoosterWrapper, _export_train_valid_to_csv, train_single_rated_model


def _make_rated_dfs(n_train: int, n_valid: int, train_cols: list[str], valid_cols: list[str], seed: int = 42):
    """Minimal train/valid DataFrames with label 0/1 mix and is_rated=True, canonical_id/run_id for sample_weight."""
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


# ---------------------------------------------------------------------------
# R188 Review #1 — 正確性：rated_art["features"] 應與 booster.feature_name() 一致
# ---------------------------------------------------------------------------

class TestR188FromFileFeaturesMatchBooster(unittest.TestCase):
    """Round 188 Review #1: When training from file with common_cols, artifact features must equal booster features."""

    def test_rated_art_features_equal_booster_feature_name_when_common_cols_used(self):
        """When export uses common_cols (valid has fewer columns), rated_art['features'] == booster.feature_name()."""
        n = 60
        train_df, valid_df = _make_rated_dfs(n, n, ["f1", "f2"], ["f1"])
        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, ["f1", "f2"], export_dir)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                rated_art, _, _ = train_single_rated_model(
                    train_df,
                    valid_df,
                    ["f1", "f2"],
                    run_optuna=False,
                    test_df=None,
                    train_from_file=True,
                )
        self.assertIsNotNone(rated_art, "expected a model when train/valid CSVs exist")
        booster = rated_art["model"].booster_
        expected_features = list(booster.feature_name())
        self.assertEqual(
            rated_art["features"],
            expected_features,
            "rated_art['features'] must match Booster feature set/order (Round 188 Review #1).",
        )


# ---------------------------------------------------------------------------
# R188 Review #2 — 邊界條件：0 列 train CSV 應 fallback 或明確處理
# ---------------------------------------------------------------------------

class TestR188FromFileZeroRowTrainCsv(unittest.TestCase):
    """Round 188 Review #2: Header-only (0 data rows) train CSV should not crash; expect fallback or clear handling."""

    def test_zero_row_train_csv_does_not_raise(self):
        """When train_for_lgb.csv has only header, train_single_rated_model(..., train_from_file=True) must not raise."""
        n = 60
        train_df, valid_df = _make_rated_dfs(n, n, ["f1"], ["f1"])
        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            (export_dir / "train_for_lgb.csv").write_text("f1,label,weight\n", encoding="utf-8")
            valid_export = valid_df[["f1", "label"]].copy()
            valid_export.to_csv(export_dir / "valid_for_lgb.csv", index=False)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                rated_art, _, _ = train_single_rated_model(
                    train_df,
                    valid_df,
                    ["f1"],
                    run_optuna=False,
                    test_df=None,
                    train_from_file=True,
                )
                # Desired: no exception; production should fallback to in-memory when train CSV has 0 rows.
                self.assertIsNotNone(rated_art)


# ---------------------------------------------------------------------------
# R188 Review #3 — 邊界條件：單一類別訓練應與 R1509 一致（ValueError 或 fallback）
# ---------------------------------------------------------------------------

class TestR188FromFileSingleClassTrain(unittest.TestCase):
    """Round 188 Review #3: Train CSV with only one class should raise ValueError or fallback, not silent model."""

    def test_single_class_train_csv_raises_or_fallbacks(self):
        """When train CSV has only label=0, expect ValueError or fallback (same as in-memory R1509)."""
        n = 60
        train_df, valid_df = _make_rated_dfs(n, n, ["f1"], ["f1"])
        train_df["label"] = 0
        with tempfile.TemporaryDirectory() as d:
            export_dir = Path(d) / "export"
            export_dir.mkdir(parents=True)
            _export_train_valid_to_csv(train_df, valid_df, ["f1"], export_dir)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", Path(d)):
                try:
                    rated_art, _, _ = train_single_rated_model(
                        train_df,
                        valid_df,
                        ["f1"],
                        run_optuna=False,
                        test_df=None,
                        train_from_file=True,
                    )
                    self.assertIsNone(
                        rated_art,
                        "Round 188 Review #3: single-class train should not produce a normal model",
                    )
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# R188 Review #4 — _BoosterWrapper：predict_proba 形狀與 [:,1] 與 booster.predict 一致
# ---------------------------------------------------------------------------

class TestR188BoosterWrapperPredictProba(unittest.TestCase):
    """Round 188 Review #4: _BoosterWrapper.predict_proba shape and [:,1] match booster.predict(X)."""

    def test_wrapper_predict_proba_shape_and_positive_class_matches_booster(self):
        """With correct DataFrame columns, predict_proba(X) has shape (n,2) and [:,1] == booster.predict(X)."""
        import lightgbm as lgb

        X = pd.DataFrame({"f1": [0.1, 0.2, 0.3], "f2": [1.0, 0.5, 0.0]})
        y = np.array([0, 1, 0])
        ds = lgb.Dataset(X, label=y)
        params = {"objective": "binary", "verbosity": -1, "num_leaves": 2}
        booster = lgb.train(params, ds, num_boost_round=3)
        wrapper = _BoosterWrapper(booster)
        proba = wrapper.predict_proba(X)
        self.assertEqual(proba.shape, (3, 2), "predict_proba must return (n, 2)")
        np.testing.assert_array_almost_equal(
            proba[:, 1],
            booster.predict(X),
            err_msg="predict_proba(X)[:,1] must equal booster.predict(X) (Round 188 Review #4).",
        )


# ---------------------------------------------------------------------------
# R188 Review #6 — LightGBM 從檔案建 Dataset + train 可成功（依賴/版本契約）
# ---------------------------------------------------------------------------

class TestR188LgbDatasetFromCsvParams(unittest.TestCase):
    """Round 188 Review #6: Current LightGBM accepts header/label_column/weight_column and builds Booster from CSV."""

    def test_lgb_dataset_and_train_from_minimal_csv_succeeds(self):
        """Minimal train/valid CSV with header + label_column => lgb.Dataset + lgb.train runs, feature_name() as expected."""
        import lightgbm as lgb

        with tempfile.TemporaryDirectory() as d:
            train_path = Path(d) / "train.csv"
            valid_path = Path(d) / "valid.csv"
            # Train: f1 + label only (no weight) to match valid; tests header + label_column (Round 188 Review #6).
            train_path.write_text(
                "f1,label\n"
                "0.1,0\n"
                "0.2,1\n"
                "0.3,0\n"
                "0.4,1\n"
                "0.5,0\n",
                encoding="utf-8",
            )
            valid_path.write_text(
                "f1,label\n"
                "0.6,0\n"
                "0.7,1\n"
                "0.8,0\n",
                encoding="utf-8",
            )
            ds_params = {"header": True, "label_column": "name:label"}
            dtrain = lgb.Dataset(str(train_path), params=ds_params)
            dvalid = lgb.Dataset(str(valid_path), reference=dtrain, params=ds_params)
            params = {"objective": "binary", "verbosity": -1, "num_leaves": 2}
            booster = lgb.train(params, dtrain, num_boost_round=3, valid_sets=[dvalid])
        self.assertEqual(list(booster.feature_name()), ["f1"], "Booster should have feature f1 (Round 188 Review #6).")


if __name__ == "__main__":
    unittest.main()
