"""Pack a flight recording directory into a zip archive."""

from __future__ import annotations

import argparse
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from trainer_hightier.serving.flight_recorder.manifest import (
    ManifestFileEntry,
    RecordingRoot,
)
from trainer_hightier.serving.flight_recorder.state_export import export_state_databases


def _load_existing_recording(recording_root: Path) -> RecordingRoot:
    """Rehydrate manifest metadata before adding pack-time exports."""
    manifest_path = recording_root / "MANIFEST.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    recording = RecordingRoot(
        root=recording_root,
        bundle_dir=Path(str(payload.get("bundle_dir") or recording_root.parent.parent)).resolve(),
        model_version=str(payload.get("model_version") or "unknown"),
        partial=bool(payload.get("recorder_partial") or payload.get("partial")),
    )
    recording.steps = list(payload.get("steps") or [])
    recording.files = [
        ManifestFileEntry(
            path=str(item["path"]),
            sha256=str(item["sha256"]),
            size_bytes=int(item["size_bytes"]),
            row_count=item.get("row_count"),
        )
        for item in payload.get("files", [])
        if isinstance(item, dict) and item.get("path") != "MANIFEST.json"
    ]
    return recording


def _infer_bundle_root(recording_root: Path, recording: RecordingRoot) -> Path:
    """Infer deploy bundle root for pack-time sibling SQLite export."""
    if recording.bundle_dir.is_dir():
        return recording.bundle_dir
    if recording_root.parent.name == "local_state":
        return recording_root.parent.parent.resolve()
    return recording_root.parent.resolve()


def _load_rel_paths(bundle_root: Path, recording_root: Path) -> dict[str, object]:
    """Load deploy relative paths or infer the local_state directory."""
    rel_path = bundle_root / "deploy_bundle_paths.json"
    if rel_path.is_file():
        return json.loads(rel_path.read_text(encoding="utf-8"))
    if recording_root.parent.name:
        return {"local_state_dir": recording_root.parent.name}
    return {"local_state_dir": "local_state"}


def refresh_sqlite_exports(recording_root: Path) -> None:
    """Export sibling SQLite DBs into the recording root before packaging."""
    recording = _load_existing_recording(recording_root)
    bundle_root = _infer_bundle_root(recording_root, recording)
    rel = _load_rel_paths(bundle_root, recording_root)
    export_state_databases(recording, bundle_root, rel)
    recording.write_manifest()


def pack_recording(
    recording_root: Path,
    output_zip: Path,
    *,
    export_sqlite: bool = True,
) -> Path:
    """Zip *recording_root* into *output_zip* (includes MANIFEST.json)."""
    recording_root = recording_root.resolve()
    if not recording_root.is_dir():
        raise FileNotFoundError(f"recording root not found: {recording_root}")
    if export_sqlite:
        refresh_sqlite_exports(recording_root)
    output_zip = output_zip.resolve()
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(recording_root.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(recording_root).as_posix()
                zf.write(path, arcname=arcname)
    return output_zip


def _default_zip_name(recording_root: Path) -> str:
    """Build a timestamped zip filename from recording root."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"flight_recording_{recording_root.name}_{stamp}.zip"


def main(argv: list[str] | None = None) -> int:
    """CLI entry for packing a recording bundle."""
    parser = argparse.ArgumentParser(description="Pack flight recording directory to zip")
    parser.add_argument("--recording-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--no-export-sqlite",
        action="store_true",
        help="skip pack-time export of sibling local_state SQLite DBs",
    )
    args = parser.parse_args(argv)
    out = args.output or (args.recording_root.parent / _default_zip_name(args.recording_root))
    pack_recording(args.recording_root, out, export_sqlite=not args.no_export_sqlite)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
