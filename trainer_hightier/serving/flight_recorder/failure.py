"""Flight recorder failure policy (fail-fast production vs debug fail-open)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig

logger = logging.getLogger(__name__)

EVIDENCE_GRADE_PRODUCTION: str = "production"
EVIDENCE_GRADE_DEBUG_ONLY: str = "debug_only"


class FlightRecorderFatalError(RuntimeError):
    """Required recorder step failed while ``fail_fast`` is enabled."""


def evidence_grade_for_config(config: FlightRecorderConfig) -> str:
    """Return manifest evidence grade for *config*."""
    return str(config.evidence_grade)


def cycle_policy_fields(config: FlightRecorderConfig) -> dict[str, bool | str]:
    """Fields to embed in per-cycle ``cycle_manifest.json``."""
    return {
        "fail_fast": bool(config.fail_fast),
        "evidence_grade": evidence_grade_for_config(config),
    }


def validate_recording_root_writable(recording_root: Path, *, fail_fast: bool) -> None:
    """Fail fast when the recording root is not writable."""
    if not fail_fast:
        return
    root = recording_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".flight_recorder_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        raise FlightRecorderFatalError(
            f"recording root not writable: {root} ({type(exc).__name__}: {exc})",
        ) from exc


def handle_recorder_failure(
    *,
    role: str,
    step: str,
    exc: BaseException,
    config: FlightRecorderConfig,
    mark_partial: Callable[[], None],
    artifact_path: Path | None = None,
) -> None:
    """Apply fail-fast or fail-open policy for one recorder hook failure."""
    path_hint = f" path={artifact_path}" if artifact_path is not None else ""
    if config.fail_fast:
        logger.error(
            "[flight_recorder] %s hook %s failed (fail_fast=true)%s: %s: %s",
            role,
            step,
            path_hint,
            type(exc).__name__,
            exc,
        )
        raise FlightRecorderFatalError(
            f"flight recorder {role} step {step!r} failed{path_hint}: "
            f"{type(exc).__name__}: {exc}",
        ) from exc
    mark_partial()
    logger.warning(
        "[flight_recorder] %s hook %s failed (fail_fast=false)%s: %s",
        role,
        step,
        path_hint,
        exc,
    )
