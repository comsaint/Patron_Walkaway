"""Load ``tables.t_session`` from consolidated ``preprocess_ingestion_fix_registry.yaml``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipelines.layered_data_assets.core.preprocess_ingestion_fix_registry_v1 import (
    load_preprocess_ingestion_fix_registry,
    table_ingestion_section,
)


def load_preprocess_session_ingestion_fix_registry(path: Path) -> dict[str, Any]:
    """Parse registry YAML and return the ``t_session`` table section (legacy flat shape).

    Args:
        path: Path to ``preprocess_ingestion_fix_registry.yaml``.

    Raises:
        FileNotFoundError: If ``path`` is not a file.
        ValueError: If YAML is invalid or missing ``tables.t_session``.
    """
    root = load_preprocess_ingestion_fix_registry(path.resolve())
    return table_ingestion_section(root, "t_session")


def _contract_ingest_delay_cap_sec(doc: dict[str, Any]) -> int | None:
    """Return ``ingest_delay_cap_sec`` from bulk synthetic contract, or ``None`` if absent."""
    bulk = doc.get("bulk_historical_ingest_episodes")
    if not isinstance(bulk, dict):
        return None
    contract = bulk.get("synthetic_observed_at_contract")
    if not isinstance(contract, dict):
        return None
    cap = contract.get("ingest_delay_cap_sec")
    if cap is None:
        return None
    if not isinstance(cap, int) or isinstance(cap, bool):
        raise ValueError(
            "bulk_historical_ingest_episodes.synthetic_observed_at_contract.ingest_delay_cap_sec "
            f"must be int, got {cap!r}"
        )
    return int(cap)


def _active_rule_session_ingest_fix001(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Return the active rule dict for ``SESSION-INGEST-FIX-001``, or ``None``."""
    rules = doc.get("active_rules")
    if not isinstance(rules, list):
        return None
    for item in rules:
        if isinstance(item, dict) and item.get("fix_rule_id") == "SESSION-INGEST-FIX-001":
            return item
    return None


def resolve_session_ingest_fix001_cap_binding(doc: dict[str, Any]) -> tuple[int, str, str, list[str]]:
    """Resolve P95-cap seconds and manifest binding for enabled ``SESSION-INGEST-FIX-001``.

    Args:
        doc: Parsed registry root mapping.

    Returns:
        Tuple ``(cap_sec, fix_rule_id, fix_rule_version, applied_fix_rules)``.

    Raises:
        ValueError: If contract / active rule / caps are inconsistent or FIX-001 is disabled.
    """
    contract_cap = _contract_ingest_delay_cap_sec(doc)
    if contract_cap is None:
        raise ValueError(
            "registry missing bulk_historical_ingest_episodes.synthetic_observed_at_contract.ingest_delay_cap_sec"
        )
    rule = _active_rule_session_ingest_fix001(doc)
    if rule is None:
        raise ValueError("registry active_rules must include fix_rule_id SESSION-INGEST-FIX-001")
    if rule.get("enabled") is not True:
        raise ValueError("SESSION-INGEST-FIX-001 must be enabled when preprocess loads this registry")
    action = rule.get("action")
    if not isinstance(action, dict) or action.get("type") != "normalize_observed_at":
        raise ValueError("SESSION-INGEST-FIX-001 action.type must be normalize_observed_at")
    params = action.get("params")
    if not isinstance(params, dict):
        raise ValueError("SESSION-INGEST-FIX-001 action.params must be a mapping")
    rule_cap = params.get("cap_delay_sec")
    if not isinstance(rule_cap, int) or isinstance(rule_cap, bool):
        raise ValueError(f"SESSION-INGEST-FIX-001 cap_delay_sec must be int, got {rule_cap!r}")
    if int(rule_cap) != int(contract_cap):
        raise ValueError(
            "ingest_delay_cap_sec mismatch between synthetic_observed_at_contract "
            f"({contract_cap}) and SESSION-INGEST-FIX-001.cap_delay_sec ({rule_cap})"
        )
    cap = int(contract_cap)
    if cap < 0 or cap > 86_400 * 366:
        raise ValueError(f"ingest_delay_cap_sec out of supported range [0, 366d], got {cap}")
    fix_rule_id = str(rule.get("fix_rule_id") or "SESSION-INGEST-FIX-001")
    fix_rule_version = str(rule.get("fix_rule_version") or "v1")
    applied = [f"{fix_rule_id}:{fix_rule_version}"]
    return cap, fix_rule_id, fix_rule_version, applied
