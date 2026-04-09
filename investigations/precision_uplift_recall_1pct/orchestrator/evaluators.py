"""Gate evaluation (PASS / PRELIMINARY / FAIL) — MVP T5."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

DEFAULT_PAT_ABS_TOL = 0.15


def _parse_iso_ts(raw: str) -> datetime:
    """Parse ISO-8601 timestamp string (supports trailing ``Z``)."""
    ts = raw.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def window_duration_hours(window: Mapping[str, Any]) -> float:
    """Return observation window length in hours (end − start)."""
    start_raw = str(window.get("start_ts", ""))
    end_raw = str(window.get("end_ts", ""))
    if not start_raw or not end_raw:
        return 0.0
    delta = _parse_iso_ts(end_raw) - _parse_iso_ts(start_raw)
    return max(0.0, delta.total_seconds() / 3600.0)


def extract_precision_at_target_recall(r1_payload: Mapping[str, Any] | None) -> float | None:
    """Read ``precision_at_target_recall`` from R1/R6 all-mode JSON."""
    if not isinstance(r1_payload, Mapping):
        return None
    uni = r1_payload.get("unified_sample_evaluation")
    if isinstance(uni, Mapping):
        block = uni.get("precision_at_recall_target")
        if isinstance(block, Mapping):
            v = block.get("precision_at_target_recall")
            if v is not None:
                return float(v)
    ev = r1_payload.get("evaluate")
    if isinstance(ev, Mapping):
        block = ev.get("precision_at_recall_target")
        if isinstance(block, Mapping):
            v = block.get("precision_at_target_recall")
            if v is not None:
                return float(v)
    return None


def _r2_mismatch_reason(r2: object) -> str | None:
    """Return a blocking code if R2 cross-check shows large PL vs alerts gap."""
    if not isinstance(r2, Mapping):
        return None
    if str(r2.get("status")) != "ok":
        return None
    diff = r2.get("difference_pl_minus_alerts")
    n_pl = r2.get("n_prediction_log_is_alert_rows")
    if not isinstance(diff, (int, float)):
        return None
    diff_f = float(diff)
    if isinstance(n_pl, int) and n_pl >= 0:
        if abs(diff_f) > max(50.0, 0.25 * float(n_pl)):
            return "r2_prediction_log_vs_alerts_mismatch"
    elif abs(diff_f) > 50.0:
        return "r2_prediction_log_vs_alerts_mismatch"
    return None


def _threshold_int(t: Mapping[str, Any], key: str, default: int) -> int:
    """Parse non-negative int threshold from config mapping."""
    v = t.get(key, default)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def _threshold_float(t: Mapping[str, Any], key: str, default: float) -> float:
    """Parse positive float threshold from config mapping."""
    v = t.get(key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def evaluate_phase1_gate(bundle: dict[str, Any]) -> dict[str, Any]:
    """Compute gate status, blocking reasons, and evidence summary.

    Rules (deterministic given ``bundle``):

    1. Any collector ``errors`` → **FAIL**.
    2. Missing ``r1_r6_final.payload`` → **FAIL**.
    3. R2 PL vs alerts gap beyond heuristic → **FAIL**.
    4. Observation hours < ``min_hours_preliminary`` → **PRELIMINARY**.
    5. Finalized alerts / TP below preliminary mins → **PRELIMINARY**.
    6. Gate-grade hours + sample counts met:
       - If mid + final PAT both present and |Δ| > tolerance → **FAIL**.
       - If mid snapshot missing (no PAT from mid) → **PRELIMINARY** (direction check
         inconclusive per runbook).
       - Else → **PASS**.
    7. Otherwise (preliminary window satisfied but not gate) → **PRELIMINARY**.

    Args:
        bundle: Output of ``collectors.collect_phase1_artifacts``.

    Returns:
        Dict with ``status``, ``blocking_reasons``, ``evidence_summary``, and
        compact ``metrics`` for reports.
    """
    reasons: list[str] = []
    window = bundle.get("window") if isinstance(bundle.get("window"), Mapping) else {}
    thr = bundle.get("thresholds") if isinstance(bundle.get("thresholds"), Mapping) else {}

    min_h_pre = _threshold_float(thr, "min_hours_preliminary", 48.0)
    min_h_gate = _threshold_float(thr, "min_hours_gate", 72.0)
    pre_fin = _threshold_int(thr, "min_finalized_alerts_preliminary", 300)
    pre_tp = _threshold_int(thr, "min_finalized_true_positives_preliminary", 30)
    gate_fin = _threshold_int(thr, "min_finalized_alerts_gate", 800)
    gate_tp = _threshold_int(thr, "min_finalized_true_positives_gate", 50)
    pat_tol = _threshold_float(thr, "gate_pat_abs_tolerance", DEFAULT_PAT_ABS_TOL)

    hours = window_duration_hours(window)

    stats = bundle.get("state_db_stats") if isinstance(bundle.get("state_db_stats"), Mapping) else {}
    n_fin = stats.get("finalized_alerts_count")
    n_tp = stats.get("finalized_true_positives_count")
    n_fin_i = int(n_fin) if isinstance(n_fin, (int, float)) else 0
    n_tp_i = int(n_tp) if isinstance(n_tp, (int, float)) else 0

    r1_block = bundle.get("r1_r6_final") if isinstance(bundle.get("r1_r6_final"), Mapping) else {}
    r1_final = r1_block.get("payload") if isinstance(r1_block.get("payload"), Mapping) else None

    pat_final = extract_precision_at_target_recall(r1_final)
    r1_mid_block = bundle.get("r1_r6_mid") if isinstance(bundle.get("r1_r6_mid"), Mapping) else {}
    r1_mid = r1_mid_block.get("payload") if isinstance(r1_mid_block.get("payload"), Mapping) else None
    pat_mid = extract_precision_at_target_recall(r1_mid)

    metrics: dict[str, Any] = {
        "window_hours": round(hours, 4),
        "finalized_alerts_count": n_fin_i,
        "finalized_true_positives_count": n_tp_i,
        "precision_at_target_recall_final": pat_final,
        "precision_at_target_recall_mid": pat_mid,
        "has_backtest_metrics": bundle.get("backtest_metrics") is not None,
    }

    def _evidence() -> str:
        parts = [
            f"window_h={hours:.2f}",
            f"finalized_alerts={n_fin_i}",
            f"finalized_tp={n_tp_i}",
        ]
        if pat_final is not None:
            parts.append(f"pat@r_final={pat_final:.4f}")
        if pat_mid is not None:
            parts.append(f"pat@r_mid={pat_mid:.4f}")
        return "; ".join(parts)

    err_list = bundle.get("errors") or []
    if err_list:
        for e in err_list:
            if isinstance(e, Mapping):
                code = str(e.get("code", "unknown"))
                reasons.append(f"collect_error:{code}")
            else:
                reasons.append("collect_error:invalid_entry")
        return {
            "status": "FAIL",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    if r1_final is None:
        reasons.append("missing_r1_r6_final_payload")
        return {
            "status": "FAIL",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    r2 = r1_final.get("r2_prediction_log_vs_alerts")
    r2_reason = _r2_mismatch_reason(r2)
    if r2_reason:
        reasons.append(r2_reason)
        return {
            "status": "FAIL",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    if hours < min_h_pre:
        reasons.append("observation_hours_below_preliminary_minimum")
        return {
            "status": "PRELIMINARY",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    if n_fin_i < pre_fin or n_tp_i < pre_tp:
        reasons.append("samples_below_preliminary_minimum")
        return {
            "status": "PRELIMINARY",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    gate_hours_ok = hours >= min_h_gate
    gate_sample_ok = n_fin_i >= gate_fin and n_tp_i >= gate_tp

    if not (gate_hours_ok and gate_sample_ok):
        reasons.append("below_gate_time_or_sample_thresholds")
        return {
            "status": "PRELIMINARY",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    if pat_final is None:
        reasons.append("missing_precision_at_target_recall_in_r1_payload")
        return {
            "status": "FAIL",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    if pat_mid is None:
        reasons.append("missing_mid_r1_snapshot_for_direction_check")
        return {
            "status": "PRELIMINARY",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    if abs(pat_final - pat_mid) > pat_tol:
        reasons.append("r1_mid_final_precision_at_target_recall_divergence")
        return {
            "status": "FAIL",
            "blocking_reasons": reasons,
            "evidence_summary": _evidence(),
            "metrics": metrics,
        }

    return {
        "status": "PASS",
        "blocking_reasons": [],
        "evidence_summary": _evidence(),
        "metrics": metrics,
    }
