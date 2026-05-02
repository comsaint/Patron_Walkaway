"""Canonical ingest fingerprints and deterministic ``source_snapshot_id`` derivation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

INGEST_RECIPE_VERSION_DEFAULT = "l0_ingest_v1"


def sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Return lowercase hex SHA-256 of file contents (streaming, bounded buffer)."""
    if not path.is_file():
        raise FileNotFoundError(f"Source file not found: {path}")
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def relative_path_for_fingerprint(path: Path, anchor: Path) -> str:
    """Return POSIX path relative to ``anchor``; require ``path`` to resolve under ``anchor``.

    Governance: fingerprints must never record absolute paths (cross-machine stability).
    """
    resolved = path.resolve()
    ares = anchor.resolve()
    try:
        return resolved.relative_to(ares).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"Source must resolve under anchor {ares}; got {resolved}. "
            "Use paths under the repo (or widen --anchor-path to a common parent)."
        ) from exc


def normalized_sorted_sources(paths: list[Path]) -> list[Path]:
    """Deduplicate by resolved path and return stable sorted order (matches fingerprint order)."""
    uniq: dict[str, Path] = {}
    for p in paths:
        rp = p.resolve()
        uniq[rp.as_posix()] = rp
    return sorted(uniq.values(), key=lambda x: x.as_posix())


def build_input_records(paths: list[Path], anchor: Path) -> list[dict[str, Any]]:
    """Build sorted ``inputs`` records: ``relative_path``, ``sha256``, ``size_bytes``."""
    records: list[dict[str, Any]] = []
    for src in normalized_sorted_sources(paths):
        records.append(
            {
                "relative_path": relative_path_for_fingerprint(src, anchor),
                "sha256": sha256_file(src),
                "size_bytes": src.stat().st_size,
            }
        )
    records.sort(key=lambda r: str(r["relative_path"]))
    return records


def build_fingerprint_document(
    *,
    source_paths: list[Path],
    anchor: Path,
    table: str,
    partition_key: str,
    partition_value: str,
    ingest_recipe_version: str = INGEST_RECIPE_VERSION_DEFAULT,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the fingerprint dict written to ``snapshot_fingerprint.json``.

    ``source_paths`` order does not matter; inputs are normalized and sorted by
    ``relative_path`` for determinism.
    """
    doc: dict[str, Any] = {
        "ingest_recipe_version": ingest_recipe_version,
        "layout": {
            "table": table.strip(),
            "partition_key": partition_key.strip(),
            "partition_value": partition_value.strip(),
        },
        "inputs": build_input_records(source_paths, anchor),
    }
    if extra:
        doc["extra"] = dict(extra)
    return doc


def fingerprint_canonical_json(doc: Mapping[str, Any]) -> str:
    """Serialize ``doc`` to a canonical JSON string (stable for hashing)."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def derive_source_snapshot_id(canonical_json: str, *, body_hex_chars: int = 32) -> str:
    """Return ``snap_<hex>`` where hex is the first ``body_hex_chars`` of SHA-256(canonical_json)."""
    if body_hex_chars < 8 or body_hex_chars > 120:
        raise ValueError(f"body_hex_chars must be in [8, 120], got {body_hex_chars}")
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    body = digest[:body_hex_chars]
    return f"snap_{body}"
