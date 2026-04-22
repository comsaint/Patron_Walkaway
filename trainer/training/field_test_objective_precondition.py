"""W1 field-test objective precondition helpers (trainer-side).

Reads ``field_test_objective_precondition_check.json`` produced by
``trainer.scripts.build_field_test_objective_precondition`` and surfaces a
compact overlay into ``training_metrics.json`` without loading large blobs.

Environment variable (optional):
    FIELD_TEST_OBJECTIVE_PRECONDITION_JSON — absolute or relative path to JSON.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger("trainer")

FIELD_TEST_OBJECTIVE_PRECONDITION_JSON_ENV = "FIELD_TEST_OBJECTIVE_PRECONDITION_JSON"


def try_load_precondition_json(path: Path) -> Optional[dict[str, Any]]:
    """Load precondition JSON; return ``None`` if missing, unreadable, or not an object."""
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Field-test precondition JSON unreadable (%s): %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Field-test precondition JSON must be an object: %s", path)
        return None
    return data


def training_metrics_overlay_from_precondition(
    doc: Mapping[str, Any],
    *,
    source_path: str,
    max_blocking_list: int = 12,
) -> dict[str, Any]:
    """Return flat keys to merge into rated ``training_metrics`` (keep JSON small)."""
    blocking = doc.get("blocking_reasons")
    if not isinstance(blocking, list):
        blocking = []
    decision = doc.get("objective_decision")
    if not isinstance(decision, str):
        decision = "unknown"
    single_allowed = doc.get("single_objective_allowed")
    if not isinstance(single_allowed, bool):
        single_allowed = len(blocking) == 0

    head = blocking[: int(max_blocking_list)]
    return {
        "field_test_objective_precondition_json": source_path,
        "field_test_objective_decision": decision,
        "field_test_single_objective_allowed": single_allowed,
        "field_test_precondition_blocking_reason_count": len(blocking),
        "field_test_precondition_blocking_reasons_head": "; ".join(str(x) for x in head),
        # Explicit contract for W2: constrained field-test objective must not run when False.
        "field_test_constrained_optuna_objective_allowed": single_allowed,
    }


def log_precondition_block_warning(doc: Mapping[str, Any]) -> None:
    """Emit a single WARNING when ``blocking_reasons`` is non-empty."""
    blocking = doc.get("blocking_reasons")
    if not isinstance(blocking, list) or not blocking:
        return
    logger.warning(
        "Field-test objective precondition: %d blocking reason(s); "
        "single constrained objective is not allowed for this run.",
        len(blocking),
    )


def log_optuna_precondition_context(doc: Optional[Mapping[str, Any]]) -> None:
    """Log once before Optuna when a precondition doc is loaded (AP objective unchanged)."""
    if doc is None:
        return
    blocking = doc.get("blocking_reasons")
    if not isinstance(blocking, list):
        blocking = []
    if blocking:
        logger.info(
            "Optuna HPO: still optimising validation AP (baseline). "
            "Precondition blocks single constrained field-test objective (%d reason(s)).",
            len(blocking),
        )
    else:
        logger.info(
            "Optuna HPO: precondition reports no blockers; "
            "single constrained field-test objective remains subject to W2 wiring."
        )
