"""Bet ingestion FIX-004 cap binding parsed from bundled preprocess L0 registry YAML.

Vendored from ``pipelines.layered_data_assets.core.preprocess_*_fix_registry_v1``
for a self-contained ``trainer_hightier`` package (no ``pipelines.*`` imports).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from trainer_hightier.config import TRAINER_HIGHTIER_PACKAGE_DIR


def bundled_preprocess_registry_yaml_path() -> Path:
    """Return ``trainer_hightier/contracts/preprocess_l0_data_contract_registry.yaml``."""

    p = TRAINER_HIGHTIER_PACKAGE_DIR / "contracts" / "preprocess_l0_data_contract_registry.yaml"
    if not p.is_file():
        raise FileNotFoundError(
            "Bundled preprocess L0 registry YAML missing at "
            f"{p} (wheel/build must ship this file)."
        )
    return p


def load_preprocess_ingestion_fix_registry(path: Path) -> dict[str, Any]:
    """Parse top-level ``preprocess_l0_data_contract_registry`` YAML.

    Parameters
    ----------
    path
        Path to registry YAML.

    Returns
    -------
    dict[str, Any]
        Parsed root mapping including ``tables``.

    Raises
    ------
    FileNotFoundError
        If ``path`` is not a file.
    ValueError
        If YAML root is not a mapping or lacks ``tables``.
    """

    rp = Path(path).resolve()
    if not rp.is_file():
        raise FileNotFoundError(f"L0 data contract registry not found: {rp}")
    raw = yaml.safe_load(rp.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"L0 data contract registry root must be a mapping, got {type(raw).__name__}")
    tables = raw.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("L0 data contract registry root must contain a top-level tables: mapping")
    return raw


def table_ingestion_section(root: dict[str, Any], table: str) -> dict[str, Any]:
    """Return one ``tables.<table>`` section merged with root ``registry_id`` / ``registry_version``."""

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


def load_preprocess_bet_ingestion_fix_registry(path: Path) -> dict[str, Any]:
    """Parse registry YAML and return the ``t_bet`` table document (legacy flat shape)."""

    root = load_preprocess_ingestion_fix_registry(Path(path).resolve())
    return table_ingestion_section(root, "t_bet")


def _contract_ingest_delay_cap_sec(doc: dict[str, Any]) -> int | None:
    """Return ``ingest_delay_cap_sec`` from bulk synthetic contract, or ``None``."""

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
    """Return active rule dict for ``BET-INGEST-FIX-004``, or ``None``."""

    rules = doc.get("active_rules")
    if not isinstance(rules, list):
        return None
    for item in rules:
        if isinstance(item, dict) and item.get("fix_rule_id") == "BET-INGEST-FIX-004":
            return item
    return None


def resolve_bet_ingest_fix004_cap_binding(doc: dict[str, Any]) -> tuple[int, str, str, list[str]]:
    """Resolve P95-cap seconds and manifest binding for enabled ``BET-INGEST-FIX-004``."""

    contract_cap = _contract_ingest_delay_cap_sec(doc)
    if contract_cap is None:
        raise ValueError(
            "registry missing bulk_historical_ingest_episodes.synthetic_observed_at_contract.ingest_delay_cap_sec"
        )
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


def load_preprocess_txn_ingestion_fix_registry(path: Path) -> dict[str, Any]:
    """Parse registry YAML and return the ``gmwds_t_casino_txn`` table document."""

    root = load_preprocess_ingestion_fix_registry(Path(path).resolve())
    return table_ingestion_section(root, "gmwds_t_casino_txn")


def _active_txn_rule(doc: dict[str, Any], fix_rule_id: str) -> dict[str, Any] | None:
    """Return active rule dict for *fix_rule_id*, or ``None``."""

    rules = doc.get("active_rules")
    if not isinstance(rules, list):
        return None
    for item in rules:
        if isinstance(item, dict) and item.get("fix_rule_id") == fix_rule_id:
            return item
    return None


def resolve_txn_ingest_fix001_cap_binding(doc: dict[str, Any]) -> tuple[int, str, str, list[str]]:
    """Resolve P95-cap seconds and manifest binding for enabled ``TXN-INGEST-FIX-001``."""

    contract_cap = _contract_ingest_delay_cap_sec(doc)
    if contract_cap is None:
        raise ValueError(
            "registry missing bulk_historical_ingest_episodes.synthetic_observed_at_contract.ingest_delay_cap_sec"
        )
    rule = _active_txn_rule(doc, "TXN-INGEST-FIX-001")
    if rule is None:
        raise ValueError("registry active_rules must include fix_rule_id TXN-INGEST-FIX-001")
    if rule.get("enabled") is not True:
        raise ValueError("TXN-INGEST-FIX-001 must be enabled when txn L0 preprocess loads this registry")
    action = rule.get("action")
    if not isinstance(action, dict) or action.get("type") != "normalize_observed_at":
        raise ValueError("TXN-INGEST-FIX-001 action.type must be normalize_observed_at")
    params = action.get("params")
    if not isinstance(params, dict):
        raise ValueError("TXN-INGEST-FIX-001 action.params must be a mapping")
    rule_cap = params.get("cap_delay_sec")
    if not isinstance(rule_cap, int) or isinstance(rule_cap, bool):
        raise ValueError(f"TXN-INGEST-FIX-001 cap_delay_sec must be int, got {rule_cap!r}")
    if int(rule_cap) != int(contract_cap):
        raise ValueError(
            "ingest_delay_cap_sec mismatch between synthetic_observed_at_contract "
            f"({contract_cap}) and TXN-INGEST-FIX-001.cap_delay_sec ({rule_cap})"
        )
    cap = int(contract_cap)
    if cap < 0 or cap > 86_400 * 366:
        raise ValueError(f"ingest_delay_cap_sec out of supported range [0, 366d], got {cap}")
    fix_rule_id = str(rule.get("fix_rule_id") or "TXN-INGEST-FIX-001")
    fix_rule_version = str(rule.get("fix_rule_version") or "v1")
    applied = [f"{fix_rule_id}:{fix_rule_version}"]
    return cap, fix_rule_id, fix_rule_version, applied


def txn_bulk_episode_match_sqls(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(episode_id, match_rule_sql)`` pairs from registry bulk episodes."""

    bulk = doc.get("bulk_historical_ingest_episodes")
    if not isinstance(bulk, dict):
        return []
    episodes = bulk.get("episodes")
    if not isinstance(episodes, list):
        return []
    out: list[tuple[str, str]] = []
    for ep in episodes:
        if not isinstance(ep, dict):
            continue
        eid = ep.get("episode_id")
        sql = ep.get("match_rule_sql")
        if isinstance(eid, str) and isinstance(sql, str) and sql.strip():
            out.append((eid, sql.strip()))
    return out


def duckdb_txn_episode_coverage_sql(episode_match_sqls: list[tuple[str, str]]) -> str:
    """Build SQL predicate: row is covered by at least one registered bulk episode."""

    if not episode_match_sqls:
        return "FALSE"
    parts = [f"({sql})" for _, sql in episode_match_sqls]
    return " OR ".join(parts)
