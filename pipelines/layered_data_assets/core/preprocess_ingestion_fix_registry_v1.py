"""Load consolidated ``preprocess_l0_data_contract_registry.yaml`` (multi-table)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_preprocess_ingestion_fix_registry(path: Path) -> dict[str, Any]:
    """Parse the top-level L0 preprocess data contract registry YAML.

    Args:
        path: Path to ``preprocess_l0_data_contract_registry.yaml``.

    Raises:
        FileNotFoundError: If ``path`` is not a file.
        ValueError: If YAML is not a mapping or missing ``tables``.
    """
    if not path.is_file():
        raise FileNotFoundError(f"L0 data contract registry not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"L0 data contract registry root must be a mapping, got {type(raw).__name__}")
    tables = raw.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("L0 data contract registry root must contain a top-level tables: mapping")
    return raw


def table_ingestion_section(root: dict[str, Any], table: str) -> dict[str, Any]:
    """Return one table's registry document plus root ``registry_id`` / ``registry_version``.

    Callers such as ``resolve_bet_ingest_fix004_cap_binding`` expect a flat mapping shaped like
    the legacy single-table YAML (``bulk_historical_ingest_episodes``, ``active_rules``, …).

    Args:
        root: Parsed root from :func:`load_preprocess_ingestion_fix_registry`.
        table: Logical table key, e.g. ``\"t_bet\"`` or ``\"t_session\"``.

    Raises:
        ValueError: If ``tables[table]`` is missing or not a mapping.
    """
    tables = root.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("root missing tables mapping")
    sec = tables.get(table)
    if not isinstance(sec, dict):
        raise ValueError(f"tables.{table!r} missing or not a mapping (known keys: {sorted(tables)!r})")
    out: dict[str, Any] = dict(sec)
    rid = root.get("registry_id")
    if rid is not None:
        out["registry_id"] = rid
    rv = root.get("registry_version")
    if rv is not None:
        out["registry_version"] = rv
    return out
