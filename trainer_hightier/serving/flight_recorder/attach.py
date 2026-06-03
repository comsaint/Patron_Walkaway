"""Attach flight recorders to deploy bundle runtime."""

from __future__ import annotations

from pathlib import Path

from trainer_hightier.serving.flight_recorder.config import (
    DEFAULT_CONFIG_REL,
    FlightRecorderConfig,
)
from trainer_hightier.serving.flight_recorder.context import RecorderContext
from trainer_hightier.serving.flight_recorder.init_recording import init_recording_root
from trainer_hightier.serving.flight_recorder.scorer_hooks import ScorerCycleRecorder
from trainer_hightier.serving.flight_recorder.session import (
    attach_scorer_recorder,
    attach_validator_recorder,
)
from trainer_hightier.serving.flight_recorder.validator_hooks import ValidatorCycleRecorder


def attach_production_flight_recorders(
    bundle_dir: Path,
    *,
    config_path: Path | None = None,
    export_sqlite: bool = True,
) -> RecorderContext:
    """Initialize recording root and attach scorer/validator shadow recorders."""
    bundle_dir = bundle_dir.resolve()
    cfg_path = config_path or (bundle_dir / DEFAULT_CONFIG_REL)
    if cfg_path.is_file():
        config = FlightRecorderConfig.from_yaml_path(cfg_path)
    else:
        config = FlightRecorderConfig()
    ctx = init_recording_root(
        bundle_dir,
        config,
        write_default_config=not cfg_path.is_file(),
        export_sqlite=export_sqlite,
    )
    if config.capture_scorer_stages:
        attach_scorer_recorder(ScorerCycleRecorder.from_recorder_context(ctx))
    if config.capture_validator_stages:
        attach_validator_recorder(ValidatorCycleRecorder.from_recorder_context(ctx))
    return ctx
