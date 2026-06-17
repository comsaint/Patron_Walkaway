"""Per-cycle recorder context (directory layout for scorer/validator)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot

CycleRole = Literal["scorer", "validator"]


@dataclass
class RecorderContext:
    """Active recording session bound to one deploy bundle."""

    bundle_root: Path
    config: FlightRecorderConfig
    recording: RecordingRoot
    rel: dict[str, Any]

    @classmethod
    def open(
        cls,
        bundle_root: Path,
        config: FlightRecorderConfig,
        *,
        rel: dict[str, Any],
        model_version: str,
    ) -> RecorderContext:
        """Open or create a recording root under *bundle_root*."""
        recording_root = config.resolve_recording_root(bundle_root)
        recording = RecordingRoot(
            root=recording_root,
            bundle_dir=bundle_root.resolve(),
            model_version=model_version,
        )
        recording.ensure_layout()
        return cls(
            bundle_root=bundle_root.resolve(),
            config=config,
            recording=recording,
            rel=rel,
        )

    def start_cycle(self, role: CycleRole) -> Path:
        """Allocate a new cycle directory for *role*."""
        if role == "scorer":
            return self.recording.next_scorer_cycle_dir()
        return self.recording.next_validator_cycle_dir()

    def scorer_cycle_subdirs(self, cycle_dir: Path) -> dict[str, Path]:
        """Create scorer stage subdirectories under *cycle_dir*."""
        paths = {
            "clickhouse": cycle_dir / "clickhouse",
            "stages": cycle_dir / "stages",
            "audits": cycle_dir / "audits",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths

    def validator_cycle_subdirs(self, cycle_dir: Path) -> dict[str, Path]:
        """Create validator subdirectories under *cycle_dir*."""
        paths = {
            "clickhouse": cycle_dir / "clickhouse",
            "alerts": cycle_dir / "alerts",
            "decisions": cycle_dir / "decisions",
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths
