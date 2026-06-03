"""Tests for replay_recording_bundle loader."""

from __future__ import annotations

import json
from pathlib import Path

from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot
from trainer_hightier.serving.replay_recording_bundle import build_bundle_summary


def test_build_bundle_summary_minimal(tmp_path: Path) -> None:
    """Summary loads manifest and counts cycle directories."""
    root = tmp_path / "recording"
    recording = RecordingRoot(
        root=root,
        bundle_dir=tmp_path / "bundle",
        model_version="mv-test",
    )
    recording.ensure_layout()
    sample = root / "identity" / "sample.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("x", encoding="utf-8")
    recording.register_file(sample)
    recording.write_manifest()
    (root / "cycles" / "scorer" / "cycle_000001").mkdir(parents=True)
    summary = build_bundle_summary(root)
    assert summary["model_version"] == "mv-test"
    assert summary["n_scorer_cycles"] == 1
    assert not any("missing:" in err for err in summary["manifest_file_errors"])
