"""Unit tests for high-roller segmentation helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer.training.high_roller_segmentation import (
    compute_high_roller_cutoff_from_train_parquet,
    validate_high_roller_theo_nonempty_on_rated_train,
)


def test_compute_high_roller_cutoff_from_train_parquet_duckdb_quantile_signature(
    tmp_path: Path,
) -> None:
    """Compute cutoff via DuckDB without parser errors on supported signature."""
    p = tmp_path / "train.parquet"
    df = pd.DataFrame(
        {
            "is_rated": [True, True, True, True, False],
            "theo_win_sum_30d": [10.0, 20.0, 30.0, 40.0, 1000.0],
        }
    )
    df.to_parquet(p, index=False)

    cutoff, meta = compute_high_roller_cutoff_from_train_parquet(
        p,
        "theo_win_sum_30d",
        0.90,
    )

    assert cutoff == pytest.approx(37.0)
    assert meta["high_roller_theo_feature"] == "theo_win_sum_30d"
    assert meta["high_roller_quantile"] == pytest.approx(0.90)
    assert meta["high_roller_rated_train_row_count"] == 4
    assert meta["high_roller_segment_train_rated_rows_low"] == 3
    assert meta["high_roller_segment_train_rated_rows_high"] == 1


def test_compute_high_roller_cutoff_constant_theo_yields_empty_low_segment(
    tmp_path: Path,
) -> None:
    """Constant COALESCE(theo,0) on rated rows: 90p equals min → low segment count 0."""
    p = tmp_path / "train.parquet"
    df = pd.DataFrame(
        {
            "is_rated": [True] * 100,
            "theo_win_sum_30d": [0.0] * 100,
        }
    )
    df.to_parquet(p, index=False)

    cutoff, meta = compute_high_roller_cutoff_from_train_parquet(
        p, "theo_win_sum_30d", 0.90
    )

    assert cutoff == pytest.approx(0.0)
    assert meta["high_roller_segment_train_rated_rows_low"] == 0
    assert meta["high_roller_segment_train_rated_rows_high"] == 100


def test_compute_high_roller_cutoff_raises_when_no_rated_train_rows(
    tmp_path: Path,
) -> None:
    p = tmp_path / "train.parquet"
    pd.DataFrame({"is_rated": [False], "theo_win_sum_30d": [1.0]}).to_parquet(
        p, index=False
    )

    with pytest.raises(RuntimeError, match="no rated train rows"):
        compute_high_roller_cutoff_from_train_parquet(p, "theo_win_sum_30d", 0.90)


def test_validate_high_roller_theo_nonempty_raises_when_all_null_on_rated(
    tmp_path: Path,
) -> None:
    """Issue #8 fail-fast: segmentation proxy must have ≥1 non-null rated train value."""
    p = tmp_path / "train.parquet"
    df = pd.DataFrame(
        {
            "is_rated": [True, True],
            "player_run_theo_sum_180d": [float("nan"), float("nan")],
        }
    )
    df.to_parquet(p, index=False)

    with pytest.raises(RuntimeError, match="non-null"):
        validate_high_roller_theo_nonempty_on_rated_train(p, "player_run_theo_sum_180d")


def test_validate_high_roller_theo_nonempty_passes_with_non_null(tmp_path: Path) -> None:
    p = tmp_path / "train.parquet"
    pd.DataFrame(
        {"is_rated": [True], "player_run_theo_sum_180d": [1.0]}
    ).to_parquet(p, index=False)
    validate_high_roller_theo_nonempty_on_rated_train(p, "player_run_theo_sum_180d")
