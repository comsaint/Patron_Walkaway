"""Round 188 Review — LibSVM disk training + Booster wrapper (CSV / train_from_file retired)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

import trainer.trainer as trainer_mod
from trainer.trainer import _BoosterWrapper, train_single_rated_model
from trainer.training.model_eval_runtime import _lgb_booster_feature_name_list


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
    valid_df["canonical_id"] = ["C0"] * n_valid
    valid_df["run_id"] = list(range(n_valid))
    return train_df, valid_df


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


class TestR188LibSvmFeaturesMatchBooster(unittest.TestCase):
    """Round 188 #1: LibSVM 維度與 feature_cols 一致時，artifact features 等於 booster。"""

    def test_rated_art_features_equal_booster_feature_name(self):
        n = 60
        train_df, valid_df = _make_rated_dfs(n, n, ["f1", "f2"], ["f1", "f2"])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l = _libsvm_train_valid(train_df, valid_df, ["f1", "f2"], root)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                rated_art, _, _ = train_single_rated_model(
                    train_df,
                    valid_df,
                    ["f1", "f2"],
                    run_optuna=False,
                    test_df=None,
                    train_libsvm_paths=(tr_l, va_l),
                )
        self.assertIsNotNone(rated_art)
        booster = rated_art["model"].booster_
        expected_features = _lgb_booster_feature_name_list(booster)
        self.assertEqual(rated_art["features"], expected_features)


class TestR188ExportZeroRatedTrainRaises(unittest.TestCase):
    """Round 188 #2: 0 rated train rows at export → RuntimeError (no silent fallback)."""

    def test_zero_rated_train_parquet_raises_on_export(self):
        n = 60
        train_df, valid_df = _make_rated_dfs(n, n, ["f1"], ["f1"])
        train_df["is_rated"] = False
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            export_dir = root / "export"
            export_dir.mkdir(parents=True, exist_ok=True)
            tmp = root / "_pq"
            tmp.mkdir(exist_ok=True)
            trp, vlp = tmp / "train.parquet", tmp / "valid.parquet"
            train_df.to_parquet(trp, index=False)
            valid_df.to_parquet(vlp, index=False)
            with self.assertRaises(RuntimeError):
                trainer_mod._export_parquet_to_libsvm(trp, vlp, ["f1"], export_dir, test_path=None)
            shutil.rmtree(tmp, ignore_errors=True)


class TestR188LibSvmSingleClassTrainRaises(unittest.TestCase):
    """Round 188 #3: Single-class train LibSVM → RuntimeError."""

    def test_single_class_train_libsvm_raises(self):
        n = 60
        train_df, valid_df = _make_rated_dfs(n, n, ["f1"], ["f1"])
        train_df["label"] = 0
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l = _libsvm_train_valid(train_df, valid_df, ["f1"], root)
            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root):
                with self.assertRaises(RuntimeError):
                    train_single_rated_model(
                        train_df,
                        valid_df,
                        ["f1"],
                        run_optuna=False,
                        test_df=None,
                        train_libsvm_paths=(tr_l, va_l),
                    )


class TestR188BoosterWrapperPredictProba(unittest.TestCase):
    """Round 188 #4: _BoosterWrapper.predict_proba."""

    def test_wrapper_predict_proba_shape_and_positive_class_matches_booster(self):
        import lightgbm as lgb

        X = pd.DataFrame({"f1": [0.1, 0.2, 0.3], "f2": [1.0, 0.5, 0.0]})
        y = np.array([0, 1, 0])
        ds = lgb.Dataset(X, label=y)
        params = {"objective": "binary", "verbosity": -1, "num_leaves": 2}
        booster = lgb.train(params, ds, num_boost_round=3)
        wrapper = _BoosterWrapper(booster)
        proba = wrapper.predict_proba(X)
        self.assertEqual(proba.shape, (3, 2))
        np.testing.assert_array_almost_equal(proba[:, 1], booster.predict(X))


class TestR188LgbDatasetFromMinimalCsv(unittest.TestCase):
    """Round 188 #6: LightGBM Dataset from minimal CSV (API contract)."""

    def test_lgb_dataset_and_train_from_minimal_csv_succeeds(self):
        import lightgbm as lgb

        with tempfile.TemporaryDirectory() as d:
            train_path = Path(d) / "train.csv"
            valid_path = Path(d) / "valid.csv"
            train_path.write_text(
                "f1,label\n0.1,0\n0.2,1\n0.3,0\n0.4,1\n0.5,0\n",
                encoding="utf-8",
            )
            valid_path.write_text("f1,label\n0.6,0\n0.7,1\n0.8,0\n", encoding="utf-8")
            ds_params = {"header": True, "label_column": "name:label"}
            dtrain = lgb.Dataset(str(train_path), params=ds_params)
            dvalid = lgb.Dataset(str(valid_path), reference=dtrain, params=ds_params)
            params = {"objective": "binary", "verbosity": -1, "num_leaves": 2}
            booster = lgb.train(params, dtrain, num_boost_round=3, valid_sets=[dvalid])
        self.assertEqual(_lgb_booster_feature_name_list(booster), ["f1"])


class TestR188TrainSingleLibSvmFailFastInSource(unittest.TestCase):
    """Round 188 follow-up: LibSVM-only fail-fast checks exist in train_single_rated_model."""

    def test_source_contains_libsvm_only_guards(self):
        import inspect

        src = inspect.getsource(trainer_mod.train_single_rated_model)
        self.assertIn("LibSVM-only: train LibSVM has only one class", src)
        self.assertIn("LibSVM-only: train LibSVM has 0 data lines", src)


if __name__ == "__main__":
    unittest.main()
