"""Tests for flight recorder manifest and recording root initialization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.init_recording import init_recording_root
from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot, sha256_file


def test_recording_root_registers_files_and_manifest(tmp_path: Path) -> None:
    """RecordingRoot tracks sha256 entries and writes MANIFEST.json."""
    root = tmp_path / "recording"
    recording = RecordingRoot(
        root=root,
        bundle_dir=tmp_path / "bundle",
        model_version="test-model-v1",
    )
    recording.ensure_layout()
    sample = root / "identity" / "sample.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("hello", encoding="utf-8")
    entry = recording.register_file(sample)
    assert entry.sha256 == sha256_file(sample)
    manifest_path = recording.write_manifest()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["model_version"] == "test-model-v1"
    assert payload["schema_version"] == "flight_recorder_v1"
    paths = {f["path"] for f in payload["files"]}
    assert "identity/sample.txt" in paths
    assert "MANIFEST.json" in paths


def test_init_recording_root_minimal_bundle(tmp_path: Path) -> None:
    """init_recording_root creates layout and manifest on a minimal bundle."""
    bundle = tmp_path / "bundle"
    model_dir = bundle / "models"
    model_dir.mkdir(parents=True)
    (model_dir / "model_version").write_text("20260529-test\n", encoding="utf-8")
    (bundle / "bundle_info.json").write_text("{}", encoding="utf-8")
    (bundle / "deploy_bundle_paths.json").write_text(
        json.dumps({"local_state_dir": "local_state"}),
        encoding="utf-8",
    )
    config = FlightRecorderConfig(recording_root="local_state/flight_recording")
    ctx = init_recording_root(bundle, config, export_sqlite=False)
    assert ctx.recording.root.is_dir()
    assert (ctx.recording.root / "MANIFEST.json").is_file()
    assert (ctx.recording.root / "identity" / "model_hashes.json").is_file()
    assert (bundle / "local_state" / "flight_recording_config.yaml").is_file()


def test_next_cycle_dirs_increment(tmp_path: Path) -> None:
    """Scorer and validator cycle directories use monotonic ids."""
    recording = RecordingRoot(
        root=tmp_path / "rec",
        bundle_dir=tmp_path,
        model_version="v",
    )
    d1 = recording.next_scorer_cycle_dir()
    d2 = recording.next_scorer_cycle_dir()
    assert d1.name == "cycle_000001"
    assert d2.name == "cycle_000002"
    v1 = recording.next_validator_cycle_dir()
    assert v1.name == "cycle_000001"
