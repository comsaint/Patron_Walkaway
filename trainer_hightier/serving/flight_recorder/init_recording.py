"""Initialize a production flight recording root on a deploy bundle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trainer_hightier.serving.flight_recorder.config import (
    DEFAULT_CONFIG_REL,
    FlightRecorderConfig,
)
from trainer_hightier.serving.flight_recorder.context import RecorderContext
from trainer_hightier.serving.flight_recorder.identity import capture_identity
from trainer_hightier.serving.flight_recorder.failure import validate_recording_root_writable
from trainer_hightier.serving.flight_recorder.state_export import export_state_databases


def _load_model_version(bundle_root: Path, rel: dict[str, Any]) -> str:
    """Read model version text from bundle."""
    model_dir = bundle_root / str(rel.get("model_bundle_dir", "models"))
    ver_path = model_dir / "model_version"
    if ver_path.is_file():
        text = ver_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return model_dir.name


def _load_rel_paths(bundle_root: Path) -> dict[str, Any]:
    """Load ``deploy_bundle_paths.json`` when present."""
    path = bundle_root / "deploy_bundle_paths.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def init_recording_root(
    bundle_root: Path,
    config: FlightRecorderConfig,
    *,
    write_default_config: bool = True,
    export_sqlite: bool = True,
) -> RecorderContext:
    """Create recording layout, identity snapshot, optional SQLite exports."""
    bundle_root = bundle_root.resolve()
    recording_root = config.resolve_recording_root(bundle_root)
    validate_recording_root_writable(recording_root, fail_fast=bool(config.fail_fast))
    rel = _load_rel_paths(bundle_root)
    model_version = _load_model_version(bundle_root, rel)
    if write_default_config:
        cfg_path = bundle_root / DEFAULT_CONFIG_REL
        if not cfg_path.is_file():
            config.write_yaml(cfg_path)
    ctx = RecorderContext.open(
        bundle_root,
        config,
        rel=rel,
        model_version=model_version,
    )
    runtime_snapshot = ctx.recording.root / "identity" / "runtime_config_snapshot.json"
    runtime_snapshot.parent.mkdir(parents=True, exist_ok=True)
    runtime_snapshot.write_text(
        json.dumps(config.to_mapping(), indent=2),
        encoding="utf-8",
    )
    ctx.recording.register_file(runtime_snapshot)
    capture_identity(ctx.recording, bundle_root, rel)
    if export_sqlite:
        export_state_databases(ctx.recording, bundle_root, rel)
    ctx.recording.write_manifest()
    return ctx
