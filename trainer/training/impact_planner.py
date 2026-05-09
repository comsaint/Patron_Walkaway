"""Impacted-only materialization planner (spec delta + snapshot signals).

Pure functions: no trainer imports. Produces ``(layer, feature_id, partition_id)``
work units with ``impact_reason`` for audit and future cache orchestration.
Partition-level precision requires upstream callers to supply partition ids;
when unknown, ``partition_id`` is ``*`` and ``impact_scope`` is ``full_matrix``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from trainer.features.features import get_cross_layer_compose_contract
from trainer.training.data_sources import _OPTIONAL_BET_LDA_RUN_TRIP_COLS


IMPACT_PLANNER_VERSION: str = "impact_planner_v3"


def _infer_spec_track_layer(feature_id: str, spec: dict) -> Optional[str]:
    """Return ``bet`` / ``run`` / ``trip`` / ``player`` for a candidate in *spec*."""
    track_default = {
        "bet_duckdb_window": "bet",
        "run_state_machine": "run",
        "player_run_asset": "player",
        "trip_asset_materialized": "trip",
    }
    for track in (
        "bet_duckdb_window",
        "run_state_machine",
        "player_run_asset",
        "trip_asset_materialized",
    ):
        for cand in (spec.get(track) or {}).get("candidates") or []:
            if isinstance(cand, dict) and str(cand.get("feature_id") or "") == feature_id:
                tl = cand.get("target_layer")
                if tl in ("bet", "run", "trip", "player"):
                    return str(tl)
                return str(track_default[track])
    return None


def resolve_materialization_layer(feature_id: str, spec: dict) -> str:
    """Best-effort layer for impact bookkeeping (includes ``trip`` for LDA bridge columns)."""
    if feature_id in _OPTIONAL_BET_LDA_RUN_TRIP_COLS:
        return "trip"
    lyr = _infer_spec_track_layer(feature_id, spec)
    if lyr is not None:
        return lyr
    return "bet"


def work_unit_asset_id(
    *,
    layer: str,
    feature_id: str,
    partition_id: str,
    feature_fingerprint: str,
    source_snapshot_id: str,
    pit_policy_id: str,
    compute_policy_version: str,
) -> str:
    """Deterministic portable id for one impacted work unit (plan partition contract)."""
    payload = {
        "kind": "materialization_work_unit",
        "layer": str(layer),
        "feature_id": str(feature_id),
        "partition_id": str(partition_id),
        "feature_fingerprint": str(feature_fingerprint),
        "source_snapshot_id": str(source_snapshot_id),
        "pit_policy_id": str(pit_policy_id),
        "compute_policy_version": str(compute_policy_version),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _expand_work_units_to_partitions(
    work: List[Dict[str, Any]],
    partition_ids: Optional[Sequence[str]],
) -> List[Dict[str, Any]]:
    """Replace ``partition_id`` ``*`` with concrete chunk partition ids when provided."""
    if not partition_ids:
        return work
    out: List[Dict[str, Any]] = []
    for w in work:
        if w.get("partition_id") != "*":
            out.append(dict(w))
            continue
        for pid in partition_ids:
            ww = dict(w)
            ww["partition_id"] = str(pid)
            out.append(ww)
    return out


def _compose_downstream_closure(
    spec: dict,
    seed_features: Set[str],
) -> Set[str]:
    """Extend *seed_features* with any cross-layer compose features that depend on them."""
    cross = get_cross_layer_compose_contract(spec)
    if not cross or not seed_features:
        return set(seed_features)
    out = set(seed_features)
    changed = True
    while changed:
        changed = False
        for fid, meta in cross.items():
            deps = set(str(x) for x in (meta.get("depends_on") or []) if str(x))
            inputs = set(str(x) for x in (meta.get("input_columns") or []) if str(x))
            upstream = deps | inputs
            if upstream & out and fid not in out:
                out.add(fid)
                changed = True
    return out


def plan_impacted_materialization_work(
    *,
    curr_spec: dict,
    prev_spec: Optional[dict],
    prev_per_feature_fp: Optional[Mapping[str, str]],
    curr_per_feature_fp: Optional[Mapping[str, str]] = None,
    prev_source_snapshot_id: Optional[str],
    curr_source_snapshot_id: Optional[str],
    lookback_partition_count: Optional[int] = None,
    chunk_partition_ids: Optional[Sequence[str]] = None,
    pit_policy_id: str = "cutoff_window",
) -> Dict[str, Any]:
    """Return impacted work units and observability flags.

    Parameters
    ----------
    curr_spec:
        Current feature spec (must be a dict).
    prev_spec:
        Previous spec for removed-candidate detection; may be None on first run.
    prev_per_feature_fp / curr_per_feature_fp:
        Fingerprints for spec semantics; when *curr* omitted, computed from *curr_spec*.
    prev_source_snapshot_id / curr_source_snapshot_id:
        Bridge / training lineage snapshot ids. When both are non-empty and differ,
        impact defaults to conservative data refresh (all declared features + trip LDA).
    lookback_partition_count:
        Optional hint for operators; when snapshot changes and this is None,
        ``impact_scope`` is ``full_matrix`` (plan: upgrade to full recompute when
        partition boundary unknown).
    chunk_partition_ids:
        When set (e.g. from Step 1 ``get_single_window_chunk``), work units with
        ``partition_id='*'`` are expanded to one row per chunk partition so
        ``(layer, feature_id, partition_id)`` is concrete for orchestration.
    pit_policy_id:
        PIT / identity policy label for ``asset_id`` hashing (aligns with trainer).
    """
    if not isinstance(curr_spec, dict):
        raise TypeError(f"curr_spec must be dict, got {type(curr_spec).__name__}")
    from trainer.training import feature_materialization as _fm

    curr_fp = dict(curr_per_feature_fp or _fm.per_feature_fingerprints(curr_spec))
    _cpv = _fm.compute_policy_version()
    _snap = str(curr_source_snapshot_id or "unknown").strip() or "unknown"
    reasons: List[str] = []
    work: List[Dict[str, Any]] = []
    full_matrix = False
    miss_reason: Optional[str] = None

    # --- Spec fingerprint propagation (bet_duckdb_window depends_on + compose closure) ---
    if prev_per_feature_fp is not None:
        hint = _fm.impacted_feature_ids_on_fingerprint_change(prev_per_feature_fp, curr_fp, curr_spec)
        impacted_ids = set(hint.get("impacted_feature_ids") or [])
        impacted_ids = _compose_downstream_closure(curr_spec, impacted_ids)
        if impacted_ids:
            reasons.append("SPEC_FINGERPRINT_OR_DEPENDS_CLOSURE")
            for fid in sorted(impacted_ids):
                lyr = resolve_materialization_layer(fid, curr_spec)
                work.append(
                    {
                        "layer": lyr,
                        "feature_id": fid,
                        "partition_id": "*",
                        "impact_reason": "SPEC_FINGERPRINT_OR_DEPENDS_CLOSURE",
                    }
                )

    # --- Removed candidates (prev spec only): stop new output / exclude at assemble ---
    if isinstance(prev_spec, dict):
        prev_ids = {
            str(c.get("feature_id"))
            for track in (
                "bet_duckdb_window",
                "run_state_machine",
                "player_run_asset",
                "trip_asset_materialized",
            )
            for c in ((prev_spec.get(track) or {}).get("candidates") or [])
            if isinstance(c, dict) and c.get("feature_id")
        }
        curr_ids = {
            str(c.get("feature_id"))
            for track in (
                "bet_duckdb_window",
                "run_state_machine",
                "player_run_asset",
                "trip_asset_materialized",
            )
            for c in ((curr_spec.get(track) or {}).get("candidates") or [])
            if isinstance(c, dict) and c.get("feature_id")
        }
        removed = sorted(prev_ids - curr_ids)
        for fid in removed:
            reasons.append("FEATURE_REMOVED_FROM_SPEC")
            lyr = resolve_materialization_layer(fid, prev_spec)
            work.append(
                {
                    "layer": lyr,
                    "feature_id": fid,
                    "partition_id": "*",
                    "impact_reason": "FEATURE_REMOVED_FROM_SPEC",
                }
            )

    # --- Data snapshot change: conservative refresh ---
    prev_s = (prev_source_snapshot_id or "").strip()
    curr_s = (curr_source_snapshot_id or "").strip()
    if prev_s and curr_s and prev_s != curr_s:
        reasons.append("DATA_SNAPSHOT_ID_CHANGED")
        if chunk_partition_ids:
            full_matrix = False
            miss_reason = (
                "source_snapshot_id changed; impacts scoped to known chunk "
                f"partitions (n={len(chunk_partition_ids)}) — verify lookback vs late-arrival"
            )
        else:
            full_matrix = True
            miss_reason = (
                "partition-level impact unknown after source_snapshot_id change "
                "(no chunk_partition_ids); treat as full_matrix (see lookback_partition_count)"
            )
        declared: Set[str] = set()
        for track in (
            "bet_duckdb_window",
            "run_state_machine",
            "player_run_asset",
            "trip_asset_materialized",
        ):
            for cand in (curr_spec.get(track) or {}).get("candidates") or []:
                if isinstance(cand, dict) and cand.get("feature_id"):
                    declared.add(str(cand["feature_id"]))
        for fid in sorted(declared):
            lyr = resolve_materialization_layer(fid, curr_spec)
            work.append(
                {
                    "layer": lyr,
                    "feature_id": fid,
                    "partition_id": "*",
                    "impact_reason": "DATA_SNAPSHOT_ID_CHANGED",
                }
            )
        for col in _OPTIONAL_BET_LDA_RUN_TRIP_COLS:
            work.append(
                {
                    "layer": "trip",
                    "feature_id": col,
                    "partition_id": "*",
                    "impact_reason": "DATA_SNAPSHOT_ID_CHANGED",
                }
            )

    work = _expand_work_units_to_partitions(work, chunk_partition_ids)

    # De-duplicate work units (same key may appear from spec + compose)
    seen: Set[Tuple[str, str, str, str]] = set()
    deduped: List[Dict[str, Any]] = []
    for w in work:
        key = (w["layer"], w["feature_id"], w["partition_id"], w["impact_reason"])
        if key in seen:
            continue
        seen.add(key)
        ww = dict(w)
        fid = str(ww.get("feature_id") or "")
        lyr = str(ww.get("layer") or "bet")
        pid = str(ww.get("partition_id") or "*")
        if fid in curr_fp:
            _fp = str(curr_fp[fid])
        elif lyr == "trip" and fid.startswith("lda_"):
            _fp = "lda_bridge"
        elif ww.get("impact_reason") == "FEATURE_REMOVED_FROM_SPEC":
            _fp = "removed"
        else:
            _fp = ""
        ww["asset_id"] = work_unit_asset_id(
            layer=lyr,
            feature_id=fid,
            partition_id=pid,
            feature_fingerprint=_fp,
            source_snapshot_id=_snap,
            pit_policy_id=str(pit_policy_id),
            compute_policy_version=_cpv,
        )
        deduped.append(ww)

    impact_scope = "full_matrix" if full_matrix else ("none" if not deduped else "spec_or_lineage_delta")
    if lookback_partition_count is not None:
        impact_scope = f"{impact_scope};lookback_partitions={int(lookback_partition_count)}"
    if chunk_partition_ids:
        impact_scope = f"{impact_scope};chunk_partitions={len(chunk_partition_ids)}"

    return {
        "impact_planner_version": IMPACT_PLANNER_VERSION,
        "impact_reasons": sorted(set(reasons)),
        "impact_scope": impact_scope,
        "full_matrix_recommended": bool(full_matrix),
        "chunk_partition_ids": list(chunk_partition_ids) if chunk_partition_ids else None,
        "miss_reason": miss_reason,
        "impacted_work_unit_count": len(deduped),
        "impacted_work_units": deduped[:5000],
        "impacted_work_units_truncated": len(deduped) > 5000,
    }
