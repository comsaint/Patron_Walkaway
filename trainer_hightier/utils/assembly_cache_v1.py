"""Training assembly cache v1 (L6): registry-driven Step 3.5 enrich output."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from trainer_hightier.utils.source_manifest_v2 import sha256_file_bytes, write_json_atomic

ASSEMBLY_KIND: Final[str] = "training_assembly_v1"
ASSEMBLY_SCHEMA_VERSION: Final[int] = 1


def enrich_module_fingerprint_sha256_hex() -> str:
    """Content fingerprint for ``dataset_enrich`` join module."""
    mod = Path(__file__).resolve().parents[1] / "feature_experiment" / "dataset_enrich.py"
    return hashlib.sha256(mod.read_bytes()).hexdigest()


def registry_baseline_fingerprint_sha256_hex(baseline_features: tuple[str, ...]) -> str:
    """Fingerprint for ordered registry baseline feature list."""
    blob = json.dumps(list(baseline_features), separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _policy_blob_sha256(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def assembly_policy_fingerprint_sha256_hex(
    *,
    registry_baseline_fingerprint_sha256_hex: str,
    training_base_fingerprint_sha256_hex: str,
    entity_set_fingerprint_sha256_hex: str,
    enrich_module_fingerprint_sha256_hex: str,
    short_term_parquet_fingerprint_sha256_hex: str | None = None,
    mid_term_snapshot_fingerprint_sha256_hex: str | None = None,
) -> str:
    """Stable fingerprint for ``training_set_fe_enriched.parquet`` assembly policy."""
    return _policy_blob_sha256(
        {
            "kind": ASSEMBLY_KIND,
            "registry_baseline_fingerprint": str(registry_baseline_fingerprint_sha256_hex).strip(),
            "training_base_fingerprint": str(training_base_fingerprint_sha256_hex).strip(),
            "entity_set_fingerprint": str(entity_set_fingerprint_sha256_hex).strip(),
            "enrich_module_fingerprint": str(enrich_module_fingerprint_sha256_hex).strip(),
            "short_term_parquet_fingerprint": str(short_term_parquet_fingerprint_sha256_hex or "").strip(),
            "mid_term_snapshot_fingerprint": str(mid_term_snapshot_fingerprint_sha256_hex or "").strip(),
        },
    )


def assembly_manifest_path(enriched_parquet: Path) -> Path:
    """Sidecar manifest path beside enriched training parquet."""
    p = Path(enriched_parquet).resolve()
    return p.with_name(f"{p.stem}.assembly_manifest.json")


def load_assembly_manifest(path: Path) -> dict[str, Any] | None:
    """Load assembly manifest JSON or ``None`` when missing/corrupt."""
    p = Path(path).resolve()
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def assembly_cache_is_hit(
    *,
    manifest_path: Path,
    enriched_parquet: Path,
    assembly_policy_fingerprint_sha256_hex: str,
) -> bool:
    """Return True when enriched parquet + manifest match the assembly policy."""
    out = Path(enriched_parquet).resolve()
    if not out.is_file():
        return False
    prev = load_assembly_manifest(manifest_path)
    if prev is None:
        return False
    return str(prev.get("assembly_policy_fingerprint")) == str(assembly_policy_fingerprint_sha256_hex)


def parquet_content_fingerprint(path: Path | None) -> str | None:
    """Return SHA-256 hex for an existing parquet file."""
    if path is None:
        return None
    p = Path(path).resolve()
    if not p.is_file():
        return None
    return sha256_file_bytes(p)


def write_assembly_manifest(
    *,
    manifest_path: Path,
    enriched_parquet: Path,
    assembly_policy_fingerprint_sha256_hex: str,
    registry_baseline_fingerprint_sha256_hex: str,
    training_base_fingerprint_sha256_hex: str,
    entity_set_fingerprint_sha256_hex: str,
    row_count: int | None = None,
) -> Path:
    """Persist assembly sidecar manifest after successful enrich."""
    payload = {
        "schema_version": ASSEMBLY_SCHEMA_VERSION,
        "kind": ASSEMBLY_KIND,
        "assembly_policy_fingerprint": str(assembly_policy_fingerprint_sha256_hex),
        "registry_baseline_fingerprint": str(registry_baseline_fingerprint_sha256_hex),
        "training_base_fingerprint": str(training_base_fingerprint_sha256_hex),
        "entity_set_fingerprint": str(entity_set_fingerprint_sha256_hex),
        "enriched_output_path": str(Path(enriched_parquet).resolve()),
        "row_count": int(row_count) if row_count is not None else None,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    mp = Path(manifest_path).resolve()
    write_json_atomic(mp, payload)
    return mp
