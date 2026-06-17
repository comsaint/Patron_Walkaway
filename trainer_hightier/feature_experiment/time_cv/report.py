"""Cross-fold aggregation and KEEP/DROP decisions for Time-CV."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Literal

from trainer_hightier.config import FeatureSelectionTimeCvConfig

TimeCvDecision = Literal["KEEP", "REVIEW", "MARGINAL", "DROP", "STRONG_DROP"]
FeaturePruningDecision = Literal["KEEP_FEATURE", "REVIEW_FEATURE", "MARGINAL_FEATURE", "DROP_FEATURE", "STRONG_DROP_FEATURE"]


@dataclass(frozen=True)
class TimeCvArmDecision:
    """Cross-fold decision for one ablation arm."""

    arm_id: str
    decision: TimeCvDecision
    mean_delta_p1hr_pp: float | None
    std_delta_p1hr_pp: float | None
    cv_ratio: float | None
    n_folds: int
    fold_deltas_pp: tuple[float, ...]
    early_stopped: bool
    reason_codes: tuple[str, ...]


def _cv_ratio(mean_delta: float, std_delta: float) -> float | None:
    """Return std / |mean|; ``None`` when mean is ~0."""

    if not math.isfinite(mean_delta) or abs(mean_delta) < 1e-9:
        return None
    return float(std_delta) / abs(float(mean_delta))


def aggregate_arm_decision(
    fold_deltas_pp: tuple[float, ...],
    *,
    arm_id: str,
    cfg: FeatureSelectionTimeCvConfig,
    early_stopped: bool = False,
) -> TimeCvArmDecision:
    """Classify one arm from per-fold ΔP@1hr values (arm minus baseline, in pp)."""

    reason_codes: list[str] = []
    if not fold_deltas_pp:
        return TimeCvArmDecision(
            arm_id=arm_id,
            decision="REVIEW",
            mean_delta_p1hr_pp=None,
            std_delta_p1hr_pp=None,
            cv_ratio=None,
            n_folds=0,
            fold_deltas_pp=(),
            early_stopped=early_stopped,
            reason_codes=("no_fold_deltas",),
        )

    if any(not math.isfinite(d) for d in fold_deltas_pp):
        reason_codes.append("non_finite_fold_delta")

    finite = tuple(d for d in fold_deltas_pp if math.isfinite(d))
    if not finite:
        return TimeCvArmDecision(
            arm_id=arm_id,
            decision="REVIEW",
            mean_delta_p1hr_pp=None,
            std_delta_p1hr_pp=None,
            cv_ratio=None,
            n_folds=len(fold_deltas_pp),
            fold_deltas_pp=fold_deltas_pp,
            early_stopped=early_stopped,
            reason_codes=tuple(reason_codes + ["all_fold_deltas_non_finite"]),
        )

    mean_d = float(statistics.mean(finite))
    std_d = float(statistics.stdev(finite)) if len(finite) > 1 else 0.0
    ratio = _cv_ratio(mean_d, std_d)

    if all(d < 0.0 for d in finite):
        reason_codes.append("all_folds_negative_delta")
        return TimeCvArmDecision(
            arm_id=arm_id,
            decision="STRONG_DROP",
            mean_delta_p1hr_pp=mean_d,
            std_delta_p1hr_pp=std_d,
            cv_ratio=ratio,
            n_folds=len(finite),
            fold_deltas_pp=fold_deltas_pp,
            early_stopped=early_stopped,
            reason_codes=tuple(reason_codes),
        )

    if mean_d < float(cfg.drop_threshold_pp):
        reason_codes.append("mean_delta_below_drop_threshold")
        decision: TimeCvDecision = "DROP"
    elif mean_d >= float(cfg.mean_delta_p1hr_pp):
        if ratio is not None and ratio >= float(cfg.max_cv_ratio):
            reason_codes.append("high_cv_ratio")
            decision = "REVIEW"
        else:
            decision = "KEEP"
    elif mean_d >= float(cfg.marginal_low_pp):
        reason_codes.append("marginal_mean_delta")
        decision = "MARGINAL"
    else:
        reason_codes.append("mean_delta_below_drop_threshold")
        decision = "DROP"

    return TimeCvArmDecision(
        arm_id=arm_id,
        decision=decision,
        mean_delta_p1hr_pp=mean_d,
        std_delta_p1hr_pp=std_d,
        cv_ratio=ratio,
        n_folds=len(finite),
        fold_deltas_pp=fold_deltas_pp,
        early_stopped=early_stopped,
        reason_codes=tuple(reason_codes),
    )


def should_early_stop_strong_drop(
    fold_deltas_pp: tuple[float, ...],
    *,
    early_stop_folds: int,
) -> bool:
    """Return True when the first ``early_stop_folds`` deltas are all negative."""

    if len(fold_deltas_pp) < early_stop_folds:
        return False
    head = fold_deltas_pp[:early_stop_folds]
    return all(d < 0.0 for d in head)


def feature_pruning_decision_from_loo(arm_decision: TimeCvArmDecision) -> FeaturePruningDecision:
    """Map LOO arm decision (baseline minus feature) to feature keep/drop semantics.

    LOO ``delta = arm - baseline``: negative delta means removing the feature hurt
    operational precision → keep the feature; positive delta → drop the feature.
    """

    finite = tuple(d for d in arm_decision.fold_deltas_pp if math.isfinite(d))
    if finite and all(d > 0.0 for d in finite):
        return "STRONG_DROP_FEATURE"

    mapping: dict[TimeCvDecision, FeaturePruningDecision] = {
        "KEEP": "DROP_FEATURE",
        "REVIEW": "REVIEW_FEATURE",
        "MARGINAL": "MARGINAL_FEATURE",
        "DROP": "KEEP_FEATURE",
        "STRONG_DROP": "KEEP_FEATURE",
    }
    return mapping[arm_decision.decision]


def arm_decision_to_dict(decision: TimeCvArmDecision) -> dict[str, Any]:
    """Serialize :class:`TimeCvArmDecision` for JSON reports."""

    return {
        "arm_id": decision.arm_id,
        "decision": decision.decision,
        "mean_delta_p1hr_pp": decision.mean_delta_p1hr_pp,
        "std_delta_p1hr_pp": decision.std_delta_p1hr_pp,
        "cv_ratio": decision.cv_ratio,
        "n_folds": decision.n_folds,
        "fold_deltas_pp": list(decision.fold_deltas_pp),
        "early_stopped": decision.early_stopped,
        "reason_codes": list(decision.reason_codes),
    }
