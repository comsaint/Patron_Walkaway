"""Feature-group ablation helpers (add-one vs baseline, leave-one-out vs full candidate)."""

from __future__ import annotations

from typing import Any, Mapping

from trainer_hightier.feature_experiment import feature_registry as _feature_registry
from trainer_hightier.feature_experiment.candidate_registry_loader import candidate_features_for_group
from trainer_hightier.feature_experiment.feature_registry import candidate_registry_snapshot


def experimental_group_ids() -> tuple[str, ...]:
    """Registry ``group_*`` keys that have at least one ablation-selectable ``fe__*`` column."""

    return candidate_registry_snapshot().ablation_experimental_group_ids


def feature_columns_add_one(group_id: str) -> tuple[str, ...]:
    """Baseline columns plus ablation-selectable ``fe__*`` columns for a single ``group_id``."""

    snap = candidate_registry_snapshot()
    extra = candidate_features_for_group(snap, group_id, slot="ablation")
    model_cols = tuple(_feature_registry.MODEL_FEATURE_COLUMNS)
    return tuple(dict.fromkeys(model_cols + tuple(extra)))


def feature_columns_leave_one_out_minus(group_id: str) -> tuple[str, ...]:
    """Full candidate columns minus candidate-selectable ``fe__*`` in one group."""

    snap = candidate_registry_snapshot()
    drop = frozenset(candidate_features_for_group(snap, group_id, slot="candidate"))
    full_cols = tuple(_feature_registry.FULL_CANDIDATE_FEATURE_COLUMNS)
    return tuple(c for c in full_cols if c not in drop)


def compute_gate1_vs_baseline(
    baseline_report: Mapping[str, Any],
    arm_report: Mapping[str, Any],
    *,
    capacity_alerts_per_hour_cap: float,
    arm_side_key_prefix: str = "candidate",
) -> dict[str, Any]:
    """Gate 1 block: arm vs baseline (WORKING_PLAN §1.4 / §4.2).

    ``arm_side_key_prefix`` names metrics from ``arm_report`` (default ``candidate``
    for the main baseline/full report; use ``arm`` in per-group ablation entries).
    """

    ap_k = f"{arm_side_key_prefix}_val_pick_feasible"
    ah_k = f"{arm_side_key_prefix}_val_alerts_per_hour"
    b_ap = float(baseline_report.get("val_ap", 0.0))
    c_ap = float(arm_report.get("val_ap", 0.0))
    b_rec = float(baseline_report.get("val_recall", 0.0))
    c_rec = float(arm_report.get("val_recall", 0.0))
    d_ap = c_ap - b_ap
    d_rec = c_rec - b_rec
    b_feas = bool(baseline_report.get("step5_val_pick_feasible", False))
    c_feas = bool(arm_report.get("step5_val_pick_feasible", False))
    vaph_raw = arm_report.get("val_alerts_per_hour")
    vaph: float | None = float(vaph_raw) if vaph_raw is not None else None
    cap = float(capacity_alerts_per_hour_cap)
    capacity_alarm = vaph is not None and vaph > cap
    cap_ok = vaph is not None and vaph <= cap
    gate1_pass = b_feas and c_feas and d_ap >= 0.003 and d_rec > 0.0 and cap_ok
    reason_codes: list[str] = []
    if not b_feas:
        reason_codes.append("baseline_val_pick_infeasible")
    if not c_feas:
        reason_codes.append(f"{arm_side_key_prefix}_val_pick_infeasible")
    if d_ap < 0.003:
        reason_codes.append("delta_ap_below_min")
    if d_rec <= 0.0:
        reason_codes.append("delta_recall_not_strictly_positive")
    if vaph is None:
        reason_codes.append(f"{arm_side_key_prefix}_val_alerts_per_hour_missing")
    elif not cap_ok:
        reason_codes.append(f"{arm_side_key_prefix}_val_alerts_per_hour_over_cap")
    return {
        "delta_val_ap": d_ap,
        "delta_val_recall_at_pmin_pick": d_rec,
        "baseline_val_pick_feasible": b_feas,
        ap_k: c_feas,
        ah_k: vaph,
        "capacity_alerts_per_hour_cap": cap,
        "capacity_alarm": bool(capacity_alarm),
        "pass_v0_thresholds": bool(gate1_pass),
        "reason_codes_if_fail": reason_codes if not gate1_pass else [],
        "thresholds_v0": {
            "delta_ap_min": 0.003,
            "delta_recall_strictly_positive": True,
            "capacity_alerts_per_hour_cap": cap,
        },
    }


def delta_full_minus_loo(full_report: Mapping[str, Any], loo_report: Mapping[str, Any]) -> dict[str, float]:
    """Positive deltas ⇒ full candidate beats leaving the group out."""

    f_ap = float(full_report.get("val_ap", 0.0))
    l_ap = float(loo_report.get("val_ap", 0.0))
    f_rec = float(full_report.get("val_recall", 0.0))
    l_rec = float(loo_report.get("val_recall", 0.0))
    return {
        "delta_val_ap_full_minus_loo": f_ap - l_ap,
        "delta_val_recall_full_minus_loo": f_rec - l_rec,
    }


def synthesize_group_decision_v0(
    *,
    group_id: str,
    add_one_gate_pass: bool,
    delta_full_minus_loo_ap: float | None,
    delta_full_minus_loo_rec: float | None,
) -> tuple[str, str]:
    """Return ``(decision, reason_code)`` for one experimental group."""

    _ = group_id
    if add_one_gate_pass:
        return "KEEP", "add_one_passes_gate1_vs_baseline"
    if delta_full_minus_loo_ap is None or delta_full_minus_loo_rec is None:
        return "DROP", "add_one_failed_insufficient_loo_evidence"
    synergy = delta_full_minus_loo_ap >= 0.003 and delta_full_minus_loo_rec > 0.0
    harmless_remove = delta_full_minus_loo_ap <= 0.0 and delta_full_minus_loo_rec <= 0.0
    if synergy:
        return "KEEP", "synergy_full_beats_leave_one_out"
    if harmless_remove:
        return "DROP", "removal_from_full_not_harmful_add_one_failed"
    return "REVIEW", "mixed_leave_one_out_signals"
