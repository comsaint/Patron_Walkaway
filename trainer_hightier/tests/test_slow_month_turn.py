"""Unit tests for slow month-turn contract helpers."""

from __future__ import annotations

from datetime import date

from trainer_hightier.utils.slow_month_turn import (
    resolve_slow_month_turn_context,
    resolve_slow_month_turn_phase,
    slow_anchors_for_phase,
)


def test_post_gap_target_equals_april_for_may_context() -> None:
    ctx = resolve_slow_month_turn_context(date(2026, 5, 22))
    assert ctx.phase == "post_gap"
    assert ctx.slow_anchor_target == date(2026, 4, 30)
    assert ctx.slow_anchor_effective == date(2026, 4, 30)
    assert ctx.slow_anchor_required == date(2026, 4, 30)


def test_gap_day_uses_prior_effective_anchor() -> None:
    epochs = [date(2026, 5, 1), date(2026, 5, 2)]
    phase = resolve_slow_month_turn_phase(date(2026, 5, 1), month_epochs=epochs)
    assert phase == "gap"
    target, effective = slow_anchors_for_phase(date(2026, 5, 1), phase)
    assert target == date(2026, 4, 30)
    assert effective == date(2026, 3, 31)


def test_evaluate_slow_freshness_post_gap_requires_target() -> None:
    from trainer_hightier.serving.snapshot_freshness import evaluate_slow_freshness

    fresh = evaluate_slow_freshness(
        anchor_max=date(2026, 4, 30),
        serving_day=date(2026, 5, 19),
    )
    assert fresh.status == "fresh"

    bad = evaluate_slow_freshness(
        anchor_max=date(2026, 3, 31),
        serving_day=date(2026, 5, 19),
    )
    assert bad.status == "hard_cap_breached"
