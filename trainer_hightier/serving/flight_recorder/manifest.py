"""Recording root layout and MANIFEST.json maintenance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trainer_hightier.config import HK_TZ
from trainer_hightier.serving.flight_recorder.config import FLIGHT_RECORDER_SCHEMA_VERSION


def sha256_file(path: Path) -> str:
    """Return hex sha256 digest for *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class ManifestFileEntry:
    """One artifact registered in MANIFEST.json."""

    path: str
    sha256: str
    size_bytes: int
    row_count: int | None = None


@dataclass
class RecordingRoot:
    """Mutable recording bundle root with manifest and cycle counters."""

    root: Path
    bundle_dir: Path
    model_version: str
    partial: bool = False
    files: list[ManifestFileEntry] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    _scorer_cycle: int = 0
    _validator_cycle: int = 0

    def ensure_layout(self) -> None:
        """Create standard recording directory tree."""
        for sub in (
            "identity",
            "permissions",
            "cycles/scorer",
            "cycles/validator",
            "ch_time_machine",
            "state",
            "feast",
            "source_context",
            "analysis",
        ):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def register_file(
        self,
        path: Path,
        *,
        row_count: int | None = None,
    ) -> ManifestFileEntry:
        """Append one file to in-memory manifest (relative to recording root)."""
        rel = path.relative_to(self.root).as_posix()
        entry = ManifestFileEntry(
            path=rel,
            sha256=sha256_file(path),
            size_bytes=int(path.stat().st_size),
            row_count=row_count,
        )
        self.files = [f for f in self.files if f.path != rel]
        self.files.append(entry)
        return entry

    def append_step(
        self,
        name: str,
        status: str,
        *,
        detail: str = "",
        error: str | None = None,
    ) -> None:
        """Record one initialization or export step."""
        self.steps.append(
            {"name": name, "status": status, "detail": detail, "error": error}
        )
        if status == "error":
            self.partial = True

    def next_scorer_cycle_dir(self) -> Path:
        """Allocate the next scorer cycle directory."""
        self._scorer_cycle += 1
        cycle_dir = self.root / "cycles" / "scorer" / f"cycle_{self._scorer_cycle:06d}"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        return cycle_dir

    def next_validator_cycle_dir(self) -> Path:
        """Allocate the next validator cycle directory."""
        self._validator_cycle += 1
        cycle_dir = self.root / "cycles" / "validator" / f"cycle_{self._validator_cycle:06d}"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        return cycle_dir

    def build_manifest_payload(self) -> dict[str, Any]:
        """Build manifest dict (without writing MANIFEST.json itself)."""
        now_utc = datetime.now(timezone.utc)
        return {
            "schema_version": FLIGHT_RECORDER_SCHEMA_VERSION,
            "collected_at_utc": now_utc.isoformat(),
            "collected_at_hk": now_utc.astimezone(ZoneInfo(HK_TZ)).isoformat(),
            "bundle_dir": str(self.bundle_dir),
            "recording_root": str(self.root),
            "model_version": self.model_version,
            "partial": bool(self.partial),
            "recorder_partial": bool(self.partial),
            "steps": self.steps,
            "files": [
                {
                    "path": f.path,
                    "sha256": f.sha256,
                    "size_bytes": f.size_bytes,
                    "row_count": f.row_count,
                }
                for f in self.files
            ],
        }

    def write_manifest(self) -> Path:
        """Write MANIFEST.json and register it in the file list."""
        manifest_path = self.root / "MANIFEST.json"
        payload = self.build_manifest_payload()
        manifest_path.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
        self.register_file(manifest_path)
        payload = self.build_manifest_payload()
        manifest_path.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
        self.files = [f for f in self.files if f.path != "MANIFEST.json"]
        self.register_file(manifest_path)
        return manifest_path
