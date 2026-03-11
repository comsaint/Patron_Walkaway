"""
Package trained model artifacts into a deployable bundle for inference.

After training, trainer writes to trainer/models/. This script copies only the
files needed by scorer/backtester into a versioned bundle directory (and
optionally a .tar.gz archive). See package/PLAN.md.

Usage (from repo root):
  python -m package.package_model_bundle --source-dir trainer/models --output-dir package/bundles
  python -m package.package_model_bundle --source-dir trainer/models --archive
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
from pathlib import Path

# Repo root: package/package_model_bundle.py -> parent.parent
REPO_ROOT = Path(__file__).resolve().parent.parent

# Files to copy when present (name only; we copy from source_dir)
BUNDLE_FILES = [
    "model.pkl",
    "rated_model.pkl",
    "walkaway_model.pkl",
    "feature_list.json",
    "feature_spec.yaml",
    "model_version",
    "reason_code_map.json",
    "training_metrics.json",
]

# At least one of these must exist for a valid bundle
MODEL_PKL_NAMES = ["model.pkl", "rated_model.pkl", "walkaway_model.pkl"]
REQUIRED_NON_MODEL = ["feature_list.json"]


def get_version(source_dir: Path) -> str:
    """Read version from source_dir/model_version or use timestamp."""
    version_file = source_dir / "model_version"
    if version_file.exists():
        return version_file.read_text(encoding="utf-8").strip()
    import time
    return time.strftime("%Y%m%d%H%M%S")


def build_bundle(
    source_dir: Path,
    output_dir: Path,
    version: str | None = None,
    create_archive: bool = False,
) -> Path:
    """
    Copy required and optional artifacts from source_dir into output_dir/<version>/.
    Returns the path to the bundle directory.
    """
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if version is None:
        version = get_version(source_dir)

    # Require at least one model pkl and feature_list.json
    has_model = any((source_dir / name).exists() for name in MODEL_PKL_NAMES)
    has_features = (source_dir / "feature_list.json").exists()
    if not has_model:
        raise FileNotFoundError(
            f"No model artifact found in {source_dir}. "
            f"Expected one of: {MODEL_PKL_NAMES}. Run trainer first."
        )
    if not has_features:
        raise FileNotFoundError(
            f"Missing {source_dir / 'feature_list.json'}. Run trainer first."
        )

    bundle_dir = output_dir / version
    bundle_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for name in BUNDLE_FILES:
        src = source_dir / name
        if not src.exists():
            continue
        dst = bundle_dir / name
        shutil.copy2(src, dst)
        copied.append(name)

    # Optional: write bundle_info.json
    bundle_info = {
        "model_version": version,
        "files": copied,
    }
    version_from_file = source_dir / "model_version"
    if version_from_file.exists():
        bundle_info["model_version_string"] = version_from_file.read_text(encoding="utf-8").strip()
    (bundle_dir / "bundle_info.json").write_text(
        json.dumps(bundle_info, indent=2), encoding="utf-8"
    )
    copied.append("bundle_info.json")

    print(f"Bundle created: {bundle_dir} ({len(copied)} files)")

    if create_archive:
        archive_path = output_dir / f"model_bundle_{version}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tf:
            tf.add(bundle_dir, arcname=bundle_dir.name)
        print(f"Archive created: {archive_path}")

    return bundle_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Package trained model artifacts into a deployable bundle (see package/PLAN.md)."
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=REPO_ROOT / "trainer" / "models",
        help="Directory containing training output (default: trainer/models)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "package" / "bundles",
        help="Output directory for bundle(s) (default: package/bundles)",
    )
    parser.add_argument(
        "--version",
        type=str,
        default=None,
        help="Bundle version; default: read from source-dir/model_version or timestamp",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Also create model_bundle_<version>.tar.gz in output-dir",
    )
    args = parser.parse_args()

    try:
        build_bundle(
            source_dir=args.source_dir,
            output_dir=args.output_dir,
            version=args.version,
            create_archive=args.archive,
        )
        return 0
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    sys.exit(main())
