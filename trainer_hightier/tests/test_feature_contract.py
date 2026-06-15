"""Tests for ``trainer_hightier.serving.feature_contract``."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.config import DEPLOY_CONTRACT_FILENAME
from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.serving.feature_contract import (
    BUNDLE_STATIC_SUPPLIER_ID,
    assert_contract_matches_recomputed,
    assert_no_legacy_feast_trial_cols,
    build_and_write_deploy_contract,
    build_deploy_contract,
    load_deploy_contract_json,
    resolve_supplier_requirements,
    run_supplier_contract_gate,
)
from trainer_hightier.serving.feature_supply import ScorerSupplierPlan, build_scorer_supplier_plan
from trainer_hightier.tests.test_scorer_v2_feast import _write_min_registry


def _empty_plan(**overrides: tuple[str, ...]) -> ScorerSupplierPlan:
    base = ScorerSupplierPlan(
        baseline_cols=(),
        feast_trial_cols=(),
        short_term_cols=(),
        txn_cols=(),
        feast_mid_cols=(),
        feast_slow_cols=(),
        mid_composite_cols=(),
        unknown_cols=(),
    )
    return replace(base, **overrides)


def _write_mapping(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"player_id": ["p1"], "canonical_id": ["c1"]}).to_parquet(path, index=False)


def _write_allowlist(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"player_id": ["p1"]}).to_parquet(path, index=False)


def test_resolve_includes_bundle_static_even_without_plan_buckets(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg, include_mid=False, include_slow=True)
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, ("wager", "player_id", "patron__adt__w180d_m1snap"))
    reqs = resolve_supplier_requirements(plan, include_bundle_static=True)
    assert any(r.supplier_id == BUNDLE_STATIC_SUPPLIER_ID for r in reqs)
    assert any(r.supplier_id == "clickhouse_raw" for r in reqs)
    assert any(r.supplier_id == "feast_online_slow" for r in reqs)


def test_assert_no_legacy_feast_trial_cols_raises() -> None:
    plan = _empty_plan(feast_trial_cols=("bet__bets_cnt__w1h",))
    with pytest.raises(ValueError, match="feast_trial_cols"):
        assert_no_legacy_feast_trial_cols(plan)


def test_build_deploy_contract_stable_fingerprint(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg, include_mid=True, include_slow=True)
    snap = load_candidate_registry(reg)
    feats = ("wager", "player_id", "fe__bets_cnt__w1d", "patron__adt__w180d_m1snap")
    plan = build_scorer_supplier_plan(snap, feats)
    reqs = resolve_supplier_requirements(plan)
    c1 = build_deploy_contract(
        plan=plan,
        requirements=reqs,
        model_version="mv-test",
        feature_count=len(feats),
        registry_fingerprint="abc123",
    )
    c2 = build_deploy_contract(
        plan=plan,
        requirements=reqs,
        model_version="mv-test",
        feature_count=len(feats),
        registry_fingerprint="abc123",
    )
    assert c1.contract_fingerprint == c2.contract_fingerprint
    assert c1.schema_version == "deploy_contract_v1"
    assert c1.flags["deploy_requires_feast_online"] is True


def test_assert_contract_matches_recomputed_fails_on_feature_count(tmp_path: Path) -> None:
    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg)
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, ("wager", "player_id"))
    reqs = resolve_supplier_requirements(plan)
    contract = build_deploy_contract(
        plan=plan,
        requirements=reqs,
        model_version="mv",
        feature_count=2,
        registry_fingerprint="fp",
    )
    with pytest.raises(ValueError, match="feature_count"):
        assert_contract_matches_recomputed(
            contract,
            plan=plan,
            requirements=reqs,
            registry_fingerprint="fp",
            feature_count=3,
        )


def test_write_load_and_package_contract(tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle"
    models_dir = bundle_root / "models"
    models_dir.mkdir(parents=True)
    mapping = bundle_root / "mapping" / "canonical_player_mapping.parquet"
    allowlist = bundle_root / "mapping" / "adt_allowed_players_q0p99.parquet"
    _write_mapping(mapping)
    _write_allowlist(allowlist)

    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg, include_mid=False, include_slow=True)
    snap = load_candidate_registry(reg)
    feats = ("wager", "player_id", "patron__adt__w180d_m1snap")
    plan = build_scorer_supplier_plan(snap, feats)
    detail = build_and_write_deploy_contract(
        plan=plan,
        model_bundle_dir=models_dir,
        model_version="mv-pack",
        registry_fingerprint="regfp",
        feature_count=len(feats),
        bundle_root=bundle_root,
        mapping=mapping,
        allowlist=allowlist,
    )
    contract_path = models_dir / DEPLOY_CONTRACT_FILENAME
    assert contract_path.is_file()
    loaded = load_deploy_contract_json(contract_path)
    assert loaded.contract_fingerprint == detail["contract_fingerprint"]
    assert loaded.flags["deploy_requires_clickhouse"] is True


def test_run_supplier_contract_gate_report_only_without_contract(tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle2"
    model_bundle = bundle_root / "models"
    model_bundle.mkdir(parents=True)
    mapping = bundle_root / "mapping" / "canonical_player_mapping.parquet"
    allowlist = bundle_root / "mapping" / "adt_allowed_players_q0p99.parquet"
    _write_mapping(mapping)
    _write_allowlist(allowlist)
    reg = tmp_path / "registry.yaml"
    _write_min_registry(reg, include_mid=False, include_slow=False)
    snap = load_candidate_registry(reg)
    plan = build_scorer_supplier_plan(snap, ("wager", "player_id"))
    detail = run_supplier_contract_gate(
        bundle_root=bundle_root,
        model_bundle=model_bundle,
        plan=plan,
        registry_fingerprint="fp",
        feature_count=2,
        stage="deploy_preflight",
        strict=False,
    )
    assert detail["cross_check"] == "missing_contract"
