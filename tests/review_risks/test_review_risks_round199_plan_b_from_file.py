"""Round 199 Review — LibSVM path edge cases (from-file / CSV retired)."""

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


def _make_train_rated(n: int, feature_cols: list[str], seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(
        {c: rng.random(n).astype(np.float64) for c in feature_cols},
        columns=feature_cols,
    )
    df["label"] = (rng.random(n) > 0.5).astype(int)
    df.loc[0, "label"] = 0
    df.loc[min(1, n - 1), "label"] = 1
    df["is_rated"] = True
    df["canonical_id"] = ["C0"] * n
    df["run_id"] = list(range(n))
    return df


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


class TestR199LibSvmValidMissingFeatureColsNoKeyError(unittest.TestCase):
    """Round 199 #1: LibSVM has full features; valid_df subset must not KeyError."""

    def test_libsvm_when_valid_has_fewer_columns_completes(self):
        train_df = _make_train_rated(60, ["f1", "f2"], seed=42)
        valid_full = pd.DataFrame(
            {
                "f1": np.random.default_rng(43).random(30),
                "f2": np.random.default_rng(44).random(30),
            }
        )
        valid_full["label"] = (np.random.default_rng(45).random(30) > 0.5).astype(int)
        valid_full.loc[0, "label"] = 0
        valid_full.loc[1, "label"] = 1
        valid_full["is_rated"] = True
        valid_full["canonical_id"] = "C0"
        valid_full["run_id"] = range(30)

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tr_l, va_l = _libsvm_train_valid(train_df, valid_full, ["f1", "f2"], root)
            valid_one_col = valid_full[["f1", "label", "is_rated"]].copy()

            with unittest.mock.patch.object(trainer_mod, "DATA_DIR", root), unittest.mock.patch.object(
                trainer_mod, "A4_TWO_STAGE_ENABLE_TRAINING", False
            ):
                rated_art, _, _ = train_single_rated_model(
                    train_df,
                    valid_one_col,
                    ["f1", "f2"],
                    run_optuna=False,
                    test_df=None,
                    train_libsvm_paths=(tr_l, va_l),
                )
        self.assertIsNotNone(rated_art, "R199 #1: LibSVM with valid missing cols should complete.")


class TestR199ExportNoCommonParquetColumnsRaises(unittest.TestCase):
    """Round 199 #2: Valid Parquet missing a selected feature column → DuckDB/export failure."""

    def test_export_when_valid_missing_feature_raises(self):
        train_df = pd.DataFrame(
            {
                "f1": [0.1, 0.2],
                "label": [0, 1],
                "is_rated": [True, True],
                "canonical_id": ["C0", "C0"],
                "run_id": [0, 1],
            }
        )
        valid_df = pd.DataFrame(
            {
                "f2": [0.3, 0.4],
                "label": [0, 1],
                "is_rated": [True, True],
                "canonical_id": ["C0", "C0"],
                "run_id": [0, 1],
            }
        )
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            export_dir = root / "export"
            export_dir.mkdir(parents=True, exist_ok=True)
            tmp = root / "_pq"
            tmp.mkdir(exist_ok=True)
            trp, vlp = tmp / "train.parquet", tmp / "valid.parquet"
            train_df.to_parquet(trp, index=False)
            valid_df.to_parquet(vlp, index=False)
            with self.assertRaises(Exception):
                trainer_mod._export_parquet_to_libsvm(
                    trp, vlp, ["f1", "f2"], export_dir, test_path=None
                )
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
