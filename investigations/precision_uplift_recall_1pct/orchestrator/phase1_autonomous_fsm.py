"""Phase 1 autonomous supervisor state machine (T8A skeleton).

MVP backbone (``PRECISION_UPLIFT_R1PCT_MVP_TASKLIST.md`` T8A)::

    init -> observe -> mid_snapshot -> final_snapshot -> collect -> report

The ``observe`` step is expected to self-loop while waiting on wall-clock or DB
maturity; ``(observe, observe)`` is therefore an allowed edge for future ticks.

Checkpoint (T8A): each stub tick writes ``tick_seq`` and a ``checkpoint`` object
(cursor before/after, tick time, optional ``config_fingerprint``). Chaining
without ``--resume`` uses disk merge in ``run_pipeline``; with ``--resume``,
``_merge_state`` copies prior ``run_state`` so the cursor survives process
restart when the fingerprint still matches.
"""

from __future__ import annotations

from typing import Any, Final, Mapping

STEP_INIT = "init"
STEP_OBSERVE = "observe"
STEP_MID_SNAPSHOT = "mid_snapshot"
STEP_FINAL_SNAPSHOT = "final_snapshot"
STEP_COLLECT = "collect"
STEP_REPORT = "report"

ORDERED_STEPS: Final[tuple[str, ...]] = (
    STEP_INIT,
    STEP_OBSERVE,
    STEP_MID_SNAPSHOT,
    STEP_FINAL_SNAPSHOT,
    STEP_COLLECT,
    STEP_REPORT,
)

_STEP_INDEX: Final[dict[str, int]] = {s: i for i, s in enumerate(ORDERED_STEPS)}

ALLOWED_TRANSITIONS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        (STEP_INIT, STEP_OBSERVE),
        (STEP_OBSERVE, STEP_OBSERVE),
        (STEP_OBSERVE, STEP_MID_SNAPSHOT),
        (STEP_MID_SNAPSHOT, STEP_FINAL_SNAPSHOT),
        (STEP_FINAL_SNAPSHOT, STEP_COLLECT),
        (STEP_COLLECT, STEP_REPORT),
    }
)


def is_valid_step(step: str) -> bool:
    """Return True if ``step`` is a known FSM step id."""

    return step in _STEP_INDEX


def successor(step: str) -> str | None:
    """Return the next linear step after ``step``, or None if ``step`` is terminal."""

    if step not in _STEP_INDEX:
        return None
    i = _STEP_INDEX[step]
    if i + 1 >= len(ORDERED_STEPS):
        return None
    return ORDERED_STEPS[i + 1]


def can_transition(frm: str, to: str) -> bool:
    """Return True if a transition from ``frm`` to ``to`` is allowed."""

    return (frm, to) in ALLOWED_TRANSITIONS


def restore_cursor(prev: Mapping[str, Any] | None, *, resume: bool) -> str:
    """Return persisted FSM cursor from ``prev`` run_state, else ``init``."""

    if not resume or prev is None:
        return STEP_INIT
    return read_autonomous_cursor(prev)


def read_autonomous_cursor(prev: Mapping[str, Any] | None) -> str:
    """Read ``current_step`` from ``prev['phase1_autonomous']``; default ``init``."""

    if prev is None:
        return STEP_INIT
    block = prev.get("phase1_autonomous")
    if not isinstance(block, Mapping):
        return STEP_INIT
    cur = str(block.get("current_step") or "").strip()
    if not cur:
        return STEP_INIT
    return cur if is_valid_step(cur) else STEP_INIT


def after_stub_tick(
    prev_state: Mapping[str, Any] | None,
    *,
    tick_iso: str,
    config_fingerprint: str | None = None,
    observe_context: Mapping[str, Any] | None = None,
    advance_mid_when_eligible: bool = False,
) -> dict[str, Any]:
    """Apply one supervisor stub tick and return new ``phase1_autonomous`` dict.

    Deterministic MVP stub:

    - ``init`` -> ``observe`` (first tick enters observation phase).
    - ``observe`` -> ``observe`` with ``stub_observe_ticks`` incremented (default).
    - When ``advance_mid_when_eligible`` is True and ``observe_context`` reports
      ``mid_snapshot_eligible``: ``observe`` -> ``mid_snapshot`` without incrementing
      ``stub_observe_ticks``.
    - Any other step: no-op (cursor unchanged); still records ``last_stub_tick_at``.

    Each call bumps monotonic ``tick_seq`` and overwrites ``checkpoint`` (T8A).

    Args:
        prev_state: Latest ``run_state`` mapping (or None).
        tick_iso: UTC ISO-8601 timestamp string for auditing.
        config_fingerprint: Optional Phase-1 config fingerprint for audit trail.
        observe_context: Optional gate/db snapshot (e.g. from run_pipeline) for advance.
        advance_mid_when_eligible: Opt-in observe->mid transition when eligible.

    Returns:
        Full ``phase1_autonomous`` object to assign into ``run_state``.
    """
    prev_block = prev_state.get("phase1_autonomous") if isinstance(prev_state, Mapping) else None
    prev_block = prev_block if isinstance(prev_block, Mapping) else None
    cursor = read_autonomous_cursor(prev_state)
    out: dict[str, Any] = dict(prev_block) if prev_block else run_state_block(cursor)
    out.setdefault("fsm_schema_version", 1)
    out.setdefault("backbone", list(ORDERED_STEPS))
    out.setdefault("observe_self_loop", True)
    tick_seq = int(out.get("tick_seq") or 0) + 1
    out["tick_seq"] = tick_seq
    out["last_stub_tick_at"] = tick_iso
    if cursor == STEP_INIT:
        out["current_step"] = STEP_OBSERVE
        out["stub_last_note"] = "stub_tick: init -> observe"
    elif cursor == STEP_OBSERVE:
        oc = observe_context if isinstance(observe_context, Mapping) else None
        if (
            advance_mid_when_eligible
            and oc is not None
            and bool(oc.get("mid_snapshot_eligible"))
        ):
            out["current_step"] = STEP_MID_SNAPSHOT
            out["stub_last_note"] = "stub_tick: observe -> mid_snapshot (eligible)"
        else:
            n = int(out.get("stub_observe_ticks") or 0) + 1
            out["stub_observe_ticks"] = n
            out["current_step"] = STEP_OBSERVE
            out["stub_last_note"] = "stub_tick: observe self-loop"
    else:
        out["current_step"] = cursor
        out["stub_last_note"] = f"stub_tick: no-op (cursor={cursor!r})"
    cursor_after = str(out.get("current_step") or "")
    ck: dict[str, Any] = {
        "schema_version": 1,
        "tick_seq": tick_seq,
        "cursor_before": cursor,
        "cursor_after": cursor_after,
        "tick_at": tick_iso,
    }
    if isinstance(config_fingerprint, str) and config_fingerprint.strip():
        ck["config_fingerprint"] = config_fingerprint.strip()
    out["checkpoint"] = ck
    return out


def run_state_block(current_step: str) -> dict[str, Any]:
    """Build ``run_state['phase1_autonomous']`` payload (JSON-serializable)."""

    return {
        "fsm_schema_version": 1,
        "backbone": list(ORDERED_STEPS),
        "current_step": current_step,
        "observe_self_loop": True,
    }
