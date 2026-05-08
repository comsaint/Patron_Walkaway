"""Unified feature materialization helpers (spec-first, lineage, impact hints).

Implements slices of the unified multi-layer materialization plan: declared-id
registry, per-candidate fingerprints for cache/audit, cross-layer compose index,
and optional strict validation that training matrices do not carry undeclared
feature columns. Heavy per-layer asset stores and a full incremental planner
remain future work; this module is the shared contract surface.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from trainer.features.features import (
    get_all_candidate_feature_ids,
    get_cross_layer_compose_contract,
)


def _truthy_env(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def strict_spec_first_enabled() -> bool:
    """When True, undeclared feature-like columns in the training matrix abort the run."""
    return _truthy_env("TRAINER_SPEC_FIRST_STRICT")


def declared_feature_id_set(spec: Optional[dict]) -> Set[str]:
    """All ``feature_id`` values declared across the three tracks (including non-screening)."""
    if not isinstance(spec, dict):
        return set()
    return {str(x) for x in get_all_candidate_feature_ids(spec, screening_only=False) if str(x)}


# Columns that may appear in chunk / split Parquets but are not YAML ``feature_id`` entries.
_TRAINING_MATRIX_RESERVED_COLUMNS: frozenset[str] = frozenset(
    {
        "bet_id",
        "session_id",
        "player_id",
        "canonical_id",
        "label",
        "censored",
        "is_rated",
        "payout_complete_dtm",
        "gaming_day",
        "run_id",
        "skip_reason_code",
        "prediction_skip",
        "sample_weight",
        "weight",
        "wager",
        "status",
        "casino_win",
        "payout_odds",
        "base_ha",
        "is_back_bet",
        "position_idx",
        "game_id",
        "table_id",
        "extended_zone",
        "alert_horizon_end",
        "reason_codes",
    }
)


def _is_reserved_training_column(name: str) -> bool:
    if name in _TRAINING_MATRIX_RESERVED_COLUMNS:
        return True
    if name.startswith("_"):
        return True
    if name.startswith("lda_"):
        return True
    return False


def find_undeclared_feature_columns(
    columns: Iterable[str],
    spec: Optional[dict],
) -> List[str]:
    """Return columns that look like model features but are not declared in *spec*."""
    declared = declared_feature_id_set(spec)
    out: List[str] = []
    for col in columns:
        if col in declared or _is_reserved_training_column(col):
            continue
        out.append(str(col))
    return sorted(out)


def validate_spec_first_training_columns(
    columns: Sequence[str],
    spec: Optional[dict],
) -> Tuple[bool, str]:
    """Return (ok, detail) for spec-first matrix column policy."""
    if spec is None:
        return True, "no feature_spec loaded; spec-first check skipped"
    bad = find_undeclared_feature_columns(columns, spec)
    if not bad:
        return True, "all non-reserved columns are declared feature_id values"
    return (
        False,
        f"undeclared columns present ({len(bad)}): {bad[:30]}"
        + (" …" if len(bad) > 30 else ""),
    )


def fingerprint_candidate(cand: Mapping[str, Any], track: str) -> str:
    """Short stable fingerprint for one YAML candidate (expression + deps + postprocess)."""
    payload = {
        "track": track,
        "feature_id": cand.get("feature_id"),
        "type": cand.get("type"),
        "expression": cand.get("expression"),
        "window_frame": cand.get("window_frame"),
        "depends_on": cand.get("depends_on"),
        "input_columns": cand.get("input_columns"),
        "output_columns": cand.get("output_columns"),
        "function_name": cand.get("function_name"),
        "postprocess": cand.get("postprocess"),
        "dtype": cand.get("dtype"),
        "source_column": cand.get("source_column"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def per_feature_fingerprints(spec: Optional[dict]) -> Dict[str, str]:
    """Map ``feature_id`` -> fingerprint string for audit and future incremental keys."""
    if not isinstance(spec, dict):
        return {}
    out: Dict[str, str] = {}
    for track in ("track_llm", "track_human", "track_profile"):
        cands = ((spec.get(track) or {}).get("candidates") or [])
        for raw in cands:
            if not isinstance(raw, dict):
                continue
            fid = raw.get("feature_id")
            if not fid:
                continue
            out[str(fid)] = fingerprint_candidate(raw, track)
    return dict(sorted(out.items()))


def _depends_closure_for_track_llm(spec: dict) -> Dict[str, Set[str]]:
    """Adjacency for track_llm ``depends_on`` edges (derived only)."""
    adj: Dict[str, Set[str]] = {}
    for cand in (spec.get("track_llm") or {}).get("candidates", []) or []:
        if not isinstance(cand, dict):
            continue
        fid = str(cand.get("feature_id") or "")
        if not fid:
            continue
        deps = cand.get("depends_on") or []
        if isinstance(deps, list) and deps:
            adj[fid] = {str(d) for d in deps if str(d)}
    return adj


def impacted_feature_ids_on_fingerprint_change(
    prev_fps: Mapping[str, str],
    curr_fps: Mapping[str, str],
    spec: dict,
) -> Dict[str, Any]:
    """Heuristic impacted set when per-feature fingerprints change (spec or semantics).

    Uses track_llm ``depends_on`` reverse reachability: if feature *dep*'s fingerprint
    changes, every derived feature that (transitively) lists *dep* in ``depends_on``
    is treated as impacted for recomputation planning.
    """
    changed = {k for k, v in curr_fps.items() if prev_fps.get(k) != v}
    adj = _depends_closure_for_track_llm(spec)
    # Reverse graph: dependency feature_id -> consumers that list it in depends_on.
    rev: Dict[str, Set[str]] = {}
    for parent, deps in adj.items():
        for d in deps:
            rev.setdefault(d, set()).add(parent)
    impacted: Set[str] = set(changed)
    stack = list(changed)
    while stack:
        cur = stack.pop()
        for consumer in rev.get(cur, ()):
            if consumer not in impacted:
                impacted.add(consumer)
                stack.append(consumer)
    return {
        "changed_feature_ids": sorted(changed),
        "impacted_feature_ids": sorted(impacted),
        "impacted_count": len(impacted),
    }


def build_pipeline_feature_materialization_audit(
    *,
    feature_spec: Optional[dict],
    train_columns: Optional[Sequence[str]] = None,
    prev_per_feature_fp: Optional[Mapping[str, str]] = None,
    prev_spec: Optional[dict] = None,
    prev_source_snapshot_id: Optional[str] = None,
    curr_source_snapshot_id: Optional[str] = None,
    lookback_partition_count: Optional[int] = None,
    pit_policy_id: str = "cutoff_window",
    chunk_partition_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """JSON-serialisable audit block for ``pipeline_diagnostics.json``."""
    fps = per_feature_fingerprints(feature_spec)
    cross = get_cross_layer_compose_contract(feature_spec) if isinstance(feature_spec, dict) else {}
    pap = _player_layer_asset_path()
    audit: Dict[str, Any] = {
        "manifest_version": "feature_materialization_audit_v1",
        "strict_spec_first_env": "TRAINER_SPEC_FIRST_STRICT",
        "strict_spec_first_enabled": strict_spec_first_enabled(),
        "compute_policy_version": compute_policy_version(),
        "declared_feature_count": len(declared_feature_id_set(feature_spec)),
        "per_feature_fingerprint_count": len(fps),
        "per_feature_fingerprints": fps,
        "cross_layer_compose_feature_ids": sorted(cross.keys()),
        "player_layer_asset_rollout": {
            "env_flag": "TRAINER_PLAYER_LAYER_ASSET",
            "env_path": "TRAINER_PLAYER_LAYER_ASSET_PATH",
            "legacy_env_enabled": _truthy_env("TRAINER_PLAYER_LAYER_ASSET"),
            "asset_path_set": bool(pap),
            "status": "asset_parquet" if pap else "inline_join_default",
        },
        "chunk_partition_ids": list(chunk_partition_ids) if chunk_partition_ids else None,
    }
    if isinstance(feature_spec, dict):
        from trainer.training.impact_planner import plan_impacted_materialization_work, resolve_materialization_layer

        audit["impact_plan"] = plan_impacted_materialization_work(
            curr_spec=feature_spec,
            prev_spec=prev_spec,
            prev_per_feature_fp=prev_per_feature_fp,
            curr_per_feature_fp=fps,
            prev_source_snapshot_id=prev_source_snapshot_id,
            curr_source_snapshot_id=curr_source_snapshot_id,
            lookback_partition_count=lookback_partition_count,
            chunk_partition_ids=chunk_partition_ids,
        )
        # WS2: sample cache-key lexicon for compose + a few declared ids (bounded size).
        _pid_sample = (chunk_partition_ids[0] if chunk_partition_ids else "*")
        _sample: List[Dict[str, Any]] = []
        for fid in sorted(cross.keys())[:12]:
            uhash = upstream_fingerprint_closure_hash(feature_spec, fid, fps)
            own_fp = fps.get(fid, "")
            _sample.append(
                {
                    "feature_id": fid,
                    "node_kind": "compose",
                    "cache_key_parts": build_feature_cache_key_parts(
                        layer="compose",
                        feature_id=fid,
                        partition_id=_pid_sample,
                        source_snapshot_id=str(curr_source_snapshot_id or "unknown"),
                        pit_policy_id=pit_policy_id,
                        feature_fingerprint=own_fp,
                        node_kind="compose",
                        upstream_fingerprint_closure_hash=uhash,
                    ),
                }
            )
        for fid in sorted(fps.keys())[:8]:
            if fid in cross:
                continue
            lyr = resolve_materialization_layer(fid, feature_spec)
            _sample.append(
                {
                    "feature_id": fid,
                    "node_kind": "layer",
                    "cache_key_parts": build_feature_cache_key_parts(
                        layer=lyr,
                        feature_id=fid,
                        partition_id=_pid_sample,
                        source_snapshot_id=str(curr_source_snapshot_id or "unknown"),
                        pit_policy_id=pit_policy_id,
                        feature_fingerprint=fps[fid],
                    ),
                }
            )
        audit["cache_key_lexicon_sample"] = _sample
    if train_columns is not None:
        ok, detail = validate_spec_first_training_columns(train_columns, feature_spec)
        audit["spec_first_column_check"] = {"ok": ok, "detail": detail}
        audit["undeclared_column_count"] = len(find_undeclared_feature_columns(train_columns, feature_spec))
    if prev_per_feature_fp is not None and isinstance(feature_spec, dict):
        audit["impact_hint_vs_previous_run"] = impacted_feature_ids_on_fingerprint_change(
            prev_per_feature_fp, fps, feature_spec,
        )
    audit["materialization_gates"] = evaluate_materialization_gate_bundle()
    return audit


def maybe_raise_spec_first_columns(train_columns: Sequence[str], feature_spec: Optional[dict]) -> None:
    """Raise ``RuntimeError`` when strict spec-first mode rejects the matrix."""
    if not strict_spec_first_enabled():
        return
    ok, detail = validate_spec_first_training_columns(train_columns, feature_spec)
    if not ok:
        raise RuntimeError(f"TRAINER_SPEC_FIRST_STRICT: {detail}")


def compute_policy_version() -> str:
    """Version token for cache keys (override with ``TRAINER_COMPUTE_POLICY_VERSION``)."""
    v = (os.environ.get("TRAINER_COMPUTE_POLICY_VERSION") or "").strip()
    return v if v else "trainer_default_v1"


def upstream_fingerprint_closure_hash(
    feature_spec: dict,
    compose_feature_id: str,
    per_fp: Mapping[str, str],
) -> str:
    """Closure hash over upstream ``feature_id`` fingerprints for cross-layer compose nodes."""
    cross = get_cross_layer_compose_contract(feature_spec)
    meta = cross.get(compose_feature_id)
    if not meta:
        return ""
    dep_ids = sorted(
        {
            str(x)
            for x in list(meta.get("depends_on") or []) + list(meta.get("input_columns") or [])
            if str(x)
        }
    )
    parts = [str(per_fp.get(d, "")) for d in dep_ids]
    blob = json.dumps({"compose": compose_feature_id, "deps": dep_ids, "fp": parts}, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()[:12]


def build_feature_cache_key_parts(
    *,
    layer: str,
    feature_id: str,
    partition_id: str,
    source_snapshot_id: str,
    pit_policy_id: str,
    feature_fingerprint: str,
    node_kind: str = "layer",
    upstream_fingerprint_closure_hash: str = "",
) -> Dict[str, str]:
    """Canonical key fields for feature-level materialization cache (WS2)."""
    out: Dict[str, str] = {
        "node_kind": str(node_kind),
        "layer": str(layer),
        "feature_id": str(feature_id),
        "partition_id": str(partition_id),
        "source_snapshot_id": str(source_snapshot_id),
        "pit_policy_id": str(pit_policy_id),
        "compute_policy_version": compute_policy_version(),
        "feature_fingerprint": str(feature_fingerprint),
    }
    if upstream_fingerprint_closure_hash:
        out["upstream_fingerprint_closure_hash"] = str(upstream_fingerprint_closure_hash)
    return out


def format_feature_cache_key(parts: Mapping[str, str]) -> str:
    """Stable single-string cache key from :func:`build_feature_cache_key_parts`."""
    return json.dumps(dict(parts), sort_keys=True, separators=(",", ":"))


def _player_layer_asset_path() -> str:
    return (os.environ.get("TRAINER_PLAYER_LAYER_ASSET_PATH") or "").strip()


def evaluate_materialization_gate_bundle() -> Dict[str, Any]:
    """Optional fail-closed gates for materialization / asset contracts (WS6).

    Strict mode: set ``TRAINER_MATERIALIZATION_STRICT_GATES=1`` to abort on failure.
    ``player_layer_asset_path_guard`` performs a single ``Path.is_file`` check when
    ``TRAINER_PLAYER_LAYER_ASSET_PATH`` is set (lightweight I/O at gate boundary).
    """
    from pathlib import Path

    gates: Dict[str, Dict[str, Any]] = {}
    pap = _player_layer_asset_path()
    if pap:
        ok = Path(pap).is_file()
        gates["player_layer_asset_path_guard"] = {
            "ok": ok,
            "detail": f"TRAINER_PLAYER_LAYER_ASSET_PATH={pap!r} exists={ok}",
            "miss_reason": None if ok else "PLAYER_LAYER_ASSET_PATH_MISSING",
        }
    else:
        gates["player_layer_asset_path_guard"] = {
            "ok": True,
            "detail": "TRAINER_PLAYER_LAYER_ASSET_PATH unset (inline profile path)",
            "miss_reason": None,
        }

    all_ok = all(bool(g.get("ok")) for g in gates.values())
    return {
        "materialization_gate_contract_version": "2026-05-08",
        "strict_materialization_gates_enabled": _truthy_env("TRAINER_MATERIALIZATION_STRICT_GATES"),
        "all_ok": all_ok,
        "gates": gates,
    }


def raise_if_strict_materialization_gates_failed(report: Mapping[str, Any]) -> None:
    """Raise ``RuntimeError`` when strict materialization gates fail."""
    if not _truthy_env("TRAINER_MATERIALIZATION_STRICT_GATES"):
        return
    gates = report.get("gates") or {}
    failed: List[str] = []
    for name, body in gates.items():
        if isinstance(body, dict) and not body.get("ok", False):
            mr = body.get("miss_reason") or body.get("detail") or ""
            failed.append(f"{name}: {mr}")
    if failed:
        raise RuntimeError("TRAINER_MATERIALIZATION_STRICT_GATES: " + " | ".join(failed))
