"""L0 directory layout and ``source_snapshot_id`` validation for layered assets."""
from __future__ import annotations

import re
from pathlib import Path

_SNAP_PREFIX = "snap_"
_ID_BODY = re.compile(r"^[A-Za-z0-9_-]{8,120}$")


def validate_source_snapshot_id(snapshot_id: str) -> None:
    """Validate ``source_snapshot_id`` shape; raise ``ValueError`` if invalid.

    Rules match ``doc/l0_layered_data_assets_convention.md`` §2.1.
    """
    if not isinstance(snapshot_id, str):
        raise TypeError(f"snapshot_id must be str, got {type(snapshot_id).__name__}")
    sid = snapshot_id.strip()
    if not sid.startswith(_SNAP_PREFIX):
        raise ValueError(f"snapshot_id must start with {_SNAP_PREFIX!r}, got {snapshot_id!r}")
    body = sid[len(_SNAP_PREFIX) :]
    if not body:
        raise ValueError("snapshot_id body is empty after snap_ prefix")
    if ".." in sid or "/" in sid or "\\" in sid:
        raise ValueError(f"snapshot_id must not contain path separators or '..', got {snapshot_id!r}")
    if not _ID_BODY.match(body):
        raise ValueError(
            f"snapshot_id body must match {_ID_BODY.pattern} (8-120 safe chars), got {snapshot_id!r}"
        )


def l0_layered_root(data_root: Path) -> Path:
    """Return ``<data_root>/l0_layered``."""
    if not isinstance(data_root, Path):
        raise TypeError(f"data_root must be pathlib.Path, got {type(data_root).__name__}")
    return data_root / "l0_layered"


def l0_snapshot_root(data_root: Path, snapshot_id: str) -> Path:
    """Return root directory for one immutable L0 snapshot."""
    validate_source_snapshot_id(snapshot_id)
    return l0_layered_root(data_root) / snapshot_id.strip()


def discover_l0_snapshot_ids_for_partition(
    data_root: Path,
    *,
    table: str,
    partition_key: str,
    partition_value: str,
) -> list[str]:
    """Return sorted ``snap_*`` ids under ``l0_layered`` that have ``part-*.parquet`` for this Hive partition.

    Used when L0 was ingested per day: each ingest embeds ``partition_value`` in the fingerprint, so
    ``source_snapshot_id`` often differs by calendar day even for the same logical batch.

    ``partition_value`` must be safe for a path segment (same rules as :func:`l0_partition_dir`).
    """
    if not isinstance(table, str) or not table.strip():
        raise ValueError(f"table must be a non-empty string, got {table!r}")
    if not isinstance(partition_key, str) or not partition_key.strip():
        raise ValueError(f"partition_key must be a non-empty string, got {partition_key!r}")
    if not isinstance(partition_value, str) or not partition_value.strip():
        raise ValueError(f"partition_value must be a non-empty string, got {partition_value!r}")
    if ".." in partition_key or "=" in partition_key:
        raise ValueError(f"invalid partition_key: {partition_key!r}")
    if (
        ".." in partition_value
        or "/" in partition_value
        or "\\" in partition_value
        or "=" in partition_value
    ):
        raise ValueError(
            f"partition_value must not contain '=', path separators, or '..', got {partition_value!r}"
        )
    root = l0_layered_root(data_root)
    if not root.is_dir():
        return []
    out: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith(_SNAP_PREFIX):
            continue
        try:
            validate_source_snapshot_id(child.name)
        except ValueError:
            continue
        part = child / table.strip() / f"{partition_key.strip()}={partition_value.strip()}"
        if part.is_dir() and any(part.glob("part-*.parquet")):
            out.append(child.name)
    return out


def l0_partition_dir(data_root: Path, snapshot_id: str, table: str, partition_key: str, partition_value: str) -> Path:
    """Return directory for one Hive-style partition (e.g. gaming_day=2026-04-01)."""
    if not isinstance(table, str) or not table.strip():
        raise ValueError(f"table must be a non-empty string, got {table!r}")
    if not isinstance(partition_key, str) or not partition_key.strip():
        raise ValueError(f"partition_key must be a non-empty string, got {partition_key!r}")
    if not isinstance(partition_value, str) or not partition_value.strip():
        raise ValueError(f"partition_value must be a non-empty string, got {partition_value!r}")
    if ".." in partition_key or "=" in partition_key:
        raise ValueError(f"invalid partition_key: {partition_key!r}")
    if (
        ".." in partition_value
        or "/" in partition_value
        or "\\" in partition_value
        or "=" in partition_value
    ):
        raise ValueError(
            f"partition_value must not contain '=', path separators, or '..', got {partition_value!r}"
        )
    root = l0_snapshot_root(data_root, snapshot_id)
    return root / table.strip() / f"{partition_key.strip()}={partition_value.strip()}"
