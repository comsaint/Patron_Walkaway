"""Fold-level operational metrics for Time-CV feature selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from trainer_hightier.evaluation.alert_band_objective import alert_band_metrics_block


def val_p1hr_precision_from_report(report: Mapping[str, Any]) -> float | None:
    """Extract validation precision at 1.0 alerts/hour from a Step 5 report."""

    band = report.get("step5_val_alert_band")
    if isinstance(band, Mapping):
        for pt in band.get("points") or ():
            if not isinstance(pt, Mapping):
                continue
            rate = float(pt.get("target_alerts_per_hour", -1.0))
            if math.isclose(rate, 1.0):
                prec = pt.get("precision")
                return float(prec) if prec is not None else None

    flat_key = "val_op_precision_at_1p0_alerts_per_hour"
    if flat_key in report:
        return float(report[flat_key])

    pick = report.get("step5_val_precision_at_pick")
    deploy = report.get("step5_deployment_target_alerts_per_hour")
    if pick is not None and deploy is not None and math.isclose(float(deploy), 1.0):
        return float(pick)
    return None


def precision_to_pp(precision: float | None) -> float | None:
    """Convert a probability precision to percentage points."""

    if precision is None or not math.isfinite(float(precision)):
        return None
    return float(precision) * 100.0


@dataclass(frozen=True)
class FoldOperationalMetrics:
    """Operational metrics for one fold and one model arm."""

    fold_idx: int
    arm_id: str
    val_p1hr_precision: float | None
    val_p1hr_precision_pp: float | None
    val_ap: float | None
    val_recall_at_pick: float | None


def fold_metrics_from_report(
    report: Mapping[str, Any],
    *,
    fold_idx: int,
    arm_id: str,
) -> FoldOperationalMetrics:
    """Build fold metrics from one Step 5 ``report`` dict."""

    p1hr = val_p1hr_precision_from_report(report)
    val_ap_raw = report.get("val_ap")
    val_rec_raw = report.get("step5_val_recall_at_pick")
    return FoldOperationalMetrics(
        fold_idx=fold_idx,
        arm_id=arm_id,
        val_p1hr_precision=p1hr,
        val_p1hr_precision_pp=precision_to_pp(p1hr),
        val_ap=float(val_ap_raw) if val_ap_raw is not None else None,
        val_recall_at_pick=float(val_rec_raw) if val_rec_raw is not None else None,
    )


def delta_p1hr_pp(
    baseline_report: Mapping[str, Any],
    arm_report: Mapping[str, Any],
) -> float | None:
    """Return arm-minus-baseline ΔP@1hr in percentage points."""

    base_pp = precision_to_pp(val_p1hr_precision_from_report(baseline_report))
    arm_pp = precision_to_pp(val_p1hr_precision_from_report(arm_report))
    if base_pp is None or arm_pp is None:
        return None
    return arm_pp - base_pp


def alert_band_block_from_candidates(
    candidates,
    *,
    window_hours: float | None,
    split_prefix: str = "val",
) -> dict[str, Any]:
    """Thin wrapper for direct candidate scoring outside Step 5."""

    return alert_band_metrics_block(
        split_prefix,
        candidates,
        window_hours=window_hours,
        target_alerts_per_hour=(1.0, 2.0),
    )
