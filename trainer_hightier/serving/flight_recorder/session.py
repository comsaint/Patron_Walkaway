"""Process-wide active scorer flight recorder (single-threaded scorer loop)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trainer_hightier.serving.flight_recorder.scorer_hooks import ScorerCycleRecorder
    from trainer_hightier.serving.flight_recorder.validator_hooks import ValidatorCycleRecorder

_ACTIVE_SCORER_RECORDER: ScorerCycleRecorder | None = None
_ACTIVE_VALIDATOR_RECORDER: ValidatorCycleRecorder | None = None


def attach_scorer_recorder(recorder: ScorerCycleRecorder | None) -> None:
    """Set the active scorer cycle recorder (``None`` disables hooks)."""
    global _ACTIVE_SCORER_RECORDER
    _ACTIVE_SCORER_RECORDER = recorder


def get_active_scorer_recorder() -> ScorerCycleRecorder | None:
    """Return the active recorder when capture is enabled."""
    rec = _ACTIVE_SCORER_RECORDER
    if rec is None or not rec.enabled:
        return None
    return rec


def attach_validator_recorder(recorder: ValidatorCycleRecorder | None) -> None:
    """Set the active validator cycle recorder (``None`` disables hooks)."""
    global _ACTIVE_VALIDATOR_RECORDER
    _ACTIVE_VALIDATOR_RECORDER = recorder


def get_active_validator_recorder() -> ValidatorCycleRecorder | None:
    """Return the active validator recorder when capture is enabled."""
    rec = _ACTIVE_VALIDATOR_RECORDER
    if rec is None or not rec.enabled:
        return None
    return rec
