"""Capture model-agnostic identity snapshots into a recording bundle."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from trainer_hightier.config import FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
from trainer_hightier.serving.adt_allowlist import sha256_file
from trainer_hightier.serving.flight_recorder.manifest import RecordingRoot

_IDENTITY_FILES: tuple[str, ...] = (
    "bundle_info.json",
    "deploy_bundle_paths.json",
)

_MODEL_IDENTITY_FILES: tuple[str, ...] = (
    "run_summary.json",
    "metrics_detailed.json",
    "model_version",
    "feature_parity_verification.json",
    "training_metrics.json",
    "run_report.json",
)


def _copy_if_exists(src: Path, dst: Path) -> bool:
    """Copy *src* to *dst* when source exists."""
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _file_hash_entry(path: Path) -> dict[str, Any] | None:
    """Build one hash entry when *path* is a file."""
    if not path.is_file():
        return None
    return {"path": str(path.name), "sha256": sha256_file(path)}


def capture_identity(
    recording: RecordingRoot,
    bundle_root: Path,
    rel: dict[str, Any],
) -> int:
    """Copy identity artifacts and write ``identity/model_hashes.json``."""
    identity_dir = recording.root / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in _IDENTITY_FILES:
        src = bundle_root / name
        if _copy_if_exists(src, identity_dir / name):
            copied += 1
            recording.register_file(identity_dir / name)
    model_dir = bundle_root / str(rel.get("model_bundle_dir", "models"))
    for name in _MODEL_IDENTITY_FILES:
        if _copy_if_exists(model_dir / name, identity_dir / name):
            copied += 1
            recording.register_file(identity_dir / name)
    registry = model_dir / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    if _copy_if_exists(registry, identity_dir / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME):
        copied += 1
        recording.register_file(identity_dir / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME)
    mapping_allow = bundle_root / "mapping" / "adt_allowed_players_q0p99.parquet"
    mapping_canon = bundle_root / "mapping" / "canonical_player_mapping.parquet"
    hashes = _build_model_hashes(model_dir, mapping_allow, mapping_canon)
    hashes_path = identity_dir / "model_hashes.json"
    hashes_path.write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    recording.register_file(hashes_path)
    copied += 1
    _write_package_freeze(identity_dir, recording)
    _write_git_status(identity_dir, bundle_root, recording)
    recording.append_step("capture_identity", "ok", detail=f"files={copied}")
    return copied


def _build_model_hashes(
    model_dir: Path,
    allowlist: Path,
    canonical_mapping: Path,
) -> dict[str, Any]:
    """Build hash summary for model bundle and mapping files."""
    entries: list[dict[str, Any]] = []
    if model_dir.is_dir():
        model_pkl = model_dir / "model.pkl"
        entry = _file_hash_entry(model_pkl)
        if entry:
            entries.append(entry)
    for label, path in (
        ("adt_allowlist", allowlist),
        ("canonical_mapping", canonical_mapping),
    ):
        if path.is_file():
            entries.append({"label": label, "path": str(path.name), "sha256": sha256_file(path)})
    return {"files": entries}


def _write_package_freeze(identity_dir: Path, recording: RecordingRoot) -> None:
    """Write ``package_freeze.txt`` via pip freeze when available."""
    out = identity_dir / "package_freeze.txt"
    try:
        proc = subprocess.run(
            ["pip", "freeze"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        body = proc.stdout if proc.returncode == 0 else f"pip freeze failed: {proc.stderr}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        body = f"pip freeze unavailable: {type(exc).__name__}: {exc}"
    out.write_text(body, encoding="utf-8")
    recording.register_file(out)


def _write_git_status(
    identity_dir: Path,
    bundle_root: Path,
    recording: RecordingRoot,
) -> None:
    """Write ``git_status.txt`` when git is available near bundle or repo."""
    out = identity_dir / "git_status.txt"
    for search in (bundle_root, bundle_root.parent, bundle_root.parent.parent):
        if (search / ".git").is_dir():
            proc = subprocess.run(
                ["git", "-C", str(search), "status", "--short", "--branch"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            body = proc.stdout if proc.returncode == 0 else proc.stderr
            out.write_text(body or "(empty git status)", encoding="utf-8")
            recording.register_file(out)
            return
    out.write_text("git not available\n", encoding="utf-8")
    recording.register_file(out)
