"""Unit tests for Step 4 dataset arrange and ``gaming_day`` splits."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig, Step4SplitConfig

_b4 = importlib.import_module("trainer_hightier.04_split_dataset")


def _tiny_duckdb(tmp_path: Path) -> DuckDbRuntimeConfig:
    """Small DuckDB settings for unit tests."""

    return DuckDbRuntimeConfig(
        memory_limit="512MB",
        temp_directory=tmp_path / "duck_tmp",
        threads=1,
    )


def test_step4_splits_rows_and_writes_report(tmp_path: Path) -> None:
    """Ten calendar days: 70/15/15 day buckets produce three Parquets and full row count."""

    days = [f"2024-01-{i:02d}" for i in range(1, 11)]
    rows = []
    for i, gd in enumerate(days):
        rows.append(
            {
                "bet_id": float(i),
                "walkaway_label": i % 2,
                "walkaway_censored": False,
                "canonical_id": f"c{i % 3}",
                "gaming_day_event": gd,
                "wager": "100.5",
            }
        )
    inp = tmp_path / "features.parquet"
    pd.DataFrame(rows).to_parquet(inp, index=False)
    out = tmp_path / "splits"
    res = _b4.arrange_and_split_training_data(
        features_parquet=inp,
        duckdb_runtime=_tiny_duckdb(tmp_path),
        step4=Step4SplitConfig(train_day_fraction=0.7, val_day_fraction=0.15),
        splits_output_dir=out,
    )
    assert res.train_parquet.is_file()
    assert res.val_parquet.is_file()
    assert res.test_parquet.is_file()
    rep = json.loads(res.split_report_json.read_text(encoding="utf-8"))
    total = sum(s["row_count"] for s in rep["splits"])
    assert total == len(rows)
    assert rep["distinct_gaming_days"] == 10
    assert rep["censored_rows_excluded"] == 0

    train_cols = pq.read_schema(res.train_parquet).names
    assert "walkaway_censored" not in train_cols


def test_step4_drops_censored_rows_and_removes_column(tmp_path: Path) -> None:
    """Censored rows are excluded; ``walkaway_censored`` is not in output Parquets."""

    days = [f"2024-01-{i:02d}" for i in range(1, 11)]
    rows = []
    for i, gd in enumerate(days):
        rows.append(
            {
                "bet_id": float(i),
                "walkaway_label": i % 2,
                "walkaway_censored": i == 3,
                "canonical_id": f"c{i % 3}",
                "gaming_day_event": gd,
            }
        )
    inp = tmp_path / "features.parquet"
    pd.DataFrame(rows).to_parquet(inp, index=False)
    out = tmp_path / "splits"
    rep = _b4.arrange_and_split_training_data(
        features_parquet=inp,
        duckdb_runtime=_tiny_duckdb(tmp_path),
        step4=Step4SplitConfig(train_day_fraction=0.7, val_day_fraction=0.15),
        splits_output_dir=out,
    ).report
    total = sum(s["row_count"] for s in rep["splits"])
    assert total == len(rows) - 1
    assert rep["censored_rows_excluded"] == 1
    for name in ("train", "val", "test"):
        p = Path(rep["outputs"][name])
        assert "walkaway_censored" not in pq.read_schema(p).names


def test_step4_schema_gate_missing_canonical_id(tmp_path: Path) -> None:
    """Fail fast when required split key columns are absent."""

    inp = tmp_path / "bad.parquet"
    pd.DataFrame(
        {
            "walkaway_label": [0],
            "walkaway_censored": [False],
            "gaming_day_event": ["2024-01-01"],
            "bet_id": [1.0],
        }
    ).to_parquet(inp, index=False)
    with pytest.raises(ValueError, match="schema gate"):
        _b4.arrange_and_split_training_data(
            features_parquet=inp,
            duckdb_runtime=_tiny_duckdb(tmp_path),
            splits_output_dir=tmp_path / "splits",
        )


def test_step4_invalid_day_fractions(tmp_path: Path) -> None:
    """Train + val fractions must stay below 1.0."""

    inp = tmp_path / "features.parquet"
    pd.DataFrame(
        {
            "bet_id": [1.0],
            "walkaway_label": [0],
            "walkaway_censored": [False],
            "canonical_id": ["a"],
            "gaming_day_event": ["2024-01-01"],
        }
    ).to_parquet(inp, index=False)
    with pytest.raises(ValueError, match="train_day_fraction"):
        _b4.arrange_and_split_training_data(
            features_parquet=inp,
            duckdb_runtime=_tiny_duckdb(tmp_path),
            step4=Step4SplitConfig(train_day_fraction=0.7, val_day_fraction=0.35),
            splits_output_dir=tmp_path / "splits",
        )


def test_step4_split_parquet_row_order_is_deterministic(tmp_path: Path) -> None:
    """Two Step 4 runs on the same input must emit identical ``bet_id`` order per split."""

    days = [f"2024-01-{i:02d}" for i in range(1, 11)]
    rows = []
    for i, gd in enumerate(days):
        rows.append(
            {
                "bet_id": float(1000 - i),
                "walkaway_label": i % 2,
                "walkaway_censored": False,
                "canonical_id": f"c{i % 3}",
                "gaming_day_event": gd,
                "payout_complete_dtm": f"2024-01-{i:02d}T12:00:00+08:00",
            }
        )
    inp = tmp_path / "features.parquet"
    pd.DataFrame(rows).to_parquet(inp, index=False)
    duck = _tiny_duckdb(tmp_path)
    cfg = Step4SplitConfig(train_day_fraction=0.7, val_day_fraction=0.15)

    def train_bet_ids(run_dir: Path) -> list[float]:
        res = _b4.arrange_and_split_training_data(
            features_parquet=inp,
            duckdb_runtime=duck,
            step4=cfg,
            splits_output_dir=run_dir,
        )
        return pd.read_parquet(res.train_parquet, columns=["bet_id"])["bet_id"].tolist()

    first = train_bet_ids(tmp_path / "splits_a")
    second = train_bet_ids(tmp_path / "splits_b")
    assert first == second
