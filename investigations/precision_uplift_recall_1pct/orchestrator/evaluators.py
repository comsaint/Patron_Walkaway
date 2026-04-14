"""Gate evaluation (PASS / PRELIMINARY / FAIL) — MVP T5."""

from __future__ import annotations

import statistics
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
    mid_rows_obj = (
        bundle.get("r1_r6_mid_snapshots")
        if isinstance(bundle.get("r1_r6_mid_snapshots"), list)
        else []
    )
    mid_pats: list[float] = []
    for row in mid_rows_obj:
        if not isinstance(row, Mapping):
            continue
        mp = row.get("payload")
        if not isinstance(mp, Mapping):
            continue
        v = extract_precision_at_target_recall(mp)
        if v is not None:
            mid_pats.append(float(v))

    metrics: dict[str, Any] = {
        "window_hours": round(hours, 4),
        "finalized_alerts_count": n_fin_i,
        "finalized_true_positives_count": n_tp_i,
        "precision_at_target_recall_final": pat_final,
        "precision_at_target_recall_mid": pat_mid,
        "precision_at_target_recall_mid_snapshots": mid_pats or None,
        "mid_snapshot_count": len(mid_rows_obj),
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
        if mid_pats:
            parts.append(f"pat@r_mid_n={len(mid_pats)}")
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

    if len(mid_pats) >= 2:
        if max(mid_pats) - min(mid_pats) > pat_tol:
            reasons.append("r1_multi_mid_precision_at_target_recall_divergence")
            return {
                "status": "FAIL",
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


PHASE2_BACKTEST_PR1_KEY = "test_precision_at_recall_0.01"


def _job_training_harvest_counts(bundle: Mapping[str, Any]) -> tuple[int, int]:
    """Return (row_count, found_count) from ``bundle['job_training_harvest']``."""
    jh = bundle.get("job_training_harvest")
    if not isinstance(jh, Mapping):
        return 0, 0
    rows = jh.get("rows")
    if not isinstance(rows, list):
        return 0, 0
    n = len(rows)
    n_found = sum(1 for r in rows if isinstance(r, Mapping) and r.get("found"))
    return n, n_found


def phase2_per_job_backtest_metrics(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize ``bundle['per_job_backtest_jobs']`` for gate metrics (T11).

    Returns:
        Empty dict when the key is missing or not a mapping; otherwise includes
        execution flags and a normalized list of per-job preview rows.
    """
    pjb = bundle.get("per_job_backtest_jobs")
    if not isinstance(pjb, Mapping):
        return {}
    out: dict[str, Any] = {
        "per_job_backtest_executed": pjb.get("executed"),
        "per_job_backtest_all_ok": pjb.get("all_ok"),
    }
    res = pjb.get("results")
    previews: list[dict[str, Any]] = []
    if isinstance(res, list):
        for r in res:
            if not isinstance(r, Mapping):
                continue
            tr = str(r.get("track") or "").strip()
            eid = str(r.get("exp_id") or "").strip()
            if not tr or not eid:
                continue
            raw_pv = r.get("shared_precision_at_recall_1pct_preview")
            pv: float | None
            if raw_pv is None:
                pv = None
            else:
                try:
                    pv = float(raw_pv)
                except (TypeError, ValueError):
                    pv = None
            previews.append(
                {
                    "track": tr,
                    "exp_id": eid,
                    "skipped": bool(r.get("skipped")),
                    "ok": r.get("ok"),
                    "shared_precision_at_recall_1pct_preview": pv,
                }
            )
    out["per_job_backtest_previews"] = previews
    out["per_job_backtest_preview_count"] = sum(
        1 for p in previews if p.get("shared_precision_at_recall_1pct_preview") is not None
    )
    return out


def phase2_pat_series_source_counts(bundle: Mapping[str, Any]) -> dict[str, int]:
    """Count PAT series source tags from ``phase2_pat_series_source_by_experiment``.

    Returns:
        Mapping from source tag to count (possibly empty when source map is absent).
    """
    root = bundle.get("phase2_pat_series_source_by_experiment")
    if not isinstance(root, Mapping):
        return {}
    out: dict[str, int] = {}
    for tkey, exp_map in root.items():
        tr = str(tkey).strip()
        if not tr.startswith("track_") or not isinstance(exp_map, Mapping):
            continue
        for row in exp_map.values():
            if not isinstance(row, Mapping):
                continue
            src = str(row.get("source") or "").strip()
            if not src:
                continue
            out[src] = out.get(src, 0) + 1
    return out


def _phase2_per_job_backtest_evidence_suffix(bundle: Mapping[str, Any]) -> str:
    """Short human-readable fragment for gate ``evidence_summary``."""
    pjb = bundle.get("per_job_backtest_jobs")
    if not isinstance(pjb, Mapping) or pjb.get("executed") is not True:
        return ""
    pj = phase2_per_job_backtest_metrics(bundle)
    prev_rows = pj.get("per_job_backtest_previews")
    if not isinstance(prev_rows, list):
        return ""
    with_pv = [
        x
        for x in prev_rows
        if isinstance(x, Mapping)
        and x.get("shared_precision_at_recall_1pct_preview") is not None
    ]
    if with_pv:
        bits = [
            f"{x['track']}/{x['exp_id']}="
            f"{float(x['shared_precision_at_recall_1pct_preview']):.4f}"
            for x in with_pv
        ]
        return "; per-job PAT@1% preview(s): " + ", ".join(bits)
    return "; per-job backtests executed; no numeric PAT@1% previews parsed"


def _parse_float_gate(
    gate: Mapping[str, Any], key: str, default: float
) -> float:
    """Read a float from ``gate[key]`` with fallback when missing or invalid."""
    raw = gate.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _parse_min_pat_windows_required(gate: Mapping[str, Any]) -> int:
    """Minimum PAT@1% series length required for uplift **PASS** (T11A dual-window gate).

    Reads ``gate.min_pat_windows_for_pass`` (default **2**). Values ``<= 0`` disable the check.
    """
    raw = gate.get("min_pat_windows_for_pass", 2)
    if raw is None:
        return 2
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return 2
    return v


def _phase2_uplift_winner_metrics(
    uplift_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pick best experiment that ``meets_min_uplift`` (max pp, then track_a/b/c, then YAML order)."""
    rank = {"track_a": 0, "track_b": 1, "track_c": 2}
    best_key: tuple[float, int, int] | None = None
    best_row: dict[str, Any] | None = None
    for i, r in enumerate(uplift_rows):
        if not isinstance(r, Mapping) or r.get("meets_min_uplift") is not True:
            continue
        try:
            u = float(r["uplift_pp_vs_baseline"])
        except (KeyError, TypeError, ValueError):
            continue
        tr = str(r.get("track") or "").strip()
        key = (-u, rank.get(tr, 9), i)
        if best_key is None or key < best_key:
            best_key = key
            best_row = dict(r)
    if best_row is None:
        return {}
    be = str(best_row.get("baseline_exp_id") or "").strip()
    eid = str(best_row.get("exp_id") or "").strip()
    trw = str(best_row.get("track") or "").strip()
    out: dict[str, Any] = {
        "phase2_winner_track": trw,
        "phase2_winner_exp_id": eid,
        "phase2_winner_baseline_exp_id": be or None,
        "phase2_winner_uplift_pp_vs_baseline": float(best_row["uplift_pp_vs_baseline"]),
    }
    try:
        out["phase2_winner_preview_pat"] = float(best_row["preview"])
    except (KeyError, TypeError, ValueError):
        out["phase2_winner_preview_pat"] = None
    return out


def _phase2_elimination_rows_for_uplift(
    uplift_rows: list[dict[str, Any]],
    *,
    min_uplift_pp: float,
    winner_track: str | None,
    winner_exp_id: str | None,
) -> list[dict[str, Any]]:
    """Non-winner challengers with auditable reasons (T11 report narrative).

    Every row in ``uplift_rows`` that carries ``uplift_pp_vs_baseline`` (challenger vs
    track baseline) is listed except the global winner (when ``winner_*`` are set).
    """
    out: list[dict[str, Any]] = []
    wtr = str(winner_track or "").strip() or None
    we = str(winner_exp_id or "").strip() or None
    for r in uplift_rows:
        if not isinstance(r, Mapping) or "uplift_pp_vs_baseline" not in r:
            continue
        tr = str(r.get("track") or "").strip()
        eid = str(r.get("exp_id") or "").strip()
        if wtr and we and tr == wtr and eid == we:
            continue
        be = str(r.get("baseline_exp_id") or "").strip()
        try:
            upp = float(r["uplift_pp_vs_baseline"])
        except (TypeError, ValueError):
            continue
        meets = r.get("meets_min_uplift") is True
        if meets:
            rc = "meets_min_uplift_but_not_global_winner"
            detail = (
                f"uplift {upp:.4f} pp >= {min_uplift_pp:.2f} pp vs baseline `{be}`; "
                f"global winner `{wtr}/{we}`"
            )
        else:
            rc = "below_min_uplift_pp_vs_baseline"
            detail = (
                f"uplift {upp:.4f} pp < {min_uplift_pp:.2f} pp vs baseline `{be}`"
            )
        out.append(
            {
                "track": tr,
                "exp_id": eid,
                "reason_code": rc,
                "baseline_exp_id": be or None,
                "uplift_pp_vs_baseline": upp,
                "gate_min_uplift_pp_vs_baseline": min_uplift_pp,
                "detail": detail,
            }
        )
    return out


def _phase2_apply_min_pat_windows_gate_for_pass(
    bundle: Mapping[str, Any],
    uplift_out: dict[str, Any],
    gate_cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """After uplift (+ std), require max PAT series length >= N for **PASS** (default N=2)."""
    if uplift_out.get("decision") != "PASS":
        return uplift_out
    req = _parse_min_pat_windows_required(gate_cfg)
    if req <= 0:
        return uplift_out
    n = _phase2_max_pat_series_window_count(bundle)
    metrics = dict(uplift_out["metrics"])
    metrics["phase2_pat_windows_max"] = n
    metrics["phase2_pat_windows_required"] = req
    base_ev = str(uplift_out.get("evidence_extra") or "")
    if n >= req:
        uplift_out = dict(uplift_out)
        uplift_out["metrics"] = metrics
        uplift_out["evidence_extra"] = (
            base_ev
            + f"; dual-window gate: max PAT@1% series len={n} (required >={req}) — OK"
        )
        return uplift_out
    uplift_out = dict(uplift_out)
    uplift_out["metrics"] = metrics
    uplift_out["decision"] = "BLOCKED"
    uplift_out["reasons"] = ["phase2_insufficient_pat_windows_for_pass"]
    uplift_out["evidence_extra"] = (
        base_ev
        + f"; dual-window gate: BLOCKED — max PAT@1% series length is {n}, "
        f"requires >={req} (`phase2_pat_series_by_experiment`)"
    )
    return uplift_out


def _phase2_preview_map_from_bundle(bundle: Mapping[str, Any]) -> dict[tuple[str, str], float]:
    """Map (track, exp_id) -> PAT@1% preview for successful non-skipped per-job rows."""
    pj = phase2_per_job_backtest_metrics(bundle)
    rows = pj.get("per_job_backtest_previews")
    if not isinstance(rows, list):
        return {}
    out: dict[tuple[str, str], float] = {}
    for r in rows:
        if not isinstance(r, Mapping) or r.get("skipped"):
            continue
        if r.get("ok") is not True:
            continue
        pv = r.get("shared_precision_at_recall_1pct_preview")
        if pv is None:
            continue
        tr = str(r.get("track") or "").strip()
        eid = str(r.get("exp_id") or "").strip()
        if tr and eid:
            out[(tr, eid)] = float(pv)
    return out


def _phase2_try_uplift_gate_from_per_job(
    bundle: Mapping[str, Any],
) -> dict[str, Any] | None:
    """If per-job backtests ran, evaluate uplift vs first YAML experiment per enabled track.

    Baseline per track is the first experiment (YAML order) that has a numeric preview.
    Each later experiment with a preview is compared; uplift is ``(pv - baseline) * 100``
    percentage points. Uses ``gate.min_uplift_pp_vs_baseline`` (default 3.0).

    Multi-window stability uses optional ``bundle["phase2_pat_series_by_experiment"]``
    (see ``_phase2_apply_std_gate``); until present, ``phase2_std_gate_evaluated`` is
    False.

    Returns:
        ``None`` when ``per_job_backtest_jobs.executed`` is not True; otherwise a dict
        with ``decision`` (``PASS`` / ``FAIL`` / ``BLOCKED``), ``reasons``, ``metrics``,
        ``evidence_extra``.
    """
    pjb = bundle.get("per_job_backtest_jobs")
    if not isinstance(pjb, Mapping) or pjb.get("executed") is not True:
        return None

    gate_cfg = bundle.get("gate") if isinstance(bundle.get("gate"), Mapping) else {}
    min_uplift = _parse_float_gate(gate_cfg, "min_uplift_pp_vs_baseline", 3.0)

    prev_map = _phase2_preview_map_from_bundle(bundle)
    tracks = bundle.get("tracks")
    if not isinstance(tracks, Mapping):
        tracks = {}
    baseline_by_track_raw = gate_cfg.get("baseline_exp_id_by_track")
    baseline_by_track: Mapping[str, Any] = (
        baseline_by_track_raw if isinstance(baseline_by_track_raw, Mapping) else {}
    )

    eligible = False
    any_meets = False
    uplift_rows: list[dict[str, Any]] = []
    baseline_cfg_errors: list[str] = []
    baseline_preview_missing: list[str] = []

    for tname in ("track_a", "track_b", "track_c"):
        tnode = tracks.get(tname)
        if not isinstance(tnode, Mapping) or not tnode.get("enabled"):
            continue
        exps = tnode.get("experiments")
        if not isinstance(exps, list):
            continue
        exp_ids = [
            str(exp.get("exp_id") or "").strip()
            for exp in exps
            if isinstance(exp, Mapping)
        ]
        baseline_pref_raw = baseline_by_track.get(tname)
        baseline_pref = str(baseline_pref_raw or "").strip()
        if baseline_pref and baseline_pref not in exp_ids:
            baseline_cfg_errors.append(
                f"{tname}:{baseline_pref}(not_in_enabled_track_experiments)"
            )
            continue
        baseline_eid: str | None = None
        base_pv: float | None = None
        if baseline_pref:
            pv_pref = prev_map.get((tname, baseline_pref))
            if pv_pref is None:
                baseline_preview_missing.append(f"{tname}:{baseline_pref}")
                continue
            baseline_eid, base_pv = baseline_pref, pv_pref
            uplift_rows.append(
                {
                    "track": tname,
                    "exp_id": baseline_eid,
                    "preview": base_pv,
                    "role": "baseline",
                    "baseline_source": "gate.baseline_exp_id_by_track",
                }
            )
        for exp in exps:
            if not isinstance(exp, Mapping):
                continue
            eid = str(exp.get("exp_id") or "").strip()
            if not eid:
                continue
            pv = prev_map.get((tname, eid))
            if pv is None:
                continue
            if base_pv is None:
                baseline_eid, base_pv = eid, pv
                uplift_rows.append(
                    {
                        "track": tname,
                        "exp_id": eid,
                        "preview": base_pv,
                        "role": "baseline",
                        "baseline_source": "first_preview_in_yaml_order",
                    }
                )
            else:
                if baseline_eid is not None and eid == baseline_eid:
                    continue
                eligible = True
                uplift_pp = (pv - base_pv) * 100.0
                meets = uplift_pp >= min_uplift
                if meets:
                    any_meets = True
                uplift_rows.append(
                    {
                        "track": tname,
                        "exp_id": eid,
                        "preview": pv,
                        "baseline_exp_id": baseline_eid,
                        "uplift_pp_vs_baseline": uplift_pp,
                        "meets_min_uplift": meets,
                    }
                )

    win_m: dict[str, Any] = (
        _phase2_uplift_winner_metrics(uplift_rows) if any_meets else {}
    )
    wt = (
        str(win_m.get("phase2_winner_track") or "").strip() or None
        if any_meets
        else None
    )
    weid = (
        str(win_m.get("phase2_winner_exp_id") or "").strip() or None
        if any_meets
        else None
    )
    elim = _phase2_elimination_rows_for_uplift(
        uplift_rows,
        min_uplift_pp=min_uplift,
        winner_track=wt,
        winner_exp_id=weid,
    )
    metrics_u: dict[str, Any] = {
        "phase2_uplift_gate_evaluated": True,
        "phase2_uplift_min_pp_vs_baseline": min_uplift,
        "phase2_uplift_rows": uplift_rows,
        "phase2_elimination_rows": elim,
        "phase2_std_gate_evaluated": False,
        "phase2_std_gate_note": (
            "max_std_pp_across_windows not applied (no multi-window series in bundle)"
        ),
    }
    if baseline_cfg_errors:
        metrics_u["phase2_uplift_baseline_config_errors"] = baseline_cfg_errors
    if baseline_preview_missing:
        metrics_u["phase2_uplift_baseline_preview_missing"] = baseline_preview_missing

    if baseline_cfg_errors:
        return {
            "decision": "FAIL",
            "reasons": ["phase2_uplift_baseline_config_invalid"],
            "metrics": metrics_u,
            "evidence_extra": (
                "; uplift gate: FAIL — baseline_exp_id_by_track references unknown "
                f"experiment(s): {', '.join(baseline_cfg_errors)}"
            ),
        }
    if baseline_preview_missing:
        return {
            "decision": "BLOCKED",
            "reasons": ["phase2_uplift_baseline_preview_missing"],
            "metrics": metrics_u,
            "evidence_extra": (
                "; uplift gate: BLOCKED — configured baseline has no successful per-job "
                f"PAT@1% preview: {', '.join(baseline_preview_missing)}"
            ),
        }

    if not eligible:
        return {
            "decision": "BLOCKED",
            "reasons": ["phase2_uplift_insufficient_comparisons"],
            "metrics": metrics_u,
            "evidence_extra": (
                "; uplift gate: BLOCKED — no enabled track has two experiments "
                "with PAT@1% previews (baseline + challenger)"
            ),
        }

    if any_meets:
        metrics_u["phase2_uplift_pass"] = True
        metrics_u.update(win_m)
        w_tr = metrics_u.get("phase2_winner_track")
        w_e = metrics_u.get("phase2_winner_exp_id")
        win_ev = (
            f"; uplift winner: `{w_tr}/{w_e}` "
            f"(uplift_pp_vs_baseline={metrics_u.get('phase2_winner_uplift_pp_vs_baseline')})"
        )
        return {
            "decision": "PASS",
            "reasons": [],
            "metrics": metrics_u,
            "evidence_extra": (
                "; uplift gate: PASS — at least one experiment meets "
                f"min_uplift_pp_vs_baseline (>={min_uplift:.2f} pp vs track baseline)"
                + win_ev
            ),
        }

    metrics_u["phase2_uplift_pass"] = False
    return {
        "decision": "FAIL",
        "reasons": ["phase2_uplift_below_min_pp_vs_baseline"],
        "metrics": metrics_u,
        "evidence_extra": (
            "; uplift gate: FAIL — no experiment meets "
            f"min_uplift_pp_vs_baseline (>={min_uplift:.2f} pp vs track baseline)"
        ),
    }


def _phase2_apply_std_gate(
    bundle: Mapping[str, Any], uplift_out: dict[str, Any]
) -> dict[str, Any]:
    """Optional std gate on PAT@1% series per (track, exp_id).

    Expects ``bundle["phase2_pat_series_by_experiment"]`` as a mapping
    ``track_name -> {exp_id: [p0, p1, ...]}`` with PAT values in ``[0, 1]`` (same
    scale as previews). Sample standard deviation of each list with length >= 2 is
    converted to **percentage points** (×100) and compared to
    ``gate.max_std_pp_across_windows`` (default 2.5).

    When uplift decision is **PASS** and any series exceeds the limit, outcome becomes
    **FAIL** with ``phase2_std_exceeds_max_pp_across_windows``. Non-PASS uplift decisions
    still record std metrics when evaluable, without upgrading to FAIL from std alone.

    Args:
        bundle: Phase 2 bundle (may include optional series payload).
        uplift_out: Result dict from ``_phase2_try_uplift_gate_from_per_job`` (mutated).

    Returns:
        The same ``uplift_out`` dict (possibly updated).
    """
    series_root = bundle.get("phase2_pat_series_by_experiment")
    if not isinstance(series_root, Mapping) or not series_root:
        return uplift_out

    gate_cfg = bundle.get("gate") if isinstance(bundle.get("gate"), Mapping) else {}
    max_allowed = _parse_float_gate(gate_cfg, "max_std_pp_across_windows", 2.5)
    metrics = uplift_out["metrics"]
    per_series: list[dict[str, Any]] = []
    max_pp = 0.0
    any_valid = False

    for tkey, exp_map in series_root.items():
        tr = str(tkey).strip()
        if not tr.startswith("track_"):
            continue
        if not isinstance(exp_map, Mapping):
            continue
        for eid_raw, raw_list in exp_map.items():
            eid = str(eid_raw).strip()
            if not eid or not isinstance(raw_list, list) or len(raw_list) < 2:
                continue
            vals: list[float] = []
            bad = False
            for x in raw_list:
                try:
                    vals.append(float(x))
                except (TypeError, ValueError):
                    bad = True
                    break
            if bad or len(vals) < 2:
                continue
            st_pp = statistics.stdev(vals) * 100.0
            any_valid = True
            max_pp = max(max_pp, st_pp)
            per_series.append(
                {"track": tr, "exp_id": eid, "n_windows": len(vals), "std_pp": st_pp}
            )

    if not any_valid:
        metrics["phase2_std_gate_note"] = (
            "phase2_pat_series_by_experiment present but no track/exp list with "
            ">=2 numeric PAT@1% values"
        )
        return uplift_out

    metrics["phase2_std_gate_evaluated"] = True
    metrics["phase2_std_max_pp_across_experiments"] = max_pp
    metrics["phase2_std_pp_limit"] = max_allowed
    metrics["phase2_std_per_series"] = per_series
    metrics.pop("phase2_std_gate_note", None)

    extra = (
        f"; std gate: max PAT@1% stdev across series={max_pp:.4f} pp "
        f"(limit {max_allowed:.4f} pp)"
    )
    if uplift_out["decision"] == "PASS":
        if max_pp > max_allowed:
            uplift_out["decision"] = "FAIL"
            uplift_out["reasons"] = ["phase2_std_exceeds_max_pp_across_windows"]
            uplift_out["evidence_extra"] += extra + " — FAIL"
        else:
            uplift_out["evidence_extra"] += extra + " — PASS"
    else:
        uplift_out["evidence_extra"] += extra + " (informational; uplift not PASS)"

    return uplift_out


def _phase2_trainer_params_nonempty_for_exp(
    bundle: Mapping[str, Any], track: str, exp_id: str
) -> bool:
    """True when ``bundle['tracks'][track].experiments`` lists ``exp_id`` with non-empty ``trainer_params``."""
    tracks = bundle.get("tracks")
    if not isinstance(tracks, Mapping):
        return False
    tnode = tracks.get(track)
    if not isinstance(tnode, Mapping):
        return False
    exps = tnode.get("experiments")
    if not isinstance(exps, list):
        return False
    for ex in exps:
        if not isinstance(ex, Mapping):
            continue
        if str(ex.get("exp_id") or "").strip() != exp_id:
            continue
        tp = ex.get("trainer_params")
        return isinstance(tp, Mapping) and bool(tp)
    return False


def _phase2_evaluate_strategy_effective(bundle: Mapping[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    """T11A: require argv fingerprint evidence for experiments that declare ``trainer_params``.

    Returns:
        ``(strategy_ok, blocking_reasons, metrics_to_merge)``. When ``trainer_jobs`` did not
        run, ``strategy_ok`` is False but ``blocking_reasons`` is empty (non-blocking).
    """
    tj = bundle.get("trainer_jobs")
    base: dict[str, Any] = {
        "phase2_strategy_effective": False,
        "phase2_trainer_jobs_executed": False,
    }
    if not isinstance(tj, Mapping) or tj.get("executed") is not True:
        base["phase2_strategy_note"] = (
            "trainer_jobs not executed; subprocess argv/fingerprint not audited (T11A)"
        )
        return False, [], base

    base["phase2_trainer_jobs_executed"] = True
    results = tj.get("results")
    if not isinstance(results, list):
        base["phase2_strategy_note"] = "trainer_jobs.executed but results is not a list"
        return False, ["phase2_strategy_params_not_effective"], base

    failures: list[str] = []
    for r in results:
        if not isinstance(r, Mapping):
            continue
        tr = str(r.get("track") or "").strip()
        eid = str(r.get("exp_id") or "").strip()
        if not tr or not eid:
            continue
        if not _phase2_trainer_params_nonempty_for_exp(bundle, tr, eid):
            continue
        fp = r.get("argv_fingerprint")
        argv = r.get("resolved_trainer_argv")
        if not (isinstance(fp, str) and fp.strip()):
            failures.append(f"{tr}/{eid}: missing argv_fingerprint")
            continue
        if not isinstance(argv, list) or len(argv) < 3:
            failures.append(f"{tr}/{eid}: missing resolved_trainer_argv")
            continue
        if r.get("ok") is not True:
            failures.append(f"{tr}/{eid}: trainer job not ok")

    if failures:
        base["phase2_strategy_effective_detail"] = failures
        base["phase2_strategy_note"] = "; ".join(failures[:8])
        if len(failures) > 8:
            base["phase2_strategy_note"] += f"; … (+{len(failures) - 8} more)"
        return False, ["phase2_strategy_params_not_effective"], base

    base["phase2_strategy_effective"] = True
    base["phase2_strategy_note"] = (
        "T11A: every experiment with trainer_params has argv_fingerprint + "
        "resolved_trainer_argv on ok trainer_jobs rows"
    )
    return True, [], base


def _phase2_max_pat_series_window_count(bundle: Mapping[str, Any]) -> int:
    """Largest PAT@1% series length in ``phase2_pat_series_by_experiment``."""
    root = bundle.get("phase2_pat_series_by_experiment")
    if not isinstance(root, Mapping):
        return 0
    mx = 0
    for exp_map in root.values():
        if not isinstance(exp_map, Mapping):
            continue
        for raw_list in exp_map.values():
            if isinstance(raw_list, list):
                mx = max(mx, len(raw_list))
    return mx


def _phase2_conclusion_strength(
    *,
    bundle: Mapping[str, Any],
    gate_status: str,
    strategy_effective: bool,
    trainer_jobs_executed: bool,
) -> str:
    """T11A label: exploratory / comparative / decision_grade (best-effort heuristics)."""
    pjb = bundle.get("per_job_backtest_jobs")
    pjb_ok = isinstance(pjb, Mapping) and pjb.get("executed") is True
    mw = _phase2_max_pat_series_window_count(bundle)
    sci = strategy_effective and trainer_jobs_executed and pjb_ok
    st = str(gate_status).strip().upper()
    if st == "PASS" and sci and mw >= 2:
        return "decision_grade"
    if st == "PASS" and sci:
        return "comparative"
    if sci:
        return "comparative"
    return "exploratory"


def _phase2_append_scientific_validity(
    out: dict[str, Any], bundle: Mapping[str, Any]
) -> dict[str, Any]:
    """Attach ``conclusion_strength`` (T11A) to gate output dict."""
    met = dict(out.get("metrics") or {})
    se = bool(met.get("phase2_strategy_effective"))
    tje = bool(met.get("phase2_trainer_jobs_executed"))
    cs = _phase2_conclusion_strength(
        bundle=bundle,
        gate_status=str(out.get("status") or ""),
        strategy_effective=se,
        trainer_jobs_executed=tje,
    )
    met["conclusion_strength"] = cs
    out = dict(out)
    out["conclusion_strength"] = cs
    out["metrics"] = met
    return out


def extract_phase2_shared_precision_at_recall_1pct(
    backtest_metrics: Mapping[str, Any] | None,
) -> float | None:
    """Read shared PAT @ recall 1% from ingested ``backtest_metrics`` (``model_default``)."""
    if not isinstance(backtest_metrics, Mapping):
        return None
    md = backtest_metrics.get("model_default")
    if not isinstance(md, Mapping):
        return None
    raw = md.get(PHASE2_BACKTEST_PR1_KEY)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def evaluate_phase2_gate(bundle: dict[str, Any]) -> dict[str, Any]:
    """Evaluate Phase 2 gate from a phase2 bundle (T11 minimal).

    When ``bundle["status"] == "plan_only"``, no per-track metrics exist yet; returns
    **BLOCKED** so the run is not mistaken for a passed uplift gate.
    When ``status == "metrics_ingested"``, shared backtest JSON is present; gate stays
    **BLOCKED** until per-track baseline/uplift exists, but evidence includes PAT@1% when
    parsable.

    When ``per_job_backtest_jobs.executed`` is True, ``metrics`` and ``evidence_summary``
    also include per-job PAT@1% previews from ``phase2_per_job_backtest_metrics``.

    When ``status == "metrics_ingested"`` and per-job backtests ran, an **uplift gate**
    compares each track's experiments (YAML order) using those previews vs
    ``gate.min_uplift_pp_vs_baseline``; may return **PASS** / **FAIL** / **BLOCKED**.
    Optional ``bundle["phase2_pat_series_by_experiment"]`` enables
    ``max_std_pp_across_windows`` when per-job uplift gate runs.

    **T11A (scientific validity)** runs on ``metrics_ingested`` **before** the uplift gate:
    when ``trainer_jobs.executed`` is True, every experiment with non-empty
    ``trainer_params`` in ``bundle["tracks"]`` must have a matching
    ``trainer_jobs.results[]`` row with ``argv_fingerprint``, ``resolved_trainer_argv``,
    and ``ok: true``; otherwise **BLOCKED** with ``phase2_strategy_params_not_effective``.
    ``conclusion_strength`` labels how strong a read is (**exploratory** /
    **comparative** / **decision_grade**).

    **T11A (dual-window hard gate)**: after uplift (+ optional std), a **PASS** requires
    ``max(len(series)) >= gate.min_pat_windows_for_pass`` (default **2**) across
    ``bundle["phase2_pat_series_by_experiment"]``; otherwise **BLOCKED** with
    ``phase2_insufficient_pat_windows_for_pass``. Set ``min_pat_windows_for_pass`` to
    **0** to disable.

    When uplift **PASS**, ``metrics`` includes **winner** fields (best ``meets_min_uplift``
    row: ``phase2_winner_track``, ``phase2_winner_exp_id``, ``phase2_winner_baseline_exp_id``,
    ``phase2_winner_uplift_pp_vs_baseline``, ``phase2_winner_preview_pat``).

    When the uplift gate evaluates (per-job backtests executed), ``metrics`` includes
    ``phase2_elimination_rows``: structured **non-winner challengers** with
    ``reason_code`` ``below_min_uplift_pp_vs_baseline`` or
    ``meets_min_uplift_but_not_global_winner`` (T11 narrative; not a full multi-window
    matrix).

    Args:
        bundle: Output of ``collectors.collect_phase2_plan_bundle`` or a future
            enriched bundle with training metrics.

    Returns:
        Dict with ``status`` (``BLOCKED`` / ``FAIL`` / ``PASS``), ``blocking_reasons``,
        ``evidence_summary``, ``metrics``, ``conclusion_strength`` (T11A), and optional
        strategy-audit fields inside ``metrics``.
    """
    reasons: list[str] = []
    errors = bundle.get("errors")
    if isinstance(errors, list) and errors:
        for e in errors:
            if isinstance(e, Mapping) and e.get("code"):
                reasons.append(str(e["code"]))
        _, _, sm_err = _phase2_evaluate_strategy_effective(bundle)
        return _phase2_append_scientific_validity(
            {
                "status": "FAIL",
                "blocking_reasons": reasons or ["phase2_collector_errors"],
                "evidence_summary": "collector recorded errors in phase2 bundle",
                "metrics": sm_err,
            },
            bundle,
        )

    st = str(bundle.get("status") or "")
    if st == "plan_only":
        reasons.append("phase2_bundle_plan_only_no_track_metrics")
        idx = bundle.get("experiments_index")
        n_idx = len(idx) if isinstance(idx, list) else 0
        n_h, n_hf = _job_training_harvest_counts(bundle)
        m_plan: dict[str, Any] = {
            "plan_experiment_slots": n_idx,
            "bundle_kind": bundle.get("bundle_kind"),
        }
        if n_h:
            m_plan["job_training_harvest_rows"] = n_h
            m_plan["job_training_harvest_found"] = n_hf
        pj_m = phase2_per_job_backtest_metrics(bundle)
        if pj_m:
            m_plan.update(pj_m)
        src_counts = phase2_pat_series_source_counts(bundle)
        if src_counts:
            m_plan["phase2_pat_series_source_counts"] = src_counts
        ev_plan = (
            f"bundle is plan_only; {n_idx} experiment slot(s) declared "
            "but no training metrics ingested"
        )
        if src_counts:
            bits = ", ".join(f"{k}={v}" for k, v in sorted(src_counts.items()))
            ev_plan += f"; PAT series source counts: {bits}"
        ev_plan += _phase2_per_job_backtest_evidence_suffix(bundle)
        _, _, strat_plan = _phase2_evaluate_strategy_effective(bundle)
        m_plan.update(strat_plan)
        return _phase2_append_scientific_validity(
            {
                "status": "BLOCKED",
                "blocking_reasons": reasons,
                "evidence_summary": ev_plan,
                "metrics": m_plan,
            },
            bundle,
        )

    if st == "metrics_ingested":
        reasons.append("phase2_shared_metrics_no_per_track_uplift")
        pr1 = extract_phase2_shared_precision_at_recall_1pct(
            bundle.get("backtest_metrics")
            if isinstance(bundle.get("backtest_metrics"), Mapping)
            else None
        )
        pjb_bt = bundle.get("per_job_backtest_jobs")
        will_uplift = (
            isinstance(pjb_bt, Mapping) and pjb_bt.get("executed") is True
        )
        if will_uplift:
            ev = (
                "shared backtest metrics ingested; per-job backtest previews drive "
                "uplift gate; optional bundle phase2_pat_series_by_experiment enables "
                "max_std_pp_across_windows when series are valid"
            )
        else:
            ev = (
                "shared backtest metrics ingested; per-track baseline/uplift and std gate "
                "not evaluable until per-experiment metrics exist"
            )
        if pr1 is not None:
            ev += f"; observed shared PAT@1%={pr1:.4f} (informational only)"
        n_h, n_hf = _job_training_harvest_counts(bundle)
        if n_h:
            ev += (
                f"; job log dirs: training_metrics.json found for {n_hf}/{n_h} "
                "job_spec(s) (per-job trainer output path is T10+)"
            )
        m_met: dict[str, Any] = {
            "has_backtest_metrics": bundle.get("backtest_metrics") is not None,
            "shared_precision_at_recall_1pct": pr1,
            "bundle_kind": bundle.get("bundle_kind"),
        }
        if n_h:
            m_met["job_training_harvest_rows"] = n_h
            m_met["job_training_harvest_found"] = n_hf
        pj_met = phase2_per_job_backtest_metrics(bundle)
        if pj_met:
            m_met.update(pj_met)
        src_counts = phase2_pat_series_source_counts(bundle)
        if src_counts:
            m_met["phase2_pat_series_source_counts"] = src_counts
        ev += _phase2_per_job_backtest_evidence_suffix(bundle)
        if src_counts:
            bits = ", ".join(f"{k}={v}" for k, v in sorted(src_counts.items()))
            ev += f"; PAT series source counts: {bits}"

        _, strat_reas, strat_m = _phase2_evaluate_strategy_effective(bundle)
        m_met.update(strat_m)
        if strat_reas:
            ev += f"; T11A strategy audit: BLOCKED — {strat_m.get('phase2_strategy_note', '')}"
            return _phase2_append_scientific_validity(
                {
                    "status": "BLOCKED",
                    "blocking_reasons": list(strat_reas),
                    "evidence_summary": ev,
                    "metrics": m_met,
                },
                bundle,
            )

        upl = _phase2_try_uplift_gate_from_per_job(bundle)
        if upl is not None:
            gate_cfg = (
                bundle.get("gate") if isinstance(bundle.get("gate"), Mapping) else {}
            )
            upl = _phase2_apply_std_gate(bundle, upl)
            upl = _phase2_apply_min_pat_windows_gate_for_pass(bundle, upl, gate_cfg)
            m_met.update(upl["metrics"])
            ev += upl["evidence_extra"]
            if upl["decision"] == "PASS":
                return _phase2_append_scientific_validity(
                    {
                        "status": "PASS",
                        "blocking_reasons": [],
                        "evidence_summary": ev,
                        "metrics": m_met,
                    },
                    bundle,
                )
            if upl["decision"] == "FAIL":
                return _phase2_append_scientific_validity(
                    {
                        "status": "FAIL",
                        "blocking_reasons": list(upl["reasons"]),
                        "evidence_summary": ev,
                        "metrics": m_met,
                    },
                    bundle,
                )
            return _phase2_append_scientific_validity(
                {
                    "status": "BLOCKED",
                    "blocking_reasons": list(upl["reasons"]),
                    "evidence_summary": ev,
                    "metrics": m_met,
                },
                bundle,
            )

        return _phase2_append_scientific_validity(
            {
                "status": "BLOCKED",
                "blocking_reasons": reasons,
                "evidence_summary": ev,
                "metrics": m_met,
            },
            bundle,
        )

    reasons.append("phase2_gate_unsupported_bundle_status")
    _, _, sm_un = _phase2_evaluate_strategy_effective(bundle)
    return _phase2_append_scientific_validity(
        {
            "status": "BLOCKED",
            "blocking_reasons": reasons,
            "evidence_summary": f"unsupported phase2 bundle status for gate: {st!r}",
            "metrics": sm_un,
        },
        bundle,
    )
