"""PLAN §7 `slice_contract` — pure helpers for Phase 1 slice RCA (W1-B2).

All logic is in-memory (no I/O). Callers supply **rated** eval rows and T0-as-of
profile fields per ``canonical_id`` (already resolved to the profile row that
should be used for this eval, i.e. ``snapshot_dtm <= T0`` and latest).

References: ``../../.cursor/plans/PLAN_precision_uplift_sprint.md`` §7.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")

def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def eval_date_hkt(decision_ts: Any) -> str | None:
    """§7.4 ``eval_date`` as ISO calendar date string in Asia/Hong_Kong."""
    dt = _parse_ts(decision_ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=HK)
    else:
        dt = dt.astimezone(HK)
    return dt.date().isoformat()


def tenure_bucket_days(days_since_first_session: Any) -> str | None:
    """§7.7 fixed buckets; returns ``T0_seg`` / ``T1`` / ``T2`` / ``T3``."""
    if days_since_first_session is None:
        return None
    try:
        d = float(days_since_first_session)
    except (TypeError, ValueError):
        return None
    if math.isnan(d) or d < 0:
        return None
    if d <= 7:
        return "T0_seg"
    if d <= 30:
        return "T1"
    if d <= 90:
        return "T2"
    return "T3"


def _empirical_decile_labels(values: Sequence[float]) -> list[str | None]:
    """Map each value to ``*_d1``…``*_d10`` using inclusive decile cutpoints.

    Uses the empirical distribution of *all supplied values* (eval-row weighted
    per §7.6 — one entry per row). Ties share the same decile index derived from
    average rank (midrank).
    """
    n = len(values)
    if n == 0:
        return []
    ranks = [0.0] * n
    sorted_idx = sorted(range(n), key=lambda i: values[i])
    i = 0
    while i < n:
        j = i
        v0 = values[sorted_idx[i]]
        while j < n and values[sorted_idx[j]] == v0:
            j += 1
        mid = (i + j - 1) / 2.0
        for k in range(i, j):
            ranks[sorted_idx[k]] = mid
        i = j
    labels: list[str | None] = []
    for r in ranks:
        # decile 1 = lowest ~10%: floor(10 * (r + 0.5) / n) clamped to 0..9
        if n == 1:
            idx = 0
        else:
            idx = int(min(9, max(0, math.floor(10.0 * (r + 0.5) / n))))
        labels.append(f"d{idx + 1}")
    return labels


def _prefix_decile(decile_suffixes: list[str | None], prefix: str) -> list[str | None]:
    out: list[str | None] = []
    for s in decile_suffixes:
        if s is None:
            out.append(None)
        else:
            out.append(f"{prefix}_{s}")
    return out


def profile_assertion_violations(profile: Mapping[str, Any]) -> list[str]:
    """§7.3 violations for one player (already T0-as-of row). Codes for gate text."""
    bad: list[str] = []
    ad = profile.get("active_days_30d")
    try:
        ad_ok = ad is not None and int(ad) >= 1
    except (TypeError, ValueError):
        ad_ok = False
    if not ad_ok:
        bad.append("active_days_30d_lt_1_or_invalid")
    if profile.get("theo_win_sum_30d") is None:
        bad.append("theo_win_sum_30d_null")
    if profile.get("turnover_sum_30d") is None:
        bad.append("turnover_sum_30d_null")
    return bad


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    return v


def build_slice_contract_bundle(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Build ``slice_contract`` bundle from an inline spec (tests / dry-run).

    Keys:
        ``T0`` (ISO timestamp, zone-aware or naive HK-as-intended),
        ``eval_rows`` (list of dict): ``canonical_id``, ``decision_ts``,
        ``table_id`` optional, ``score`` optional, ``label`` (0/1),
        ``is_alert`` optional (else derived from ``score`` + ``recall_score_threshold``),
        ``bet_id`` optional,
        ``profiles`` (dict[str, dict]): profile fields per §7.2 for that player.

    Optional:
        ``recall_score_threshold`` (float): used when ``is_alert`` missing.
        Phase 1 collector may set this from R1 ``precision_at_recall_target.threshold_at_target``,
        else from ``backtest_metrics`` ``model_default`` / ``rated`` key ``threshold_at_recall_0.01``,
        when the key is omitted in config.
        ``min_slice_n`` (int): minimum rows to include a slice in top_drag (default 5).
    """
    T0_raw = spec.get("T0")
    T0 = _parse_ts(T0_raw)
    if T0 is None:
        return {
            "slice_contract_version": "inline-v1",
            "slice_data_incomplete": True,
            "slice_contract_violations": [],
            "blocking_profile_codes": ["T0_unparseable"],
            "top_drag_slices": [],
            "row_annotations": [],
            "notes": "T0 missing or unparseable",
        }

    eval_rows_in = spec.get("eval_rows")
    if not isinstance(eval_rows_in, list):
        return {
            "slice_contract_version": "inline-v1",
            "slice_data_incomplete": True,
            "slice_contract_violations": [],
            "blocking_profile_codes": ["no_eval_rows"],
            "top_drag_slices": [],
            "row_annotations": [],
            "notes": "eval_rows must be a non-empty list",
        }

    thr_raw = spec.get("recall_score_threshold", 0.5)
    try:
        thr = float(thr_raw)
    except (TypeError, ValueError):
        thr = 0.5

    min_n = int(spec.get("min_slice_n", 5) or 5)
    profiles = spec.get("profiles") if isinstance(spec.get("profiles"), Mapping) else {}

    violations: list[dict[str, Any]] = []
    row_ann: list[dict[str, Any]] = []
    incomplete = False
    blocking_codes: list[str] = []

    adt_vals: list[float] = []
    act_vals: list[float] = []
    to_vals: list[float] = []

    prepared: list[dict[str, Any]] = []

    for raw in eval_rows_in:
        if not isinstance(raw, Mapping):
            continue
        cid = str(raw.get("canonical_id") or "").strip()
        if not cid:
            incomplete = True
            blocking_codes.append("missing_canonical_id")
            continue
        prof = profiles.get(cid)
        if not isinstance(prof, Mapping):
            incomplete = True
            violations.append({"canonical_id": cid, "codes": ("no_profile_row",)})
            blocking_codes.append("T0_no_profile")
            continue
        pv = profile_assertion_violations(prof)
        if pv:
            incomplete = True
            violations.append({"canonical_id": cid, "codes": tuple(pv)})
            blocking_codes.extend(pv)
            continue

        ed = eval_date_hkt(raw.get("decision_ts"))
        if ed is None:
            incomplete = True
            blocking_codes.append("decision_ts_missing")
            continue

        ad = int(prof["active_days_30d"])
        theo = float(prof["theo_win_sum_30d"])
        to = float(prof["turnover_sum_30d"])
        adt = theo / float(ad)
        adt_vals.append(adt)
        act_vals.append(float(ad))
        to_vals.append(to)

        is_alert = raw.get("is_alert")
        if is_alert is None:
            sc = _safe_float(raw.get("score"))
            is_alert = bool(sc is not None and sc >= thr) if sc is not None else False
        else:
            is_alert = bool(is_alert)
        try:
            lab = int(raw.get("label"))
        except (TypeError, ValueError):
            lab = 0

        tb = tenure_bucket_days(prof.get("days_since_first_session"))
        if tb is None:
            incomplete = True
            blocking_codes.append("tenure_bucket_unavailable")

        prepared.append(
            {
                "canonical_id": cid,
                "bet_id": raw.get("bet_id"),
                "eval_date": ed,
                "table_id": raw.get("table_id") if raw.get("table_id") not in (None, "") else "UNKNOWN_TABLE",
                "adt_30d": adt,
                "active_days_30d": float(ad),
                "turnover_sum_30d": to,
                "tenure_bucket": tb or "UNKNOWN_TENURE",
                "is_alert": is_alert,
                "label": lab,
            }
        )

    if not prepared:
        return {
            "slice_contract_version": "inline-v1",
            "slice_data_incomplete": True,
            "slice_contract_violations": violations,
            "blocking_profile_codes": sorted(set(blocking_codes)) if blocking_codes else ["no_prepared_rows"],
            "top_drag_slices": [],
            "row_annotations": [],
            "notes": "no eval rows after validation",
        }

    adt_lbl = _prefix_decile(_empirical_decile_labels(adt_vals), "adt")
    act_lbl = _prefix_decile(_empirical_decile_labels(act_vals), "activity")
    to_lbl = _prefix_decile(_empirical_decile_labels(to_vals), "to")

    global_tp = sum(1 for r in prepared if r["is_alert"] and r["label"] == 1)
    global_fp = sum(1 for r in prepared if r["is_alert"] and r["label"] != 1)
    global_prec = (global_tp / (global_tp + global_fp)) if (global_tp + global_fp) > 0 else None

    for i, r in enumerate(prepared):
        row_ann.append(
            {
                "canonical_id": r["canonical_id"],
                "bet_id": r.get("bet_id"),
                "eval_date": r["eval_date"],
                "table_id": r["table_id"],
                "adt_percentile_bucket": adt_lbl[i],
                "activity_percentile_bucket": act_lbl[i],
                "turnover_30d_percentile_bucket": to_lbl[i],
                "tenure_bucket": r["tenure_bucket"],
                "is_alert": r["is_alert"],
                "label": r["label"],
            }
        )

    def _dim_stats(key_fn) -> dict[str, dict[str, Any]]:
        buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "n": 0})
        for i, r in enumerate(prepared):
            k = key_fn(i, r)
            if k is None:
                continue
            buckets[k]["n"] += 1
            if r["is_alert"]:
                if r["label"] == 1:
                    buckets[k]["tp"] += 1
                else:
                    buckets[k]["fp"] += 1
        out_stats: dict[str, dict[str, Any]] = {}
        for k, c in buckets.items():
            tp, fp, n = c["tp"], c["fp"], c["n"]
            denom = tp + fp
            prec = (tp / denom) if denom > 0 else None
            delta = None
            if prec is not None and global_prec is not None:
                delta = prec - global_prec
            out_stats[k] = {
                "n": n,
                "tp": tp,
                "fp": fp,
                "precision_at_target_recall": prec,
                "delta_vs_global": delta,
                "confidence_flag": n < min_n,
            }
        return out_stats

    dim_fns = [
        ("eval_date", lambda i, r: r["eval_date"]),
        ("table_id", lambda i, r: str(r["table_id"])),
        ("adt_percentile_bucket", lambda i, r: adt_lbl[i]),
        ("tenure_bucket", lambda i, r: r["tenure_bucket"]),
        ("activity_percentile_bucket", lambda i, r: act_lbl[i]),
        ("turnover_30d_percentile_bucket", lambda i, r: to_lbl[i]),
    ]

    drag: list[dict[str, Any]] = []
    for dim, fn in dim_fns:
        stats = _dim_stats(fn)
        for sk, st in stats.items():
            if st["n"] < min_n:
                continue
            prec = st["precision_at_target_recall"]
            drag.append(
                {
                    "dimension": dim,
                    "slice_key": str(sk),
                    "n": st["n"],
                    "tp": st["tp"],
                    "fp": st["fp"],
                    "precision_at_target_recall": prec,
                    "delta_vs_global": st["delta_vs_global"],
                    "confidence_flag": st["confidence_flag"],
                }
            )

    def _drag_sort_key(item: Mapping[str, Any]) -> tuple[float, int]:
        d = item.get("delta_vs_global")
        d_f = float(d) if isinstance(d, (int, float)) else 0.0
        n = int(item.get("n") or 0)
        return (d_f, -n)

    drag.sort(key=_drag_sort_key)
    top10 = drag[:10]

    return {
        "slice_contract_version": "inline-v1",
        "slice_data_incomplete": incomplete,
        "slice_contract_violations": violations,
        "blocking_profile_codes": sorted(set(blocking_codes)),
        "global_precision_at_alert": global_prec,
        "top_drag_slices": top10,
        "row_annotations": row_ann,
        "notes": None,
    }
