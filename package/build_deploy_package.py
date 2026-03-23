"""
Build a single deploy package: one folder (or one .zip file) containing
everything needed to run the ML API on the target machine.

After you run this script, you get either:
  - A folder: deploy_dist/   (at repo root; copy this folder to the target), or
  - A single file: deploy_dist.zip   (copy this file, then unzip on target; works well on Windows)

Contents: walkaway_ml wheel (from current trainer/ code), main.py, .env.example,
model artifacts (model.pkl, feature_list.json, feature_spec.yaml, etc.),
generated requirements.txt, and local_state dir. On the target you only:
  pip install -r requirements.txt
  cp .env.example .env  &&  edit .env  (set ClickHouse)
  python main.py
Then GET /alerts and GET /validation are available at http://0.0.0.0:8001.

Usage (from repo root):
  python -m package.build_deploy_package
  python -m package.build_deploy_package --archive
  python -m package.build_deploy_package --model-source trainer/models --output-dir deploy_dist
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "package" / "deploy"

logger = logging.getLogger(__name__)

# Model/bundle files (pkl, feature_list.json, feature_spec.yaml, etc.)
BUNDLE_FILES = [
    "model.pkl",
    "rated_model.pkl",
    "walkaway_model.pkl",
    "feature_list.json",
    "feature_spec.yaml",
    "model_version",
    "reason_code_map.json",
    "training_metrics.json",
    "pipeline_diagnostics.json",
]
MODEL_PKL_NAMES = ["model.pkl", "rated_model.pkl", "walkaway_model.pkl"]

# Phase 2 P0-P1: mlflow for export script when run on deploy (cron/scheduler on same or another machine).
REQUIREMENTS_DEPS = [
    "Flask>=2.0",
    "mlflow",
    "pandas",
    "numpy",
    "joblib",
    "lightgbm",
    "pyyaml",
    "python-dotenv",
    "clickhouse-driver",
]


def build_wheel(wheels_dir: Path) -> str:
    """Build walkaway_ml wheel into wheels_dir. Returns the wheel filename."""
    wheels_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "-w", str(wheels_dir), "--no-deps"],
        check=True,
        cwd=REPO_ROOT,
    )
    whls = list(wheels_dir.glob("walkaway_ml*.whl"))
    if not whls:
        raise FileNotFoundError(f"No walkaway_ml wheel found in {wheels_dir}")
    return whls[0].name


def copy_model_bundle(source_dir: Path, dest_models: Path) -> None:
    """Copy model and config files (pkl, json, yaml, etc.) from source_dir to dest_models."""
    source_dir = source_dir.resolve()
    dest_models = dest_models.resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Model source is not a directory: {source_dir}")
    try:
        found = list(source_dir.iterdir())
        names = [p.name for p in found]
    except OSError as e:
        raise FileNotFoundError(f"Cannot read model source {source_dir}: {e}") from e
    has_model = any((source_dir / n).exists() for n in MODEL_PKL_NAMES)
    if not has_model:
        raise FileNotFoundError(
            f"No model artifact in {source_dir}. Expected one of: {MODEL_PKL_NAMES}. "
            f"Found files: {names!r}. Run trainer or set --model-source to a directory containing model.pkl (or rated_model.pkl / walkaway_model.pkl)."
        )
    if not (source_dir / "feature_list.json").exists():
        raise FileNotFoundError(f"Missing {source_dir / 'feature_list.json'}. Found: {names!r}")

    # Flush existing content so we don't leave stale files from a previous run
    if dest_models.exists():
        shutil.rmtree(dest_models)
    dest_models.mkdir(parents=True, exist_ok=True)
    for name in BUNDLE_FILES:
        src = source_dir / name
        if src.exists():
            shutil.copy2(src, dest_models / name)
        elif name == "pipeline_diagnostics.json":
            logger.warning(
                "Model source missing optional %s; deploy bundle will omit it.",
                name,
            )


def write_requirements_txt(out_dir: Path, wheel_filename: str) -> None:
    # Use path relative to requirements.txt so pip finds the wheel in wheels/ subfolder
    wheel_path = f"wheels/{wheel_filename}"
    (out_dir / "requirements.txt").write_text(
        "# Install local wheel first (from wheels/), then PyPI deps. Run: pip install -r requirements.txt\n"
        f"{wheel_path}\n"
        + "\n".join(REQUIREMENTS_DEPS)
        + "\n",
        encoding="utf-8",
    )


def write_readme(out_dir: Path) -> None:
    (out_dir / "README_DEPLOY.txt").write_text(
        """ML API deploy package — GET /alerts, GET /validation

Target machine can be Windows, Linux, or Mac. Steps below cover all.

1. Get the folder on the target
   - Copy this folder to the target, or
   - If you have the .zip: unzip it.
     Windows (PowerShell): Expand-Archive -Path deploy_dist.zip -DestinationPath .
     Windows (GUI): Right-click the .zip → Extract All
     Linux / Mac:     unzip deploy_dist.zip
   Then open a terminal in the extracted folder (e.g. deploy_dist).

2. Install Python dependencies (no repo needed)
   All platforms:
     pip install -r requirements.txt
   (Use py -m pip or python3 -m pip if pip is not on PATH.)

3. Configure environment
   Create .env from the example and set ClickHouse: CH_HOST, CH_PORT, CH_USER, CH_PASS, SOURCE_DB.
   Optional: PORT or ML_API_PORT (default 8001).

   Windows (cmd):     copy .env.example .env
   Windows (PowerShell): Copy-Item .env.example .env
   Linux / Mac:       cp .env.example .env

   Then edit .env in any text editor (Notepad, VS Code, nano, vim, etc.).

4. Start the service
   All platforms:
     python main.py
   (Use py main.py on Windows or python3 main.py on Linux/Mac if needed.)

   Scorer, validator, and Flask API run in one process.
   Endpoints: http://0.0.0.0:8001/alerts  and  http://0.0.0.0:8001/validation

5. To swap the model only (same code / same requirements as before):
   Replace the files in the models/ folder with the new bundle, then restart (step 4).
   No pip — the model is files on disk, not a Python package.

6. To update after a new deploy package (new wheel and/or new dependencies):
   Keep the same venv from step 2. Copy or merge the new folder over the old one so
   wheels/ and requirements.txt match the new build. Then run again:
     pip install -r requirements.txt
   Pip usually only installs what changed (e.g. a new walkaway_ml wheel filename).
   To reinstall only the application wheel and skip other packages:
     pip install --upgrade --no-deps wheels/<wheel filename>
   Use the exact .whl name from the first line of requirements.txt or under wheels/.
   More detail: see package/deploy/README.md in the repository (section "Production bundle: updates").
""",
        encoding="utf-8",
    )


def build_deploy_package(
    output_dir: Path,
    model_source: Path,
    create_archive: bool = False,
) -> Path:
    """
    Build the deploy package into output_dir.
    Returns the path to the output directory.
    """
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve model_source: if relative, treat as relative to repo root so it works from any cwd
    if not model_source.is_absolute():
        model_source = (REPO_ROOT / model_source).resolve()
    else:
        model_source = model_source.resolve()

    wheels_dir = output_dir / "wheels"
    models_dir = output_dir / "models"
    local_state_dir = output_dir / "local_state"

    # 1. Build wheel from current repo (trainer/ -> walkaway_ml)
    print("Building walkaway_ml wheel...")
    wheel_name = build_wheel(wheels_dir)
    print(f"  -> wheels/{wheel_name}")

    # 2. Deploy entry and config
    shutil.copy2(DEPLOY_DIR / "main.py", output_dir / "main.py")
    env_example_src = DEPLOY_DIR / ".env.example"
    if not env_example_src.exists():
        env_example_src = DEPLOY_DIR / "env.example"
    if env_example_src.exists():
        shutil.copy2(env_example_src, output_dir / ".env.example")
    # Remove legacy env.example if present (bundle should only have .env.example)
    _legacy = output_dir / "env.example"
    if _legacy.exists():
        _legacy.unlink()
    if (DEPLOY_DIR / "app.yaml").exists():
        shutil.copy2(DEPLOY_DIR / "app.yaml", output_dir / "app.yaml")
    print("  -> main.py, .env.example, app.yaml")

    # 2b. Player profile: ship if exists (repo data/ = trainer LOCAL_PARQUET_DIR)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    profile_src = REPO_ROOT / "data" / "player_profile.parquet"
    profile_shipped = False
    if profile_src.exists():
        try:
            shutil.copy2(profile_src, data_dir / "player_profile.parquet")
            profile_shipped = True
            print("  -> data/player_profile.parquet")
        except OSError as e:
            print(
                f"  Warning: failed to copy player_profile.parquet: {e}",
                file=sys.stderr,
            )
            # profile_shipped stays False; error printed at end
    # If not shipped, error is printed at the end (after archive).

    # 3. Model bundle (includes .pkl, feature_list.json, feature_spec.yaml, etc.)
    print(f"Copying model and config from {model_source}...")
    copy_model_bundle(model_source, models_dir)
    print(f"  -> models/ ({len(list(models_dir.iterdir()))} files)")

    # 4. Dir for state DB (app creates state.db here)
    local_state_dir.mkdir(parents=True, exist_ok=True)
    (local_state_dir / ".gitkeep").write_text("", encoding="utf-8")

    # 5. requirements.txt
    write_requirements_txt(output_dir, wheel_name)
    write_readme(output_dir)
    print("  -> requirements.txt, README_DEPLOY.txt")

    print(f"\nDeploy package ready: {output_dir}")
    print("  Copy this folder to the target machine, then follow README_DEPLOY.txt")

    if create_archive:
        archive_name = output_dir.name + ".zip"
        archive_path = output_dir.parent / archive_name
        try:
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in output_dir.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(output_dir.parent))
            print(f"\nSingle file for transfer: {archive_path}")
            print(f"  Copy this file to the target, then unzip (e.g. Expand-Archive on Windows, or unzip {archive_name}) and cd into the folder.")
        except Exception as e:
            print(f"\nWarning: could not create archive {archive_path}: {e}", file=sys.stderr)
            print("  You can still copy the folder above.", file=sys.stderr)

    if not profile_shipped:
        print(
            f"\nError: player_profile.parquet not found at {profile_src}; not shipped. "
            "Scorer will run with profile features as NaN.",
            file=sys.stderr,
        )

    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a single deploy package (folder or .zip) to move to the target machine."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "deploy_dist",
        help="Output folder (default: ./deploy_dist)",
    )
    # Treat empty or whitespace-only as unset (STATUS Code Review 項目 4 §1).
    _model_dir_env = os.environ.get("MODEL_DIR")
    _model_dir_effective = (
        _model_dir_env.strip() if (_model_dir_env and _model_dir_env.strip()) else None
    )
    _default_model_source = (
        Path(_model_dir_effective) if _model_dir_effective else (REPO_ROOT / "out" / "models")
    )
    parser.add_argument(
        "--model-source",
        type=Path,
        default=_default_model_source,
        help="Source of model artifacts and YAML (default: out/models or MODEL_DIR env)",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="Also create deploy_dist.zip so you can move a single file (Windows-friendly)",
    )
    args = parser.parse_args()

    try:
        build_deploy_package(
            output_dir=args.output_dir,
            model_source=args.model_source,
            create_archive=args.archive,
        )
        return 0
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"Error: pip wheel failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
