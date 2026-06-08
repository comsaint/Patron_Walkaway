"""Optional pre-Step-5 feature screening hook (manifest-first; default no-op)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Final

from trainer_hightier.config import (
    FeatureScreeningPolicy,
    feature_selection_policy_fingerprint,
)

logger = logging.getLogger(__name__)

FEATURE_SCREENING_MANIFEST_KIND: Final[str] = "feature_screening_manifest_v1"
FEATURE_SCREENING_MANIFEST_SCHEMA_VERSION: Final[int] = 1
FQG_SAMPLE_POLICY_KEY: Final[str] = "sample_policy"


def load_feature_screening_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load and validate a feature screening manifest JSON file."""
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"feature screening manifest not found at {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(
            f"feature screening manifest must be a JSON object at {path}, "
            f"got {type(payload).__name__}",
        )
    validate_feature_screening_manifest(payload)
    return payload


def validate_feature_screening_manifest(manifest: dict[str, Any]) -> None:
    """Validate manifest contract aligned with FQG experiment pipeline fields."""
    if manifest.get("kind") != FEATURE_SCREENING_MANIFEST_KIND:
        raise ValueError(
            f"manifest kind must be {FEATURE_SCREENING_MANIFEST_KIND!r}, "
            f"got {manifest.get('kind')!r}",
        )
    schema_version = manifest.get("schema_version")
    if int(schema_version or 0) != FEATURE_SCREENING_MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"manifest schema_version must be {FEATURE_SCREENING_MANIFEST_SCHEMA_VERSION}, "
            f"got {schema_version!r}",
        )
    selected = manifest.get("selected_features")
    if not isinstance(selected, list) or not selected:
        raise ValueError(
            "manifest selected_features must be a non-empty list of feature names",
        )
    for idx, name in enumerate(selected):
        if not isinstance(name, str) or not str(name).strip():
            raise ValueError(
                f"manifest selected_features[{idx}] must be a non-empty string, got {name!r}",
            )
    method = manifest.get("method")
    if not isinstance(method, str) or not str(method).strip():
        raise ValueError("manifest method must be a non-empty string")
    evidence = manifest.get("evidence_refs")
    if evidence is not None and not isinstance(evidence, list):
        raise TypeError(
            f"manifest evidence_refs must be a list when present, got {type(evidence).__name__}",
        )


def feature_selection_manifest_fingerprint(manifest: dict[str, Any]) -> str:
    """Fingerprint selected feature set + method + evidence refs (distinct from policy fp)."""
    validate_feature_screening_manifest(manifest)
    blob = {
        "kind": FEATURE_SCREENING_MANIFEST_KIND,
        "selected_features": [str(c) for c in manifest["selected_features"]],
        "method": str(manifest["method"]),
        "evidence_refs": manifest.get("evidence_refs") or [],
        "fqg_version": manifest.get("fqg_version"),
    }
    raw = json.dumps(blob, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_selected_features_from_manifest(
    manifest: dict[str, Any],
    *,
    baseline_features: tuple[str, ...],
) -> tuple[str, ...]:
    """Return manifest-selected features that are subset of registry baseline."""
    validate_feature_screening_manifest(manifest)
    baseline_set = set(baseline_features)
    selected_raw = [str(c).strip() for c in manifest["selected_features"]]
    if len(selected_raw) != len(set(selected_raw)):
        dupes = sorted({c for c in selected_raw if selected_raw.count(c) > 1})
        raise ValueError(f"manifest selected_features contains duplicates: {dupes}")
    unknown = [c for c in selected_raw if c not in baseline_set]
    if unknown:
        raise ValueError(
            f"manifest selected_features not in registry baseline: {unknown}; "
            f"baseline_count={len(baseline_features)}",
        )
    return tuple(selected_raw)


def resolve_step5_feature_columns(
    *,
    baseline_features: tuple[str, ...],
    policy: FeatureScreeningPolicy,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Apply optional screening hook; default path returns baseline unchanged."""
    baseline = tuple(baseline_features)
    if not baseline:
        raise ValueError("baseline_features must be non-empty for Step 5")
    policy_fp = feature_selection_policy_fingerprint(policy)
    meta: dict[str, Any] = {
        "enabled": bool(policy.enabled),
        "noop": not bool(policy.enabled),
        "feature_selection_policy_fingerprint": policy_fp,
        "baseline_feature_count": len(baseline),
        "registry_baseline_preserved": True,
    }
    if not policy.enabled:
        meta["selected_feature_count"] = len(baseline)
        meta["selected_features"] = list(baseline)
        return baseline, meta

    manifest_path = policy.manifest_path
    if manifest_path is None:
        raise ValueError("feature_screening enabled requires manifest_path")
    manifest = load_feature_screening_manifest(manifest_path)
    selected = resolve_selected_features_from_manifest(manifest, baseline_features=baseline)
    manifest_fp = feature_selection_manifest_fingerprint(manifest)
    meta.update(
        {
            "noop": False,
            "manifest_path": str(Path(manifest_path).resolve()),
            "manifest_kind": manifest.get("kind"),
            "manifest_schema_version": manifest.get("schema_version"),
            "method": manifest.get("method"),
            "fqg_version": manifest.get("fqg_version"),
            "evidence_refs": manifest.get("evidence_refs") or [],
            "feature_selection_manifest_fingerprint": manifest_fp,
            "selected_feature_count": len(selected),
            "selected_features": list(selected),
        },
    )
    logger.info(
        "[feature_screening] enabled method=%s selected=%d baseline=%d manifest=%s",
        manifest.get("method"),
        len(selected),
        len(baseline),
        Path(manifest_path).name,
    )
    return selected, meta
