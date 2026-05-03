"""Atomic replace for Parquet + JSON manifest sidecar (LDA execution plan §5.3 row 4)."""

from __future__ import annotations

import os
from pathlib import Path


def staged_parquet_path(final_parquet: Path) -> Path:
    """Return sibling path ``<name>.tmp`` for staging DuckDB ``COPY … TO`` output."""
    return final_parquet.with_name(final_parquet.name + ".tmp")


def staged_manifest_path(final_manifest: Path) -> Path:
    """Return sibling path for staging ``manifest.json`` before atomic replace."""
    return final_manifest.with_name(final_manifest.name + ".tmp")


def remove_staged_outputs(*paths: Path) -> None:
    """Best-effort delete of stale ``*.tmp`` files before a new run."""
    for p in paths:
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass


def commit_parquet_and_manifest(
    *,
    staged_parquet: Path,
    final_parquet: Path,
    manifest_text: str,
    final_manifest: Path,
) -> None:
    """Write manifest to a temp file, then ``os.replace`` parquet then manifest.

    Args:
        staged_parquet: Fully written parquet (typically ``*.parquet.tmp``).
        final_parquet: Target ``*.parquet`` path.
        manifest_text: UTF-8 JSON text for ``manifest.json``.
        final_manifest: Target ``manifest.json`` path.

    Raises:
        FileNotFoundError: If ``staged_parquet`` does not exist.
    """
    tmp_manifest = staged_manifest_path(final_manifest)
    if not staged_parquet.is_file():
        raise FileNotFoundError(f"staged parquet missing: {staged_parquet}")
    final_parquet.parent.mkdir(parents=True, exist_ok=True)
    final_manifest.parent.mkdir(parents=True, exist_ok=True)
    remove_staged_outputs(tmp_manifest)
    tmp_manifest.write_text(manifest_text, encoding="utf-8")
    os.replace(staged_parquet, final_parquet)
    os.replace(tmp_manifest, final_manifest)
