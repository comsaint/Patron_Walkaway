"""L2 miss-path orchestration helpers (hard-gate B): impacted plan → execution contract.

Pure helpers for partition id extraction and strict-mode checks used by
``process_chunk`` / ``pipeline_run_core``.
"""

from __future__ import annotations

from typing import Any, FrozenSet, Mapping, Optional


def impacted_partition_ids_from_plan(plan: Optional[Mapping[str, Any]]) -> FrozenSet[str]:
    """Return distinct concrete ``partition_id`` values from *plan* (excludes ``*``)."""
    if not isinstance(plan, dict):
        return frozenset()
    rows = plan.get("impacted_work_units")
    if not isinstance(rows, list):
        return frozenset()
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("partition_id") or "").strip()
        if pid and pid != "*":
            out.add(pid)
    return frozenset(out)


def enforce_impacted_only_forbids_chunk_cache_miss(
    *,
    impact_orchestrator_mode: str,
    orchestrator_execution_mode: str,
    allow_chunk_full_fallback: bool,
) -> bool:
    """Return True when a chunk final-cache miss must not proceed to full recompute."""
    im = str(impact_orchestrator_mode or "off").strip().lower()
    ex = str(orchestrator_execution_mode or "off").strip().lower()
    return im == "enforce" and ex == "impacted_only" and not bool(allow_chunk_full_fallback)


def raise_if_impacted_only_chunk_miss_forbidden(
    *,
    impact_orchestrator_mode: str,
    orchestrator_execution_mode: str,
    allow_chunk_full_fallback: bool,
    chunk_label: str,
    miss_reasons: Optional[list],
) -> None:
    """Raise when strict impacted-only mode forbids falling through to full chunk recompute."""
    if not enforce_impacted_only_forbids_chunk_cache_miss(
        impact_orchestrator_mode=impact_orchestrator_mode,
        orchestrator_execution_mode=orchestrator_execution_mode,
        allow_chunk_full_fallback=allow_chunk_full_fallback,
    ):
        return
    raise RuntimeError(
        "L2 hard gate B: L2_IMPACT_ORCHESTRATOR_MODE=enforce with impacted_only execution "
        "forbids chunk final-cache miss → full recompute while L2_IMPACT_ALLOW_CHUNK_FULL_FALLBACK "
        f"is false. chunk={chunk_label!r} miss_reasons={miss_reasons!r}. "
        "Set L2_IMPACT_ALLOW_CHUNK_FULL_FALLBACK=1 (YAML/PY config), switch orchestrator to observe/off, "
        "or use full_matrix when the planner recommends it."
    )
