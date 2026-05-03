"""Locate the repository root for CLI defaults and manifest URI anchors."""

from __future__ import annotations

from pathlib import Path


def discover_repo_root() -> Path:
    """Return the directory that contains both ``scripts/`` and ``schema/``.

    Returns:
        Absolute path to the repository root.

    Raises:
        RuntimeError: If no ancestor of this file satisfies the layout.
    """
    here = Path(__file__).resolve()
    for anc in here.parents:
        if (anc / "scripts").is_dir() and (anc / "schema").is_dir():
            return anc
    raise RuntimeError(
        "Could not locate repo root (expected directories scripts/ and schema/). "
        f"Walked ancestors from {here}"
    )
