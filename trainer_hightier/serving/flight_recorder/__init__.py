"""Production flight recorder foundations (config, manifest, identity, redaction)."""

from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.context import RecorderContext
from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot
from trainer_hightier.serving.flight_recorder.attach import attach_production_flight_recorders
from trainer_hightier.serving.flight_recorder.scorer_hooks import ScorerCycleRecorder
from trainer_hightier.serving.flight_recorder.session import (
    attach_scorer_recorder,
    attach_validator_recorder,
)
from trainer_hightier.serving.flight_recorder.validator_hooks import ValidatorCycleRecorder

__all__ = [
    "FlightRecorderConfig",
    "RecorderContext",
    "RecordingRoot",
    "ScorerCycleRecorder",
    "ValidatorCycleRecorder",
    "attach_production_flight_recorders",
    "attach_scorer_recorder",
    "attach_validator_recorder",
]
