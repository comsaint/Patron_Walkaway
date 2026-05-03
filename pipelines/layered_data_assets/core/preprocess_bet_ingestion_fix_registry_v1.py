"""Load ``preprocess_bet_ingestion_fix_registry.yaml`` for LDA-E1-11 (synthetic observed-at cap)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_preprocess_bet_ingestion_fix_registry(path: Path) -> dict[str, Any]:
    """Parse the ingestion-fix registry YAML into a dict.

    Args:
        path: Path to ``preprocess_bet_ingestion_fix_registry.yaml``.

    Raises:
        FileNotFoundError: If ``path`` is not a file.
        ValueError: If YAML is not a mapping.
    """
    if not path.is_file():
        raise FileNotFoundError(f"ingestion fix registry not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"ingestion fix registry root must be a mapping, got {type(raw).__name__}")
    return raw


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


def _active_rule_004(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Return the active rule dict for ``BET-INGEST-FIX-004``, or ``None``."""
    rules = doc.get("active_rules")
    if not isinstance(rules, list):
        return None
    for item in rules:
        if isinstance(item, dict) and item.get("fix_rule_id") == "BET-INGEST-FIX-004":
            return item
    return None


def resolve_bet_ingest_fix004_cap_binding(doc: dict[str, Any]) -> tuple[int, str, str, list[str]]:
    """Resolve P95-cap seconds and manifest binding for enabled ``BET-INGEST-FIX-004``.

    Args:
        doc: Parsed registry root mapping.

    Returns:
        Tuple ``(cap_sec, fix_rule_id, fix_rule_version, applied_fix_rules)``.

    Raises:
        ValueError: If contract / active rule / caps are inconsistent or FIX-004 is disabled.
    """
    contract_cap = _contract_ingest_delay_cap_sec(doc)
    if contract_cap is None:
        raise ValueError("registry missing bulk_historical_ingest_episodes.synthetic_observed_at_contract.ingest_delay_cap_sec")
    rule = _active_rule_004(doc)
    if rule is None:
        raise ValueError("registry active_rules must include fix_rule_id BET-INGEST-FIX-004")
    if rule.get("enabled") is not True:
        raise ValueError("BET-INGEST-FIX-004 must be enabled when preprocess loads this registry")
    action = rule.get("action")
    if not isinstance(action, dict) or action.get("type") != "normalize_observed_at":
        raise ValueError("BET-INGEST-FIX-004 action.type must be normalize_observed_at")
    params = action.get("params")
    if not isinstance(params, dict):
        raise ValueError("BET-INGEST-FIX-004 action.params must be a mapping")
    rule_cap = params.get("cap_delay_sec")
    if not isinstance(rule_cap, int) or isinstance(rule_cap, bool):
        raise ValueError(f"BET-INGEST-FIX-004 cap_delay_sec must be int, got {rule_cap!r}")
    if int(rule_cap) != int(contract_cap):
        raise ValueError(
            "ingest_delay_cap_sec mismatch between synthetic_observed_at_contract "
            f"({contract_cap}) and BET-INGEST-FIX-004.cap_delay_sec ({rule_cap})"
        )
    cap = int(contract_cap)
    if cap < 0 or cap > 86_400 * 366:
        raise ValueError(f"ingest_delay_cap_sec out of supported range [0, 366d], got {cap}")
    fix_rule_id = str(rule.get("fix_rule_id") or "BET-INGEST-FIX-004")
    fix_rule_version = str(rule.get("fix_rule_version") or "v1")
    applied = [f"{fix_rule_id}:{fix_rule_version}"]
    return cap, fix_rule_id, fix_rule_version, applied
