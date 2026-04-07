"""Versioned model bundle layout under ``DEFAULT_MODEL_DIR`` (Priority 1 / investigation plan).

Layout::

    <versions_root>/
      _latest_model_manifest.json   # default "latest" pointer
      <model_version>/              # one directory per train run
        model.pkl
        ...

``model_version`` strings must be a single path segment (no ``/``, ``\\``, ``..``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

LATEST_MODEL_MANIFEST_NAME = "_latest_model_manifest.json"


def safe_version_subdirectory(versions_root: Path, model_version: str) -> Path:
    """Return ``versions_root / model_version`` after validation (no path traversal)."""
    cleaned = (model_version or "").strip()
    if not cleaned:
        raise ValueError("model_version must be non-empty")
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError(
            f"model_version must be a single path segment (no separators or ..), got {model_version!r}"
        )
    vr = versions_root.resolve()
    out = (vr / cleaned).resolve()
    try:
        out.relative_to(vr)
    except ValueError as e:
        raise ValueError(
            f"Resolved bundle dir {out} escapes versions_root {vr}"
        ) from e
    return out


def write_latest_model_manifest(
    versions_root: Path,
    model_version: str,
    bundle_dir: Path,
) -> None:
    """Write ``_latest_model_manifest.json`` under *versions_root*."""
    vr = versions_root.resolve()
    bd = bundle_dir.resolve()
    if bd.parent != vr:
        raise ValueError(
            f"bundle_dir must be a direct child of versions_root: got bundle_dir={bd}, root={vr}"
        )
    payload: dict[str, Any] = {
        "model_version": model_version,
        "bundle_relative": bd.name,
    }
    manifest_path = vr / LATEST_MODEL_MANIFEST_NAME
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _log.info("Wrote latest model manifest: %s -> %s", manifest_path, bd.name)


def read_latest_bundle_dir(versions_root: Path) -> Path:
    """Resolve default bundle directory from manifest, with legacy flat-layout fallback."""
    vr = versions_root.resolve()
    manifest_path = vr / LATEST_MODEL_MANIFEST_NAME
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {manifest_path}: {e}") from e
        rel = (data.get("bundle_relative") or "").strip()
        if not rel:
            raise ValueError(f"{manifest_path} missing non-empty bundle_relative")
        return safe_version_subdirectory(vr, rel)
    # Legacy: artifacts directly under versions_root (pre-versioned layout).
    if (vr / "model.pkl").is_file():
        _log.warning(
            "No %s; using legacy flat bundle at %s (consider re-training to emit versioned dir)",
            LATEST_MODEL_MANIFEST_NAME,
            vr,
        )
        return vr
    raise FileNotFoundError(
        f"No {LATEST_MODEL_MANIFEST_NAME} under {vr} and no model.pkl at root; "
        "pass --model-dir or --model-version, or run trainer once."
    )


def resolve_model_bundle_dir(
    versions_root: Path,
    *,
    explicit_dir: Optional[Path] = None,
    model_version: Optional[str] = None,
) -> Path:
    """Resolve bundle directory for backtester / tooling.

    Precedence: *explicit_dir* > *model_version* > latest manifest (or legacy flat).
    """
    if explicit_dir is not None:
        p = explicit_dir.expanduser().resolve()
        if not p.is_dir():
            raise FileNotFoundError(f"model bundle directory does not exist: {p}")
        if not (p / "model.pkl").is_file():
            raise FileNotFoundError(f"No model.pkl under {p}")
        return p
    if model_version is not None and str(model_version).strip():
        cand = safe_version_subdirectory(versions_root, str(model_version).strip())
        if not (cand / "model.pkl").is_file():
            raise FileNotFoundError(
                f"No model.pkl under versioned bundle {cand} (model_version={model_version!r})"
            )
        return cand
    latest = read_latest_bundle_dir(versions_root)
    if not (latest / "model.pkl").is_file():
        raise FileNotFoundError(f"No model.pkl under resolved bundle {latest}")
    return latest
