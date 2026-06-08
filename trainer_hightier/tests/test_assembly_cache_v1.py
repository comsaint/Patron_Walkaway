"""Tests for training assembly cache v1 (L6)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.utils.assembly_cache_v1 import (
    assembly_cache_is_hit,
    assembly_manifest_path,
    assembly_policy_fingerprint_sha256_hex,
    enrich_module_fingerprint_sha256_hex,
    registry_baseline_fingerprint_sha256_hex,
    write_assembly_manifest,
)
from trainer_hightier.utils.source_manifest_v2 import sha256_file_bytes


def test_registry_baseline_fingerprint_order_sensitive() -> None:
    """Registry reorder should change assembly policy (Phase 4 milestone)."""
    a = ("fe__a", "fe__b")
    b = ("fe__b", "fe__a")
    assert registry_baseline_fingerprint_sha256_hex(a) != registry_baseline_fingerprint_sha256_hex(b)


def test_assembly_cache_hit_requires_matching_policy(tmp_path: Path) -> None:
    enriched = tmp_path / "training_set_fe_enriched.parquet"
    pq.write_table(pa.table({"bet_id": [1.0]}), enriched)
    training_fp = sha256_file_bytes(enriched)
    registry_fp = registry_baseline_fingerprint_sha256_hex(("fe__x",))
    enrich_fp = enrich_module_fingerprint_sha256_hex()
    entity_fp = "entity1234567890abcd"
    policy_fp = assembly_policy_fingerprint_sha256_hex(
        registry_baseline_fingerprint_sha256_hex=registry_fp,
        training_base_fingerprint_sha256_hex=training_fp,
        entity_set_fingerprint_sha256_hex=entity_fp,
        enrich_module_fingerprint_sha256_hex=enrich_fp,
    )
    manifest = assembly_manifest_path(enriched)
    write_assembly_manifest(
        manifest_path=manifest,
        enriched_parquet=enriched,
        assembly_policy_fingerprint_sha256_hex=policy_fp,
        registry_baseline_fingerprint_sha256_hex=registry_fp,
        training_base_fingerprint_sha256_hex=training_fp,
        entity_set_fingerprint_sha256_hex=entity_fp,
        row_count=1,
    )
    assert assembly_cache_is_hit(
        manifest_path=manifest,
        enriched_parquet=enriched,
        assembly_policy_fingerprint_sha256_hex=policy_fp,
    )
    other = assembly_policy_fingerprint_sha256_hex(
        registry_baseline_fingerprint_sha256_hex=registry_fp,
        training_base_fingerprint_sha256_hex=training_fp,
        entity_set_fingerprint_sha256_hex="different_entity_fp",
        enrich_module_fingerprint_sha256_hex=enrich_fp,
    )
    assert not assembly_cache_is_hit(
        manifest_path=manifest,
        enriched_parquet=enriched,
        assembly_policy_fingerprint_sha256_hex=other,
    )


def test_assembly_manifest_sidecar_name(tmp_path: Path) -> None:
    enriched = tmp_path / "training_set_fe_enriched.parquet"
    enriched.touch()
    assert assembly_manifest_path(enriched).name == "training_set_fe_enriched.assembly_manifest.json"
