"""Load ``tables.t_session`` from consolidated ``preprocess_l0_data_contract_registry.yaml``."""
from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from pipelines.layered_data_assets.core.preprocess_ingestion_fix_registry_v1 import (
    load_preprocess_ingestion_fix_registry,
    table_ingestion_section,
)


class SessionMaterializationContract(NamedTuple):
    """Resolved ``session_for_mapping`` DuckDB materialization contract (t_session only)."""

    clean_logic_version: str
    required_l0_columns: tuple[str, ...]
    episode_calendar_tags: tuple[tuple[str, str], ...]
    correction_pairing_enabled: bool
    correction_winner_order_sql: str


def load_preprocess_session_ingestion_fix_registry(path: Path) -> dict[str, Any]:
    """Parse registry YAML and return the ``t_session`` table section (legacy flat shape).

    Args:
        path: Path to ``preprocess_l0_data_contract_registry.yaml``.

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


def _episode_calendar_tags_from_bulk(doc: dict[str, Any], *, tagging_enabled: bool) -> list[tuple[str, str]]:
    """Return ``(YYYY-MM-DD, episode_id)`` pairs from bulk episodes when tagging is enabled."""
    if not tagging_enabled:
        return []
    bulk = doc.get("bulk_historical_ingest_episodes")
    if not isinstance(bulk, dict):
        raise ValueError("ingestion_episode_tagging enabled but bulk_historical_ingest_episodes missing")
    episodes = bulk.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("ingestion_episode_tagging enabled but bulk episodes list is empty")
    out: list[tuple[str, str]] = []
    for ep in episodes:
        if not isinstance(ep, dict):
            raise ValueError("bulk episode entry must be a mapping")
        day = ep.get("observed_at_calendar_day")
        eid = ep.get("episode_id")
        if not isinstance(day, str) or not day.strip():
            raise ValueError(
                f"bulk episode {eid!r} missing observed_at_calendar_day (required for session_for_mapping tagging)"
            )
        if not isinstance(eid, str) or not eid.strip():
            raise ValueError(f"bulk episode missing episode_id on entry with day={day!r}")
        out.append((day.strip(), eid.strip()))
    return out


def _sql_fragment_for_correction_winner_col(col: str, direction: str, nulls: str | None) -> str:
    """Map registry winner column to DuckDB ORDER BY fragment (``e.*`` alias context)."""
    d = str(direction).upper()
    if d not in ("ASC", "DESC"):
        raise ValueError(f"winner_order.order must be asc or desc, got {direction!r}")
    nulls_clause = ""
    if nulls == "last":
        nulls_clause = " NULLS LAST"
    elif nulls == "first":
        nulls_clause = " NULLS FIRST"
    elif nulls not in (None, ""):
        raise ValueError(f"winner_order.nulls must be last, first, or empty, got {nulls!r}")
    if col == "is_manual":
        expr = "CASE WHEN e.is_manual_i64 = 1 THEN 1 ELSE 0 END"
    elif col == "lud_dtm":
        expr = "e.lud_ts"
    elif col == "__etl_insert_Dtm":
        expr = "e.observed_raw_ts"
    elif col == "session_id":
        expr = "e.session_id"
    else:
        raise ValueError(f"unsupported correction winner col {col!r}")
    return f"{expr} {d}{nulls_clause}"


def _correction_winner_order_sql(winner_order: Any) -> str:
    """Build comma-separated ORDER BY clause body for correction_pairing.winner_order."""
    if not isinstance(winner_order, list) or not winner_order:
        raise ValueError("correction_pairing.winner_order must be a non-empty list when pairing is enabled")
    parts: list[str] = []
    for item in winner_order:
        if not isinstance(item, dict):
            raise ValueError("correction_pairing.winner_order entries must be mappings")
        col = item.get("col")
        direction = item.get("order", "desc")
        nulls = item.get("nulls")
        if not isinstance(col, str):
            raise ValueError(f"winner_order.col must be str, got {col!r}")
        parts.append(
            _sql_fragment_for_correction_winner_col(col, str(direction), str(nulls) if nulls is not None else None)
        )
    return ", ".join(parts)


def _validate_event_time_effective_block(block: Any) -> None:
    """Validate fixed event_time_effective policy agreed with product owner."""
    if not isinstance(block, dict):
        raise ValueError("session_for_mapping_materialization_contract.event_time_effective must be a mapping")
    if block.get("true_source_col") != "session_end_dtm":
        raise ValueError("event_time_effective.true_source_col must be session_end_dtm for v1 contract")
    chain = block.get("fallback_chain")
    expected = ["session_end_dtm", "session_start_dtm", "__etl_insert_Dtm_synthetic"]
    if chain != expected:
        raise ValueError(f"event_time_effective.fallback_chain must be {expected}, got {chain!r}")
    if block.get("clamp_to_observed_logical") is not True:
        raise ValueError("event_time_effective.clamp_to_observed_logical must be true for v1 contract")


def _resolve_correction_pairing(pair: Any) -> tuple[bool, str]:
    """Return ``(enabled, winner_order_sql)`` from ``correction_pairing`` block."""
    if not isinstance(pair, dict):
        raise ValueError("session_for_mapping_materialization_contract.correction_pairing must be a mapping")
    pairing_enabled = pair.get("enabled") is True
    pkeys = pair.get("partition_keys")
    expected_keys = ["player_id", "session_start_dtm"]
    if pairing_enabled:
        if pkeys != expected_keys:
            raise ValueError(f"correction_pairing.partition_keys must be {expected_keys}, got {pkeys!r}")
        return True, _correction_winner_order_sql(pair.get("winner_order"))
    if pkeys not in (None, expected_keys, []):
        raise ValueError("correction_pairing.partition_keys invalid when pairing disabled")
    return False, ""


def resolve_session_for_mapping_materialization_contract(doc: dict[str, Any]) -> SessionMaterializationContract:
    """Parse ``session_for_mapping_materialization_contract`` from a loaded ``t_session`` registry doc.

    Args:
        doc: Return value of ``load_preprocess_session_ingestion_fix_registry``.

    Raises:
        ValueError: If contract is missing or violates the supported v1 shape.
    """
    block = doc.get("session_for_mapping_materialization_contract")
    if not isinstance(block, dict):
        raise ValueError("registry tables.t_session missing session_for_mapping_materialization_contract")
    clean = block.get("clean_logic_version")
    if not isinstance(clean, str) or not clean.strip():
        raise ValueError("session_for_mapping_materialization_contract.clean_logic_version must be a non-empty string")
    cols = block.get("required_l0_columns")
    if not isinstance(cols, list) or not cols or not all(isinstance(c, str) for c in cols):
        raise ValueError("session_for_mapping_materialization_contract.required_l0_columns must be a non-empty str list")
    _validate_event_time_effective_block(block.get("event_time_effective"))
    pairing_enabled, winner_sql = _resolve_correction_pairing(block.get("correction_pairing"))
    tag_block = block.get("ingestion_episode_tagging")
    if not isinstance(tag_block, dict):
        raise ValueError("session_for_mapping_materialization_contract.ingestion_episode_tagging must be a mapping")
    tagging_enabled = tag_block.get("enabled") is True
    tags = _episode_calendar_tags_from_bulk(doc, tagging_enabled=tagging_enabled)
    return SessionMaterializationContract(
        clean_logic_version=clean.strip(),
        required_l0_columns=tuple(str(c) for c in cols),
        episode_calendar_tags=tuple(tags),
        correction_pairing_enabled=pairing_enabled,
        correction_winner_order_sql=winner_sql,
    )
