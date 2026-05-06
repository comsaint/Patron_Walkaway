"""Snapshot-scoped deterministic ``trip_id`` (implementation plan §4.1 + SSOT §6)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

_TRIP_ID_BODY_HEX = 32


def derive_trip_id(
    *,
    canonical_id: Any,
    trip_start_gaming_day: str,
    first_run_id: str,
    trip_definition_version: str,
    source_namespace: str,
    source_snapshot_id: str,
) -> str:
    """Return ``trip_<32hex>`` from SHA-256 of canonical JSON (``trip_end_*`` excluded).

    Args:
        canonical_id: PIT-resolved identity key (string); must not be empty after strip.
        trip_start_gaming_day: Trip partition day ``YYYY-MM-DD``.
        first_run_id: First ``run_id`` in the trip (``run_start_ts``, ``run_id`` order).
        trip_definition_version: Trip boundary definition string.
        source_namespace: Logical namespace for the hash.
        source_snapshot_id: L0/L1 snapshot id anchoring the id (SSOT §6).
    """
    cid = str(canonical_id).strip()
    if not cid:
        raise ValueError("canonical_id must be non-empty")
    payload = {
        "canonical_id": cid,
        "first_run_id": str(first_run_id).strip(),
        "source_namespace": str(source_namespace).strip(),
        "source_snapshot_id": str(source_snapshot_id).strip(),
        "trip_definition_version": str(trip_definition_version).strip(),
        "trip_start_gaming_day": str(trip_start_gaming_day).strip(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_TRIP_ID_BODY_HEX]
    return f"trip_{digest}"
