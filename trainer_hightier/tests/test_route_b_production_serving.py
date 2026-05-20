"""Route B production feature serving: materialize metadata, joins, and supply gates."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.config import SLOW_PATRON_GRAIN_CANONICAL_ASOF
from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.serving.feature_builder import (
    attach_canonical_id,
    join_fe_derived_snapshot,
    join_slow_patron_snapshot,
)
from trainer_hightier.serving.feature_supply import (
    assert_feature_supplyability_or_raise,
    audit_feature_supplier_routes,
)
from trainer_hightier.serving.production_materialize import (
    DEFAULT_MODEL_FE_DERIVED_COLUMNS,
    shadow_validate_route_b_features,
)


def test_audit_feature_supplier_routes_classifies_mvp_model(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: route-b-audit
features:
  - feature_id: wager
    group_id: g
    source: baseline_model
    status: active
    enabled_for: [baseline]
    time_horizon: none
  - feature_id: fe__wager_sum__w15m
    group_id: g
    source: fe_derived
    status: active
    enabled_for: [baseline]
    time_horizon: short_term
    max_lookback: PT1H
  - feature_id: patron__adt__w180d_m1snap
    group_id: g
    source: feast_slow_180d
    status: active
    enabled_for: [baseline]
    time_horizon: long_term
    max_lookback: P180D
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snap = load_candidate_registry(reg)
    feats = ("wager", "fe__wager_sum__w15m", "patron__adt__w180d_m1snap")
    man = {
        "fe_derived_source_kind": "production_clickhouse",
        "slow_patron_grain": SLOW_PATRON_GRAIN_CANONICAL_ASOF,
    }
    out = audit_feature_supplier_routes(snap, feats, fe_bundled=True, manifest=man)
    by_id = {r["feature_id"]: r["supplier"] for r in out["features"]}
    assert by_id["wager"] == "clickhouse_raw"
    assert by_id["fe__wager_sum__w15m"] == "fe_short_term_parquet"
    assert "canonical_asof" in by_id["patron__adt__w180d_m1snap"]


def test_shadow_validate_route_b_non_null(tmp_path: Path) -> None:
    fe_p = tmp_path / "fe.parquet"
    slow_p = tmp_path / "slow.parquet"
    pd.DataFrame(
        {"bet_id": [1.0, 2.0], **{c: [1.0, 2.0] for c in DEFAULT_MODEL_FE_DERIVED_COLUMNS}}
    ).to_parquet(fe_p, index=False)
    pd.DataFrame(
        {
            "canonical_id": ["c1"],
            "anchor_gaming_day": pd.to_datetime(["2025-01-01"]),
            "patron__theo_win_sum__w180d_m1snap": [100.0],
            "patron__gaming_days_cnt__w180d_m1snap": [10],
            "patron__adt__w180d_m1snap": [10.0],
        }
    ).to_parquet(slow_p, index=False)

    bets = pd.DataFrame(
        {
            "bet_id": [1.0],
            "player_id": [42],
            "gaming_day": pd.to_datetime(["2025-06-01"]),
        }
    )
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [42], "canonical_id": ["c1"]}).to_parquet(cmap, index=False)
    staged = attach_canonical_id(bets, mapping_parquet=cmap)
    staged = join_slow_patron_snapshot(staged, slow_p, slow_grain=SLOW_PATRON_GRAIN_CANONICAL_ASOF)
    staged = join_fe_derived_snapshot(staged, fe_p)

    rep = shadow_validate_route_b_features(staged, feature_columns=DEFAULT_MODEL_FE_DERIVED_COLUMNS)
    assert rep["fe_features_missing_max"] == 0
    assert rep["slow_null_fraction_max"] < 0.5


def test_production_supply_gate_rejects_training_fe_path(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: route-b-gate
features:
  - feature_id: fe__wager_sum__w15m
    group_id: g
    source: fe_derived
    status: active
    enabled_for: [baseline]
    time_horizon: short_term
    max_lookback: PT1H
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snap = load_candidate_registry(reg)
    fe_p = tmp_path / "training_data" / "_main_trainer_fe_derived.parquet"
    fe_p.parent.mkdir(parents=True)
    pd.DataFrame({"bet_id": [1.0], "fe__wager_sum__w15m": [1.0]}).to_parquet(fe_p, index=False)
    man = {
        "coverage_end_exclusive": "2099-01-01T00:00:00+00:00",
        "fe_derived_source_kind": "production_clickhouse",
    }
    with pytest.raises(ValueError, match=r"training artifact"):
        assert_feature_supplyability_or_raise(
            snap,
            ("fe__wager_sum__w15m",),
            slow_pack_path=None,
            trial_pack_path=None,
            fe_pack_path=fe_p,
            fe_short_term_pack_path=fe_p,
            manifest=man,
        )
