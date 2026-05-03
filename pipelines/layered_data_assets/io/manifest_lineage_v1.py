"""Manifest lineage helpers (LDA-E1-06): ``source_hashes`` + ``ingestion_delay_summary``."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def source_hashes_from_l0_fingerprint(l0_fingerprint_path: Path) -> list[str]:
    """Return ``sha256:<hex>`` for each ``inputs[*].sha256`` in ``snapshot_fingerprint.json``."""
    if not l0_fingerprint_path.is_file():
        return []
    fp = json.loads(l0_fingerprint_path.read_text(encoding="utf-8"))
    inputs = fp.get("inputs")
    if not isinstance(inputs, list):
        return []
    out: list[str] = []
    for item in inputs:
        if isinstance(item, dict) and "sha256" in item:
            out.append(f"sha256:{item['sha256']}")
    return out


def pad_source_hashes(hashes: list[str], min_len: int) -> list[str]:
    """Pad or truncate to ``min_len`` entries (schema / UI stability for single-partition MVP)."""
    h = list(hashes)
    while len(h) < min_len:
        h.append("sha256:unknown")
    return h[:min_len]


def merge_source_hashes_into_manifest(
    manifest: dict[str, Any],
    l0_fingerprint_path: Path | None,
    *,
    pad_to_partitions: bool = True,
) -> dict[str, Any]:
    """Return a copy of ``manifest`` with ``source_hashes`` from fingerprint when available.

    When ``pad_to_partitions`` is true, ``len(source_hashes)`` matches ``len(source_partitions)``
    by padding with the first fingerprint hash (MVP) or ``sha256:unknown``.
    """
    out = dict(manifest)
    if l0_fingerprint_path is None or not l0_fingerprint_path.is_file():
        return out
    raw = source_hashes_from_l0_fingerprint(l0_fingerprint_path)
    if not raw:
        return out
    parts = out.get("source_partitions")
    if pad_to_partitions and isinstance(parts, list) and len(parts) > 0:
        if len(raw) >= len(parts):
            out["source_hashes"] = raw[: len(parts)]
        else:
            padded = list(raw)
            while len(padded) < len(parts):
                padded.append(raw[0])
            out["source_hashes"] = padded
    else:
        out["source_hashes"] = raw
    return out


def merge_ingestion_delay_summary(manifest: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``manifest`` with ``ingestion_delay_summary`` replaced."""
    out = dict(manifest)
    out["ingestion_delay_summary"] = dict(summary)
    return out
