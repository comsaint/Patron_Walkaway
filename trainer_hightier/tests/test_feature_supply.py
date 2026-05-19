"""Tests for fe_derived serving merge and supplyability helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.serving.feature_builder import join_fe_derived_snapshot
from trainer_hightier.serving.feature_supply import assert_feature_supplyability_or_raise


def test_join_fe_derived_snapshot_merges_on_bet_id(tmp_path: Path) -> None:
    fe_p = tmp_path / "fe.parquet"
    pd.DataFrame({"bet_id": [1.0, 2.0], "fe__wager_sum__w15m": [10.0, 20.0]}).to_parquet(fe_p, index=False)
    bets = pd.DataFrame({"bet_id": [1.0, 3.0], "player_id": [100, 300]})
    out = join_fe_derived_snapshot(bets, fe_p)
    assert "fe__wager_sum__w15m" in out.columns
    assert float(out.loc[out["bet_id"] == 1.0, "fe__wager_sum__w15m"].iloc[0]) == 10.0
    assert pd.isna(out.loc[out["bet_id"] == 3.0, "fe__wager_sum__w15m"].iloc[0])


def test_mid_term_manifest_stale_fails(tmp_path: Path) -> None:
    """Mid-term model features require fresh coverage_end_exclusive."""

    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: test-mid-stale
features:
  - feature_id: c
    group_id: g
    source: fe_derived
    status: active
    enabled_for: [baseline]
    time_horizon: mid_term
    max_lookback: P1D
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snap = load_candidate_registry(reg)
    fe_p = tmp_path / "fe.parquet"
    pd.DataFrame({"bet_id": [1.0], "c": [0.1]}).to_parquet(fe_p, index=False)
    stale_iso = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match=r"mid-term snapshot stale"):
        assert_feature_supplyability_or_raise(
            snap,
            ("c",),
            slow_pack_path=None,
            trial_pack_path=None,
            fe_pack_path=fe_p,
            manifest={"coverage_end_exclusive": stale_iso},
        )


def test_feast_trial_1h_passes_without_trial_parquet(tmp_path: Path) -> None:
    """Trial features are supplied online; optional bundled trial parquet is ignored."""

    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: test-trial-online
features:
  - feature_id: trial_x
    group_id: g
    source: feast_trial_1h
    status: active
    enabled_for: [baseline]
    time_horizon: short_term
    max_lookback: PT1H
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snap = load_candidate_registry(reg)
    summary = assert_feature_supplyability_or_raise(
        snap,
        ("trial_x",),
        slow_pack_path=None,
        trial_pack_path=None,
        fe_pack_path=None,
        manifest={"coverage_end_exclusive": "2099-01-01T00:00:00+00:00"},
    )
    assert summary["features"][0]["supplier"] == "online_trial_builder"
