"""Unit tests for Time-CV runner path and column resolution helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.feature_experiment.time_cv.fold_definitions import GAMING_DAY_COLUMN
from trainer_hightier.feature_experiment.time_cv.runner import (
    _cv_pool_parquet_paths,
    _feature_columns_present,
)


def test_cv_pool_parquet_paths_prefers_enriched_file(tmp_path: Path) -> None:
    enriched = tmp_path / "training_set_fe_enriched.parquet"
    enriched.write_bytes(b"stub")
    splits = tmp_path / "splits"
    splits.mkdir()
    got = _cv_pool_parquet_paths(enriched_parquet=enriched, splits_dir=splits)
    assert got == (enriched.resolve(),)


def test_cv_pool_parquet_paths_uses_train_and_val_splits(tmp_path: Path) -> None:
    splits = tmp_path / "splits"
    splits.mkdir()
    train_p = splits / "train.parquet"
    val_p = splits / "val.parquet"
    train_p.write_bytes(b"train")
    val_p.write_bytes(b"val")
    got = _cv_pool_parquet_paths(enriched_parquet=None, splits_dir=splits)
    assert got == (train_p.resolve(), val_p.resolve())


def test_cv_pool_parquet_paths_train_sampled_split(tmp_path: Path) -> None:
    splits = tmp_path / "splits"
    splits.mkdir()
    sampled = splits / "train_sampled.parquet"
    val_p = splits / "val.parquet"
    sampled.write_bytes(b"sampled")
    val_p.write_bytes(b"val")
    got = _cv_pool_parquet_paths(
        enriched_parquet=None,
        splits_dir=splits,
        train_split="train_sampled",
    )
    assert got == (sampled.resolve(), val_p.resolve())


def test_cv_pool_parquet_paths_raises_when_split_missing(tmp_path: Path) -> None:
    splits = tmp_path / "splits"
    splits.mkdir()
    with pytest.raises(FileNotFoundError, match="missing"):
        _cv_pool_parquet_paths(enriched_parquet=None, splits_dir=splits)


def test_feature_columns_present_filters_to_parquet_schema(tmp_path: Path) -> None:
    parquet = tmp_path / "fold.parquet"
    pd.DataFrame(
        {
            GAMING_DAY_COLUMN: ["2026-01-01"],
            "fe__a": [1.0],
            "fe__b": [2.0],
        }
    ).to_parquet(parquet, index=False)
    got = _feature_columns_present(parquet, ("fe__a", "fe__missing", "fe__b"))
    assert got == ("fe__a", "fe__b")
