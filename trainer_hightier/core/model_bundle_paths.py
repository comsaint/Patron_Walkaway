"""Resolve versioned Step-5 model bundles under an ``out/*/`` root directory."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

LATEST_MODEL_MANIFEST_NAME: Final[str] = "_latest_model_manifest.json"
_MODEL_VERSION_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"^[\w.\-]+$")


def safe_version_subdirectory(versions_root: Path | str, model_version: str) -> Path:
    """Return ``versions_root / model_version`` after validating *model_version* is a single safe segment."""

    root = Path(versions_root).expanduser().resolve()
    mv = str(model_version).strip()
    if not mv or "/" in mv or "\\" in mv or not _MODEL_VERSION_SAFE_RE.fullmatch(mv):
        raise ValueError(f"Unsafe or empty model_version {model_version!r}")
    return root / mv


def write_latest_model_manifest(
    versions_root: Path | str,
    model_version: str,
    bundle_dir: Path | str,
) -> None:
    """Write ``bundle_relative`` pointer for :func:`resolve_model_bundle_dir`."""

    root = Path(versions_root).expanduser().resolve()
    bd = Path(bundle_dir).expanduser().resolve()
    expected = (root / str(model_version).strip()).resolve()
    if bd != expected:
        rel_any = bd.relative_to(root).as_posix()
        blob: dict[str, Any] = {"bundle_relative": rel_any}
    else:
        blob = {"bundle_relative": str(model_version).strip()}
    out = root / LATEST_MODEL_MANIFEST_NAME
    out.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")


def resolve_model_bundle_dir(
    versions_root: Path | str,
    *,
    explicit_dir: Path | str | None = None,
    model_version: str | None = None,
) -> Path:
    """Locate a bundle directory containing ``model.pkl``.

    Preference order mirrors legacy trainer CLI:

    #. Explicit directory
    #. Named ``model_version`` child of *versions_root*
    #. ``_latest_model_manifest.json``
    #. Legacy flat bundle at *versions_root*

    Raises
    ------
    FileNotFoundError
        When ``model.pkl`` cannot be resolved.
    """

    root = Path(versions_root).expanduser().resolve()

    if explicit_dir is not None:
        d = Path(explicit_dir).expanduser().resolve()
        if not (d / "model.pkl").is_file():
            raise FileNotFoundError(f"No model.pkl under explicit bundle dir {d}")
        return d

    mv = str(model_version or "").strip()
    if mv:
        cand = root / mv
        if not (cand / "model.pkl").is_file():
            raise FileNotFoundError(f"No model.pkl under versions_root/{mv}: {cand}")
        return cand.resolve()

    manifest = root / LATEST_MODEL_MANIFEST_NAME
    if manifest.is_file():
        raw_obj = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw_obj, dict):
            raise ValueError(f"{manifest}: root must be a JSON object, got {type(raw_obj)!r}")
        rel = raw_obj.get("bundle_relative")
        if not rel or not str(rel).strip():
            raise ValueError(f"{manifest}: missing bundle_relative")
        cand = Path(root, *Path(str(rel)).parts).resolve()
        try:
            cand.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{manifest}: bundle_relative resolves outside versions root ({cand})") from exc
        if not (cand / "model.pkl").is_file():
            raise FileNotFoundError(f"Latest manifest points to {cand}, but model.pkl is missing.")
        return cand

    legacy = root / "model.pkl"
    if legacy.is_file():
        return root.resolve()

    raise FileNotFoundError(
        "Could not resolve model bundle: missing _latest_model_manifest.json, "
        f"explicit model_version, and flat model.pkl under {root}"
    )
