"""Unit checks for :mod:`trainer_hightier.feature_experiment.feature_quality_gate`."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from trainer_hightier.config import FeatureQualityGateConfig
from trainer_hightier.feature_experiment.feature_quality_gate import run_feature_quality_gate


def _write_minimal_splits(
    dest: Path,
    *,
    col_values: tuple[np.ndarray, np.ndarray, np.ndarray],
    col_name: str = "feat_x",
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    n_each = len(col_values[0])
    for split_name, arr in zip(("train", "val", "test"), col_values, strict=True):
        n = len(arr)
        day0 = pd.Timestamp("2024-06-01")
        df = pd.DataFrame(
            {
                "gaming_day": [day0 + pd.Timedelta(days=i % 180) for i in range(n)],
                "walkaway_label": ([0, 1] * ((n // 2) + 2))[:n],
                col_name: arr.astype(float),
            },
        )
        df.to_parquet(dest / f"{split_name}.parquet", index=False)


def test_fqg_blocks_constant_feature(tmp_path: Path) -> None:
    splits = tmp_path / "splits_const"
    n = 512
    const = np.ones(n, dtype=np.float64)
    _write_minimal_splits(splits, col_values=(const, const.copy(), const.copy()))
    cfg = FeatureQualityGateConfig(random_seed=1, max_rows_per_split=min(4096, n))
    result = run_feature_quality_gate(
        splits_dir=splits,
        candidate_feature_columns=("feat_x",),
        cfg=cfg,
    )
    assert not result.fqg_pass
    codes = [b["reason_code"] for b in result.blocklist]
    assert any("constant" in c or "near_constant" in c or "l1_constant_unique" == c or "blocked" == c for c in codes)


def test_fqg_baseline_unique_constant_soft_warn_allowlisted(tmp_path: Path) -> None:
    """Registry baseline columns: nunique==1 under sample → WARN, auto-approved like other baseline WARNs."""

    splits = tmp_path / "splits_soft_const"
    n = 512
    const = np.ones(n, dtype=np.float64)
    _write_minimal_splits(splits, col_values=(const, const.copy(), const.copy()), col_name="wager_nn")
    cfg = FeatureQualityGateConfig(random_seed=1, max_rows_per_split=min(4096, n))
    result = run_feature_quality_gate(
        splits_dir=splits,
        candidate_feature_columns=("wager_nn",),
        cfg=cfg,
    )
    assert result.fqg_pass
    assert "wager_nn" in result.allowlist
    assert result.blocklist == []


def test_fqg_passes_non_constant_feature(tmp_path: Path) -> None:
    splits = tmp_path / "splits_ok"
    rng = np.random.default_rng(43)
    n = 2048
    _write_minimal_splits(
        splits,
        col_values=(rng.standard_normal(n), rng.standard_normal(n), rng.standard_normal(max(900, int(n / 6)))),
    )
    cfg = FeatureQualityGateConfig(random_seed=2, max_rows_per_split=min(4096, n))
    result = run_feature_quality_gate(splits_dir=splits, candidate_feature_columns=("feat_x",), cfg=cfg)
    assert result.fqg_pass
    assert "feat_x" in result.allowlist
