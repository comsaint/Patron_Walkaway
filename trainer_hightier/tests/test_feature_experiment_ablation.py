"""Unit tests for :mod:`trainer_hightier.feature_experiment.ablation`."""

from __future__ import annotations

from trainer_hightier.feature_experiment.ablation import (
    compute_gate1_vs_baseline,
    experimental_group_ids,
    feature_columns_add_one,
    feature_columns_leave_one_out_minus,
    synthesize_group_decision_v0,
)


def test_experimental_group_ids_only_group_prefix() -> None:
    """Registry keys must be ``group_*`` experimental arms."""

    g = experimental_group_ids()
    assert all(x.startswith("group_") for x in g)
    assert "existing_trial_1h" not in g


def test_feature_columns_add_one_extends_baseline() -> None:
    """Add-one columns include baseline plus one group's ``fe__*`` fields."""

    gid = "group_d_personal_z"
    cols = feature_columns_add_one(gid)
    assert "wager" in cols
    assert "fe__wager_z_prior_w30d" in cols


def test_leave_one_out_drops_group_columns() -> None:
    """LOO removes exactly one group's experimental columns."""

    gid = "group_d_personal_z"
    cols = feature_columns_leave_one_out_minus(gid)
    assert "fe__wager_z_prior_w30d" not in cols
    assert "fe__wager_sum__w15m" in cols


def test_gate1_pass_strict_recall_and_capacity() -> None:
    """Gate requires ΔRecall>0, ΔAP≥0.003, feasibility, alerts cap."""

    base = {"val_ap": 0.4, "val_recall": 0.01, "step5_val_pick_feasible": True}
    arm_weak_ap = {
        "val_ap": 0.401,
        "val_recall": 0.02,
        "step5_val_pick_feasible": True,
        "val_alerts_per_hour": 10.0,
    }
    assert not compute_gate1_vs_baseline(base, arm_weak_ap, capacity_alerts_per_hour_cap=120.0)["pass_v0_thresholds"]
    arm_weak_rec = {
        "val_ap": 0.41,
        "val_recall": 0.01,
        "step5_val_pick_feasible": True,
        "val_alerts_per_hour": 10.0,
    }
    assert not compute_gate1_vs_baseline(base, arm_weak_rec, capacity_alerts_per_hour_cap=120.0)["pass_v0_thresholds"]
    arm_good = {
        "val_ap": 0.405,
        "val_recall": 0.02,
        "step5_val_pick_feasible": True,
        "val_alerts_per_hour": 10.0,
    }
    assert compute_gate1_vs_baseline(base, arm_good, capacity_alerts_per_hour_cap=120.0)["pass_v0_thresholds"]


def test_synthesize_decision_keep_on_add_one() -> None:
    """Add-one pass short-circuits to KEEP."""

    d, r = synthesize_group_decision_v0(
        group_id="group_a_velocity_ratios",
        add_one_gate_pass=True,
        delta_full_minus_loo_ap=-1.0,
        delta_full_minus_loo_rec=-1.0,
    )
    assert d == "KEEP" and "add_one" in r


def test_synthesize_decision_drop_when_harmless_loo() -> None:
    """Failed add-one and non-positive LOO deltas → DROP."""

    d, r = synthesize_group_decision_v0(
        group_id="group_c_burstiness",
        add_one_gate_pass=False,
        delta_full_minus_loo_ap=0.0,
        delta_full_minus_loo_rec=0.0,
    )
    assert d == "DROP"
