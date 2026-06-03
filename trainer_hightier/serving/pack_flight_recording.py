"""Pack a flight recording directory into a zip archive."""

from __future__ import annotations

import argparse
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def pack_recording(recording_root: Path, output_zip: Path) -> Path:
    """Zip *recording_root* into *output_zip* (includes MANIFEST.json)."""
    recording_root = recording_root.resolve()
    if not recording_root.is_dir():
        raise FileNotFoundError(f"recording root not found: {recording_root}")
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
    args = parser.parse_args(argv)
    out = args.output or (args.recording_root.parent / _default_zip_name(args.recording_root))
    pack_recording(args.recording_root, out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
