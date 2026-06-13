"""Tests for fe_derived serving merge and supplyability helpers."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.config import (
    FE_DERIVED_SOURCE_KIND_PRODUCTION,
    MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
    MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    MID_TERM_SNAPSHOT_SCOPE_TRAINING,
)
from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    MID_TERM_SNAPSHOT_OUTPUT_COLUMNS,
)
from trainer_hightier.serving.feature_builder import join_fe_derived_snapshot
from trainer_hightier.serving.feature_supply import assert_feature_supplyability_or_raise
from trainer_hightier.serving.snapshot_freshness import expected_mid_term_anchor


def _write_mid_term_snapshot(
    path: Path,
    *,
    anchor: date | None = None,
    snapshot_scope: str = MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
) -> None:
    anchor_day = anchor or expected_mid_term_anchor(date.today())
    row: dict[str, object] = {
        "canonical_id": ["c1"],
        "anchor_gaming_day_event": [anchor_day.isoformat()],
    }
    for col in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS:
        if col.startswith("fe__"):
            row[col] = [1.0]
    pd.DataFrame(row).to_parquet(path, index=False)
    meta = path.parent / f"{path.stem}.meta.json"
    meta.write_text(json.dumps({"snapshot_scope": snapshot_scope}), encoding="utf-8")


def test_join_fe_derived_snapshot_merges_on_bet_id(tmp_path: Path) -> None:
    fe_p = tmp_path / "fe.parquet"
    pd.DataFrame({"bet_id": [1.0, 2.0], "fe__wager_sum__w15m": [10.0, 20.0]}).to_parquet(fe_p, index=False)
    bets = pd.DataFrame({"bet_id": [1.0, 3.0], "player_id": [100, 300]})
    out = join_fe_derived_snapshot(bets, fe_p)
    assert "fe__wager_sum__w15m" in out.columns
    assert float(out.loc[out["bet_id"] == 1.0, "fe__wager_sum__w15m"].iloc[0]) == 10.0
    assert pd.isna(out.loc[out["bet_id"] == 3.0, "fe__wager_sum__w15m"].iloc[0])


def test_short_term_fe_passes_with_fe_short_term_parquet(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: test-short-fe
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
    fe_p = tmp_path / "fe_short.parquet"
    pd.DataFrame({"bet_id": [1.0], "fe__wager_sum__w15m": [1.0]}).to_parquet(fe_p, index=False)
    summary = assert_feature_supplyability_or_raise(
        snap,
        ("fe__wager_sum__w15m",),
        slow_pack_path=None,
        trial_pack_path=None,
        fe_pack_path=None,
        fe_short_term_pack_path=fe_p,
        manifest={
            "coverage_end_exclusive": "2099-01-01T00:00:00+00:00",
            "fe_derived_source_kind": FE_DERIVED_SOURCE_KIND_PRODUCTION,
        },
    )
    assert summary["fe_short_term_column_count"] == 1


def test_mid_term_fe_fails_without_mid_term_snapshot(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: test-mid-miss
features:
  - feature_id: fe__bets_cnt__w1d
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
    with pytest.raises(ValueError, match=r"mid_term_snapshot_parquet"):
        assert_feature_supplyability_or_raise(
            snap,
            ("fe__bets_cnt__w1d",),
            slow_pack_path=None,
            trial_pack_path=None,
            fe_pack_path=None,
            manifest={
                "coverage_end_exclusive": "2099-01-01T00:00:00+00:00",
                "fe_derived_source_kind": FE_DERIVED_SOURCE_KIND_PRODUCTION,
            },
        )


def test_legacy_fe_derived_alone_does_not_satisfy_mid_term_gate(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: test-legacy-mid
features:
  - feature_id: fe__bets_cnt__w1d
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
    fe_p = tmp_path / "fe_legacy.parquet"
    pd.DataFrame({"bet_id": [1.0], "fe__bets_cnt__w1d": [0.1]}).to_parquet(fe_p, index=False)
    with pytest.raises(ValueError, match=r"legacy fe_derived_parquet does not satisfy"):
        assert_feature_supplyability_or_raise(
            snap,
            ("fe__bets_cnt__w1d",),
            slow_pack_path=None,
            trial_pack_path=None,
            fe_pack_path=fe_p,
            manifest={
                "coverage_end_exclusive": "2099-01-01T00:00:00+00:00",
                "fe_derived_source_kind": FE_DERIVED_SOURCE_KIND_PRODUCTION,
            },
        )


def test_training_mid_term_snapshot_rejected(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: test-mid-train
features:
  - feature_id: fe__bets_cnt__w1d
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
    mid_p = tmp_path / "mid.parquet"
    _write_mid_term_snapshot(mid_p, snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_TRAINING)
    with pytest.raises(ValueError, match=r"not production-safe"):
        assert_feature_supplyability_or_raise(
            snap,
            ("fe__bets_cnt__w1d",),
            slow_pack_path=None,
            trial_pack_path=None,
            fe_pack_path=None,
            mid_term_pack_path=mid_p,
            manifest={
                "coverage_end_exclusive": "2099-01-01T00:00:00+00:00",
                "fe_derived_source_kind": FE_DERIVED_SOURCE_KIND_PRODUCTION,
                "mid_term_grain": MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
            },
        )


def test_mid_term_manifest_stale_fails(tmp_path: Path) -> None:
    """Mid-term model features require fresh mid_term_snapshot when only coverage metadata exists."""

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
    stale_iso = "2020-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match=r"mid_term_snapshot_parquet|mid-term snapshot stale"):
        assert_feature_supplyability_or_raise(
            snap,
            ("c",),
            slow_pack_path=None,
            trial_pack_path=None,
            fe_pack_path=None,
            manifest={
                "coverage_end_exclusive": stale_iso,
                "fe_derived_source_kind": FE_DERIVED_SOURCE_KIND_PRODUCTION,
            },
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
    assert summary["features"][0]["supplier"] == "short_term_pit_builder"


def test_mid_term_audit_model_columns_pass_without_registry_rows(tmp_path: Path) -> None:
    """Audit columns in model.pkl must not fail supplier plan when frozen snapshot omits them."""

    from trainer_hightier.serving.feature_supply import (
        assert_scorer_supplier_plan_or_raise,
        build_scorer_supplier_plan,
    )

    reg = tmp_path / "registry.yaml"
    reg.write_text(
        """
registry_version: t
features:
  - feature_id: wager
    group_id: g
    source: baseline_model
    status: active
    enabled_for: [baseline]
    time_horizon: none
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snap = load_candidate_registry(reg)
    model_feats = (
        "wager",
        "mid_term_snapshot_age_days",
        "mid_term_snapshot_missing_flag",
    )
    plan = build_scorer_supplier_plan(snap, model_feats)
    assert plan.unknown_cols == ()
    assert_scorer_supplier_plan_or_raise(plan)


def test_bet_trial_pack_routes_to_short_term_pit_builder() -> None:
    from trainer_hightier.serving.feature_supply import build_scorer_supplier_plan

    snap = load_candidate_registry(None)
    plan = build_scorer_supplier_plan(
        snap,
        (
            "bet__bets_cnt__w1h",
            "bet__wager_sum__w1h",
            "bet__back_bet_ratio__w1h",
            "bet__payout_odds_avg__w1h",
        ),
    )
    assert plan.feast_trial_cols == ()
    assert set(plan.short_term_cols) == {
        "bet__bets_cnt__w1h",
        "bet__wager_sum__w1h",
        "bet__back_bet_ratio__w1h",
        "bet__payout_odds_avg__w1h",
    }


def test_default_registry_feast_schema_supports_payout_odds_w7d_composite() -> None:
    """Default registry composite route must pass scorer and Feast schema gates."""

    from trainer_hightier.serving.feature_supply import (
        assert_feast_plan_schema_support_or_raise,
        assert_scorer_supplier_plan_or_raise,
        build_scorer_supplier_plan,
    )

    snap = load_candidate_registry(None)
    plan = build_scorer_supplier_plan(snap, ("fe__odds__payout_odds_z__w7d",))
    assert plan.mid_composite_cols == ("fe__odds__payout_odds_z__w7d",)
    assert_scorer_supplier_plan_or_raise(plan)
    assert_feast_plan_schema_support_or_raise(plan)


def test_build_scorer_supplier_plan_routes_txn_lite_columns() -> None:
    """txn__* registry rows route to txn_lite_builder without unknown_cols."""

    from trainer_hightier.serving.feature_supply import (
        assert_scorer_supplier_plan_or_raise,
        build_scorer_supplier_plan,
        scorer_supplier_route_counts,
    )

    snap = load_candidate_registry(None)
    txn_cols = tuple(c for c in snap.model_feature_columns if c.startswith("txn__"))
    assert len(txn_cols) == 7
    plan = build_scorer_supplier_plan(snap, txn_cols)
    assert plan.txn_cols == txn_cols
    assert plan.unknown_cols == ()
    assert_scorer_supplier_plan_or_raise(plan)
    routes = scorer_supplier_route_counts(plan)
    assert routes["txn_lite_builder"] == 7
