"""Tests for feature cadence contract, audit, and gates."""

from __future__ import annotations

from trainer_hightier.feature_experiment.candidate_registry_loader import (
    load_candidate_registry,
    load_registry_raw_feature_dicts,
)
from trainer_hightier.feature_experiment.feature_cadence import (
    ANCHOR_PRIOR_GAMING_DAY_END,
    CADENCE_DAILY_GAMING_DAY,
    SUPPLIER_MID_TERM_DAILY,
    SUPPLIER_SHORT_TERM_PIT,
    assert_feature_cadence_contract_or_raise,
    build_feature_cadence_audit,
    classify_model_fe_features,
    resolve_feature_cadence,
    short_term_enrich_columns_with_dependencies,
)
from trainer_hightier.serving.production_materialize import DEFAULT_MODEL_FE_DERIVED_COLUMNS


_DISABLED_BASELINE_MID: tuple[str, ...] = (
    "fe__bets_cnt__w1d",
    "fe__wager_sum__w15m_over_w1d",
    "fe__wager_cv_w7d",
    "fe__payout_odds_z_prior_w30d",
    "fe__interarrival__last_gap_z__w7d",
    "fe__odds__payout_odds_z__w7d",
)


def test_disabled_mid_term_features_still_resolve_daily_snapshot_supplier() -> None:
    """Disabled baseline mid-term ``fe__*`` keep daily snapshot cadence metadata."""

    snap = load_candidate_registry(None)
    raw_rows = load_registry_raw_feature_dicts(None)
    split = classify_model_fe_features(snap, _DISABLED_BASELINE_MID, raw_rows=raw_rows)
    assert "fe__wager_sum__w15m" not in split["mid_term"]
    assert set(split["mid_term"]) == set(_DISABLED_BASELINE_MID)
    for feat in split["mid_term"]:
        row = next(r for r in snap.rows if r.feature_id == feat)
        resolved = resolve_feature_cadence(row, next(x for x in raw_rows if x["feature_id"] == feat))
        assert resolved.cadence == CADENCE_DAILY_GAMING_DAY
        assert resolved.anchor_rule == ANCHOR_PRIOR_GAMING_DAY_END
        assert resolved.allowed_training_supplier == SUPPLIER_MID_TERM_DAILY


def test_short_term_dependency_columns_for_composites() -> None:
    """Composite mid-term enrich requires short-term numerator / interarrival inputs."""

    got = short_term_enrich_columns_with_dependencies(
        ("fe__wager_sum__w15m",),
        ("fe__wager_sum__w15m_over_w1d", "fe__interarrival__last_gap_z__w7d"),
    )
    assert "fe__time_since_last_bet_sec" in got
    assert got[0] == "fe__wager_sum__w15m"


def test_cadence_audit_has_no_legacy_mid_term_violations_for_active_model() -> None:
    """After migration, active model audit should not flag legacy bet-grain mid-term owner."""

    snap = load_candidate_registry(None)
    raw_rows = load_registry_raw_feature_dicts(None)
    audit = assert_feature_cadence_contract_or_raise(
        snap,
        snap.model_feature_columns,
        raw_rows=raw_rows,
        fail_on_legacy_mid_term_owner=True,
    )
    assert audit["violation_count"] == 0


def test_short_term_fe_resolve_to_pit_supplier() -> None:
    """Short-term active fe columns default to short_term_pit_builder."""

    snap = load_candidate_registry(None)
    row = next(r for r in snap.rows if r.feature_id == "fe__wager_sum__w15m")
    resolved = resolve_feature_cadence(row)
    assert resolved.allowed_training_supplier == SUPPLIER_SHORT_TERM_PIT


def test_build_feature_cadence_audit_counts_terms() -> None:
    """Audit report includes term and cadence counts for model features."""

    snap = load_candidate_registry(None)
    audit = build_feature_cadence_audit(snap, snap.model_feature_columns)
    assert audit["model_feature_count"] == len(snap.model_feature_columns)
    assert audit["term_counts"]["short_term"] >= 1
    assert audit["term_counts"].get("mid_term", 0) >= 8
