"""Tests for main trainer integration with ``feature_candidate_registry.yaml``."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    FeatureScreeningPolicy,
    HighTierObjectiveConfig,
    SamplePolicy,
    Step4SplitConfig,
    Step5TrainConfig,
)
from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.trainer import HighTierTrainArgs, fit_model

_trainer_mod = importlib.import_module("trainer_hightier.trainer")

_b5_mod = importlib.import_module("trainer_hightier.05_lgbm_train")


def _write_minimal_step5_splits(
    splits_dir: Path,
    *,
    drop_columns: frozenset[str],
    feature_columns: tuple[str, ...],
) -> None:
    """Tiny Parquets with baseline + label + payout columns for Step 5 schema gate."""

    splits_dir.mkdir(parents=True, exist_ok=True)
    n = 96
    rng = np.random.default_rng(7)
    day0 = pd.Timestamp("2025-01-05")
    payout = pd.date_range(day0, periods=n, freq="h")
    rows: dict[str, object] = {
        _b5_mod.LABEL_COLUMN: np.array(([0, 1] * (n // 2 + 2))[:n], dtype=np.int8),
        _b5_mod.PAYOUT_TS_COLUMN: payout,
    }
    for c in feature_columns:
        if c in drop_columns:
            continue
        if c in _b5_mod.CAT_COLUMNS:
            rows[c] = [f"cv_{i % 3}" for i in range(n)]
        else:
            rows[c] = rng.standard_normal(n)
    df = pd.DataFrame(rows)
    for split_name in ("train", "val", "test"):
        df.to_parquet(splits_dir / f"{split_name}.parquet", index=False)


@patch("trainer_hightier.trainer._b5.train_lgbm_from_splits")
def test_fit_model_passes_registry_feature_columns(mock_train: MagicMock, tmp_path: Path) -> None:
    """Registry baseline columns must be forwarded as ``feature_columns`` to Step 5."""

    snap0 = load_candidate_registry(None)
    splits = tmp_path / "splits"
    _write_minimal_step5_splits(splits, drop_columns=frozenset(), feature_columns=snap0.model_feature_columns)
    mock_train.return_value = MagicMock(
        report={"val_ap": 0.1},
        model_path=tmp_path / "model.pkl",
        metrics_path=tmp_path / "m.json",
        threshold=0.5,
    )
    metrics: dict[str, object] = {}
    args = HighTierTrainArgs(
        output_dir=tmp_path / "out",
        duckdb_runtime=DuckDbRuntimeConfig(),
        objective=HighTierObjectiveConfig(),
        step4_split=Step4SplitConfig(splits_output_dir=splits.resolve()),
        step5=Step5TrainConfig(run_step5=True),
    )
    fit_model(args, metrics=metrics)

    snap = load_candidate_registry(None)
    feat_cols_expected = snap.model_feature_columns
    kwargs = mock_train.call_args.kwargs
    assert kwargs["feature_columns"] == feat_cols_expected
    assert "candidate_registry" in metrics
    assert metrics["candidate_registry"]["resolved_path"].endswith("feature_candidate_registry.yaml")
    assert int(metrics["candidate_registry"]["n_baseline_features"]) == len(feat_cols_expected)


def test_fit_model_registry_missing_file_raises(tmp_path: Path) -> None:
    """Explicit registry path must exist or loader raises FileNotFoundError."""

    snap0 = load_candidate_registry(None)
    splits = tmp_path / "splits"
    _write_minimal_step5_splits(
        splits,
        drop_columns=frozenset(),
        feature_columns=snap0.model_feature_columns,
    )
    missing = tmp_path / "no_such_registry.yaml"
    args = HighTierTrainArgs(
        output_dir=tmp_path / "out",
        duckdb_runtime=DuckDbRuntimeConfig(),
        objective=HighTierObjectiveConfig(),
        step4_split=Step4SplitConfig(splits_output_dir=splits.resolve()),
        step5=Step5TrainConfig(run_step5=True),
        feature_candidate_registry=missing,
    )
    with pytest.raises(FileNotFoundError, match="Feature candidate registry file not found"):
        fit_model(args, metrics=None)


@patch("trainer_hightier.trainer._b5.train_lgbm_from_splits")
def test_fit_model_wires_screening_and_sampling_into_step5(
    mock_train: MagicMock,
    tmp_path: Path,
) -> None:
    """Step 5 must receive sampled train parquet plus screening/sampling disclosure blocks."""

    snap0 = load_candidate_registry(None)
    baseline = snap0.model_feature_columns
    manifest_p = tmp_path / "selected_features.json"
    selected = list(baseline[: max(2, len(baseline) // 2)])
    manifest_p.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "feature_screening_manifest_v1",
                "selected_features": selected,
                "method": "fqg_allowlist_v0",
                "fqg_version": "v0",
                "evidence_refs": [],
            },
        ),
        encoding="utf-8",
    )
    splits = tmp_path / "splits"
    _write_minimal_step5_splits(splits, drop_columns=frozenset(), feature_columns=baseline)
    mock_train.return_value = MagicMock(
        report={"val_ap": 0.1},
        model_path=tmp_path / "model.pkl",
        metrics_path=tmp_path / "m.json",
        threshold=0.5,
    )
    metrics: dict[str, object] = {
        "training_acceleration_policy": {},
    }
    args = HighTierTrainArgs(
        output_dir=tmp_path / "out",
        duckdb_runtime=DuckDbRuntimeConfig(),
        objective=HighTierObjectiveConfig(),
        step4_split=Step4SplitConfig(splits_output_dir=splits.resolve()),
        step5=Step5TrainConfig(run_step5=True, skip_optuna=True),
        feature_screening_policy=FeatureScreeningPolicy(enabled=True, manifest_path=manifest_p),
        sample_policy=SamplePolicy(neg_sample_frac=0.5, neg_sample_seed=3),
    )
    fit_model(args, metrics=metrics)

    kwargs = mock_train.call_args.kwargs
    assert kwargs["feature_columns"] == tuple(selected)
    assert kwargs["sample_policy_meta"]["enabled"] is True
    assert kwargs["feature_screening_meta"]["enabled"] is True
    assert kwargs["train_parquet"].resolve() == (splits / "train_sampled.parquet").resolve()
    assert metrics["negative_sampling_summary"]["enabled"] is True
    assert metrics["feature_screening_summary"]["selected_feature_count"] == len(selected)
    accel = metrics["training_acceleration_policy"]
    assert isinstance(accel, dict)
    assert accel["negative_sampling_summary"]["enabled"] is True
    assert accel["feature_screening_summary"]["selected_feature_count"] == len(selected)


def test_fit_model_preflight_missing_parquet_column(tmp_path: Path) -> None:
    """Splits without a registry baseline column fail before Step 5 with a registry-aware message."""

    snap0 = load_candidate_registry(None)
    splits = tmp_path / "splits"
    _write_minimal_step5_splits(
        splits,
        drop_columns=frozenset({"wager"}),
        feature_columns=snap0.model_feature_columns,
    )
    args = HighTierTrainArgs(
        output_dir=tmp_path / "out",
        duckdb_runtime=DuckDbRuntimeConfig(),
        objective=HighTierObjectiveConfig(),
        step4_split=Step4SplitConfig(splits_output_dir=splits.resolve()),
        step5=Step5TrainConfig(run_step5=True),
    )
    with pytest.raises(ValueError, match="missing baseline columns.*wager"):
        fit_model(args, metrics=None)


def test_freeze_deploy_inputs_manifest_has_freshness_metadata(monkeypatch, tmp_path: Path) -> None:
    """Training-side deploy manifest must satisfy packaging mid-term freshness gate inputs."""

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    map_p = tmp_path / "canonical_player_mapping.parquet"
    allow_p = tmp_path / "adt_allowed_players_q0p99.parquet"
    slow_p = tmp_path / "slow_patron_180d_monthly.parquet"
    pd.DataFrame({"player_id": [1], "canonical_id": [10]}).to_parquet(map_p, index=False)
    pd.DataFrame({"player_id": [1]}).to_parquet(allow_p, index=False)
    pd.DataFrame({"player_id": [1], "gaming_day_event": [20250101]}).to_parquet(slow_p, index=False)
    (bundle_dir / "feature_candidate_registry.snapshot.yaml").write_text(
        "registry_version: test\nfeatures: []\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(_trainer_mod, "default_canonical_mapping_parquet_path", lambda: map_p)
    monkeypatch.setattr(_trainer_mod, "default_adt_allowed_players_parquet_path", lambda _q: allow_p)
    monkeypatch.setattr(_trainer_mod, "default_slow_patron_180d_monthly_parquet_path", lambda: slow_p)

    metrics: dict[str, object] = {"training_cutoff_iso": "2026-05-19T00:00:00+00:00"}
    args = HighTierTrainArgs(
        output_dir=tmp_path / "out",
        objective=HighTierObjectiveConfig(theo_train_quantile=0.99),
    )
    _trainer_mod._freeze_deploy_inputs(
        args,
        metrics,
        bundle_dir=bundle_dir,
        model_version="mv-test",
    )

    manifest = json.loads((bundle_dir / "deploy_inputs" / "active_manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "mv-test"
    assert manifest["model_version"] == "mv-test"
    assert manifest["training_cutoff_iso"] == "2026-05-19T00:00:00+00:00"
    assert isinstance(manifest["coverage_end_exclusive"], str)
    pd.Timestamp(manifest["coverage_end_exclusive"])
