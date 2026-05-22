"""Slow monthly snapshot month-turn contract (gap / post-gap).

Shared by training materialization, serving readiness, and Step 06 verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

SlowMonthTurnPhase = Literal["gap", "post_gap"]


def previous_calendar_month_end(as_of_day: date) -> date:
    """Return the last calendar day of the month before ``as_of_day``."""
    first_this_month = date(as_of_day.year, as_of_day.month, 1)
    return first_this_month - timedelta(days=1)


def fallback_slow_calendar_anchor(target_anchor: date) -> date:
    """Prior published month-end before ``target_anchor``."""
    first_target_month = target_anchor.replace(day=1)
    return previous_calendar_month_end(first_target_month)


def slow_anchors_for_phase(as_of_day: date, phase: SlowMonthTurnPhase) -> tuple[date, date]:
    """Return ``(slow_anchor_target, slow_anchor_effective)`` for a calendar context day."""
    target = previous_calendar_month_end(as_of_day)
    effective = fallback_slow_calendar_anchor(target) if phase == "gap" else target
    return target, effective


def gaming_day_epochs_in_calendar_month(epochs: list[date], *, year: int, month: int) -> list[date]:
    """Sorted unique ``gaming_day`` dates in one calendar month."""
    return sorted({d for d in epochs if d.year == year and d.month == month})


def resolve_slow_month_turn_phase(
    context_day: date,
    *,
    month_epochs: list[date] | None = None,
) -> SlowMonthTurnPhase:
    """Resolve gap vs post-gap from a context ``gaming_day`` and optional month epochs.

    Gap = first distinct ``gaming_day`` epoch in the calendar month of ``context_day``.
    Post-gap = second epoch onward, or any day after the first epoch when only one epoch is known.
    """
    epochs = month_epochs or []
    in_month = gaming_day_epochs_in_calendar_month(
        epochs,
        year=context_day.year,
        month=context_day.month,
    )
    if not in_month:
        return "post_gap"
    first_epoch = in_month[0]
    if len(in_month) >= 2:
        second_epoch = in_month[1]
        if context_day <= first_epoch:
            return "gap"
        if context_day >= second_epoch:
            return "post_gap"
        return "post_gap"
    if context_day <= first_epoch:
        return "gap"
    return "post_gap"


def required_slow_anchor_for_phase(
    as_of_day: date,
    phase: SlowMonthTurnPhase,
) -> date:
    """Anchor that artifacts / readiness must expose for the given phase."""
    _target, effective = slow_anchors_for_phase(as_of_day, phase)
    target, _ = slow_anchors_for_phase(as_of_day, "post_gap")
    return effective if phase == "gap" else target


@dataclass(frozen=True)
class SlowMonthTurnContext:
    """Resolved slow month-turn metadata for one run or scoring cycle."""

    context_day: date
    phase: SlowMonthTurnPhase
    slow_anchor_target: date
    slow_anchor_effective: date
    slow_anchor_required: date

    def to_manifest_dict(self) -> dict[str, str]:
        """JSON-serializable manifest fragment."""
        return {
            "slow_month_turn_phase": self.phase,
            "slow_anchor_target": self.slow_anchor_target.isoformat(),
            "slow_anchor_effective": self.slow_anchor_effective.isoformat(),
            "slow_anchor_required": self.slow_anchor_required.isoformat(),
        }


def resolve_slow_month_turn_context(
    context_day: date,
    *,
    month_epochs: list[date] | None = None,
) -> SlowMonthTurnContext:
    """Build full month-turn context for ``context_day``."""
    phase = resolve_slow_month_turn_phase(context_day, month_epochs=month_epochs)
    target, effective = slow_anchors_for_phase(context_day, phase)
    required = effective if phase == "gap" else target
    return SlowMonthTurnContext(
        context_day=context_day,
        phase=phase,
        slow_anchor_target=target,
        slow_anchor_effective=effective,
        slow_anchor_required=required,
    )


def slow_month_turn_metadata(
    context_day: date,
    *,
    month_epochs: list[date] | None = None,
) -> dict[str, Any]:
    """Dict for run reports / active manifest (includes resolution hints)."""
    ctx = resolve_slow_month_turn_context(context_day, month_epochs=month_epochs)
    out = ctx.to_manifest_dict()
    out["slow_month_turn_context_day"] = context_day.isoformat()
    if month_epochs is not None:
        in_month = gaming_day_epochs_in_calendar_month(
            month_epochs,
            year=context_day.year,
            month=context_day.month,
        )
        out["gaming_day_epochs_in_context_month"] = [d.isoformat() for d in in_month[:10]]
    return out
