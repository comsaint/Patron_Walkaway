"""Unified L2 reuse key contracts (source / window / label store).

Pure helpers: no trainer pipeline imports. Aligns cache sidecars with
``l2_trainer_contracts.LABEL_INVALIDATION_SEMANTIC_KEYS``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Tuple

AUTO_L2_KIND_V2 = "trainer_auto_l2_bundle_v2"
AUTO_L2_KIND_V3 = "trainer_auto_l2_bundle_v3"

REQUIRED_V3_SOURCE_INVARIANT_FIELDS: frozenset[str] = frozenset(
    {
        "bridge_manifest_stat",
        "feature_spec_fingerprint",
        "compute_policy_version",
        "identity_mapping_mode",
        "identity_mapping_revision",
        "label_definition_version",
        "censoring_policy_id",
        "source_snapshot_id",
        "rebuild_canonical_mapping",
    }
)
REQUIRED_V3_WINDOW_VIEW_FIELDS: frozenset[str] = frozenset(
    {
        "window_start_iso",
        "window_end_iso",
        "recent_chunks",
        "train_split_frac",
        "valid_split_frac",
        "neg_sample_frac_config",
        "force_recompute",
    }
)


def validate_expected_l2_cache_key(expected: Mapping[str, Any], *, strict: bool) -> Optional[str]:
    """Return a human-readable validation error or None when *expected* is usable (fail-closed)."""
    if not strict:
        return None
    v3_on = True
    try:
        from trainer.core import _config_training_domain as _tdom

        v3_on = bool(getattr(_tdom, "L2_REUSE_V3_CACHE_KEYS", True))
    except Exception:
        v3_on = True
    if not v3_on:
        return None
    kind = expected.get("kind")
    if kind != AUTO_L2_KIND_V3:
        return f"expected kind={kind!r} (need {AUTO_L2_KIND_V3!r})"
    src = expected.get("source_invariant")
    view = expected.get("window_view")
    if not isinstance(src, dict):
        return "source_invariant must be dict"
    if not isinstance(view, dict):
        return "window_view must be dict"
    miss_src = REQUIRED_V3_SOURCE_INVARIANT_FIELDS - set(src.keys())
    if miss_src:
        return f"source_invariant missing keys: {sorted(miss_src)}"
    miss_view = REQUIRED_V3_WINDOW_VIEW_FIELDS - set(view.keys())
    if miss_view:
        return f"window_view missing keys: {sorted(miss_view)}"
    ws = str(view.get("window_start_iso") or "").strip()
    we = str(view.get("window_end_iso") or "").strip()
    if not ws or not we:
        return "window_view has empty window_start_iso or window_end_iso"
    return None


def identity_mapping_revision(*, identity_mapping_mode: str, pit_identity_engine: str) -> str:
    """Revision token for label + L2 keys (mode + engine)."""
    return f"{str(identity_mapping_mode).strip()}:{str(pit_identity_engine).strip()}"


def censoring_policy_id_from_semantics(
    *,
    walkaway_gap_min: int,
    alert_horizon_min: int,
    label_lookahead_min: int,
) -> str:
    """Match :func:`trainer.training.label_asset_cache.build_label_disk_cache_components` censoring hash."""
    import hashlib

    sem = json.dumps(
        {
            "WALKAWAY_GAP_MIN": int(walkaway_gap_min),
            "ALERT_HORIZON_MIN": int(alert_horizon_min),
            "LABEL_LOOKAHEAD_MIN": int(label_lookahead_min),
        },
        sort_keys=True,
    )
    return hashlib.sha256(sem.encode()).hexdigest()[:16]


def build_source_invariant_key_parts(
    *,
    bridge_manifest_stat: Optional[str],
    feature_spec_fingerprint: str,
    compute_policy_version: str,
    identity_mapping_mode: str,
    identity_mapping_revision: str,
    label_definition_version: str,
    censoring_policy_id: str,
    source_snapshot_id: str,
    rebuild_canonical_mapping: bool,
) -> Dict[str, Any]:
    """Fields that must invalidate heavy assets when changed (excludes window)."""
    return {
        "bridge_manifest_stat": bridge_manifest_stat,
        "feature_spec_fingerprint": str(feature_spec_fingerprint),
        "compute_policy_version": str(compute_policy_version),
        "identity_mapping_mode": str(identity_mapping_mode),
        "identity_mapping_revision": str(identity_mapping_revision),
        "label_definition_version": str(label_definition_version),
        "censoring_policy_id": str(censoring_policy_id),
        "source_snapshot_id": str(source_snapshot_id).strip() or "unknown",
        "rebuild_canonical_mapping": bool(rebuild_canonical_mapping),
    }


def build_window_view_key_parts(
    *,
    window_start_iso: str,
    window_end_iso: str,
    recent_chunks: Optional[int],
    train_split_frac: float,
    valid_split_frac: float,
    neg_sample_frac_config: float,
    force_recompute: bool,
) -> Dict[str, Any]:
    """Window / split parameters (view layer only)."""
    return {
        "window_start_iso": str(window_start_iso),
        "window_end_iso": str(window_end_iso),
        "recent_chunks": recent_chunks,
        "train_split_frac": float(train_split_frac),
        "valid_split_frac": float(valid_split_frac),
        "neg_sample_frac_config": float(neg_sample_frac_config),
        "force_recompute": bool(force_recompute),
    }


def build_auto_l2_cache_key_v3(
    *,
    bridge_manifest_stat: Optional[str],
    window_start_iso: str,
    window_end_iso: str,
    recent_chunks: Optional[int],
    train_split_frac: float,
    valid_split_frac: float,
    neg_sample_frac_config: float,
    feature_spec_fingerprint: str,
    rebuild_canonical_mapping: bool,
    identity_mapping_mode: str,
    pit_identity_engine: str,
    source_snapshot_id: str,
    label_definition_version: str,
    censoring_policy_id: str,
    compute_policy_version: str,
    force_recompute: bool,
) -> Dict[str, Any]:
    """Full v3 auto-bundle cache sidecar payload."""
    im_rev = identity_mapping_revision(
        identity_mapping_mode=identity_mapping_mode,
        pit_identity_engine=pit_identity_engine,
    )
    src = build_source_invariant_key_parts(
        bridge_manifest_stat=bridge_manifest_stat,
        feature_spec_fingerprint=feature_spec_fingerprint,
        compute_policy_version=compute_policy_version,
        identity_mapping_mode=identity_mapping_mode,
        identity_mapping_revision=im_rev,
        label_definition_version=label_definition_version,
        censoring_policy_id=censoring_policy_id,
        source_snapshot_id=source_snapshot_id,
        rebuild_canonical_mapping=rebuild_canonical_mapping,
    )
    view = build_window_view_key_parts(
        window_start_iso=window_start_iso,
        window_end_iso=window_end_iso,
        recent_chunks=recent_chunks,
        train_split_frac=train_split_frac,
        valid_split_frac=valid_split_frac,
        neg_sample_frac_config=neg_sample_frac_config,
        force_recompute=force_recompute,
    )
    out_v3 = {
        "kind": AUTO_L2_KIND_V3,
        "source_invariant": src,
        "window_view": view,
    }
    try:
        from trainer.core import _config_training_domain as _tdom

        _err = validate_expected_l2_cache_key(
            out_v3,
            strict=bool(getattr(_tdom, "L2_REUSE_STRICT_KEY_SCHEMA", True)),
        )
    except Exception:
        _err = None
    if _err:
        raise ValueError(f"build_auto_l2_cache_key_v3: invalid composed key ({_err})")
    return out_v3


def _norm_key_blob(d: Mapping[str, Any]) -> str:
    return json.dumps(dict(d), sort_keys=True, separators=(",", ":"), default=str)


def source_invariant_match(cached: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """Return True when ``source_invariant`` dicts match canonically."""
    a = cached.get("source_invariant")
    b = expected.get("source_invariant")
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return _norm_key_blob(a) == _norm_key_blob(b)


def window_view_match(cached: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    """Return True when ``window_view`` dicts match canonically."""
    a = cached.get("window_view")
    b = expected.get("window_view")
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return _norm_key_blob(a) == _norm_key_blob(b)


def normalize_auto_l2_cache_key(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Upgrade legacy v2 flat payloads to v3 layout for comparison."""
    kind = raw.get("kind")
    if kind == AUTO_L2_KIND_V3 and isinstance(raw.get("source_invariant"), dict) and isinstance(
        raw.get("window_view"), dict
    ):
        return dict(raw)
    # v2 or unknown flat layout
    if kind != AUTO_L2_KIND_V2 and "window_start_iso" not in raw:
        return dict(raw)
    src = {
        "bridge_manifest_stat": raw.get("bridge_manifest_stat"),
        "feature_spec_fingerprint": raw.get("feature_spec_fingerprint", ""),
        "compute_policy_version": raw.get("compute_policy_version", "trainer_default_v1"),
        "identity_mapping_mode": raw.get("identity_mapping_mode", "cutoff_window"),
        "identity_mapping_revision": raw.get(
            "identity_mapping_revision",
            identity_mapping_revision(
                identity_mapping_mode=str(raw.get("identity_mapping_mode", "cutoff_window")),
                pit_identity_engine=str(raw.get("pit_identity_engine", "cutoff_window_map")),
            ),
        ),
        "label_definition_version": raw.get("label_definition_version", "unknown_v2_sidecar"),
        "censoring_policy_id": raw.get("censoring_policy_id", "unknown_v2_sidecar"),
        "source_snapshot_id": raw.get("source_snapshot_id", "unknown_v2_sidecar"),
        "rebuild_canonical_mapping": bool(raw.get("rebuild_canonical_mapping", False)),
    }
    view = {
        "window_start_iso": str(raw.get("window_start_iso", "")),
        "window_end_iso": str(raw.get("window_end_iso", "")),
        "recent_chunks": raw.get("recent_chunks"),
        "train_split_frac": float(raw.get("train_split_frac", 0.0)),
        "valid_split_frac": float(raw.get("valid_split_frac", 0.0)),
        "neg_sample_frac_config": float(raw.get("neg_sample_frac_config", 1.0)),
        "force_recompute": bool(raw.get("force_recompute", False)),
    }
    return {"kind": AUTO_L2_KIND_V3, "source_invariant": src, "window_view": view}


def resolve_l2_auto_cache(
    *,
    bundle_dir: Any,
    expected_key: Mapping[str, Any],
    bundle_files_ok: bool,
) -> Dict[str, Any]:
    """Return reuse diagnostics without I/O beyond caller-supplied *bundle_files_ok*."""
    from pathlib import Path

    try:
        from trainer.core import _config_training_domain as _tdom

        _strict = bool(getattr(_tdom, "L2_REUSE_STRICT_KEY_SCHEMA", True))
    except Exception:
        _strict = True
    _exp_err = validate_expected_l2_cache_key(expected_key, strict=_strict)
    if _exp_err is not None:
        return {
            "l2_cache_full_hit": False,
            "l2_cache_source_invariant_match": False,
            "l2_cache_window_view_match": False,
            "l2_cache_miss_reason": "invalid_expected_key",
            "l2_cache_invalid_key_detail": _exp_err,
            "source_cache_hit": False,
            "view_cache_hit": False,
        }

    p = Path(bundle_dir)
    cached = None
    sidecar = p / ".l2_bundle_cache_key.json"
    if sidecar.is_file():
        try:
            cached = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            cached = None
    exp = normalize_auto_l2_cache_key(expected_key)
    cache_norm = normalize_auto_l2_cache_key(cached) if isinstance(cached, dict) else None
    src_match = bool(cache_norm and source_invariant_match(cache_norm, exp))
    view_match = bool(cache_norm and window_view_match(cache_norm, exp))
    full_hit = bool(bundle_files_ok and cache_norm and src_match and view_match)
    miss_reason: Optional[str] = None
    if not bundle_files_ok:
        miss_reason = "bundle_files_missing"
    elif cache_norm is None:
        miss_reason = "no_cache_sidecar"
    elif not src_match:
        miss_reason = "source_invariant_mismatch"
    elif not view_match:
        miss_reason = "window_view_mismatch"
    else:
        miss_reason = None
    return {
        "l2_cache_full_hit": full_hit,
        "l2_cache_source_invariant_match": src_match,
        "l2_cache_window_view_match": view_match,
        "l2_cache_miss_reason": miss_reason,
        "source_cache_hit": bool(src_match),
        "view_cache_hit": bool(view_match),
    }
