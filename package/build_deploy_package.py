"""
Build a single deploy package: one folder (or one .zip file) containing
everything needed to run the ML API on the target machine.

After you run this script, you get either:
  - A folder: deploy_dist/   (at repo root; copy this folder to the target), or
  - A single file: deploy_dist.zip   (copy this file, then unzip on target; works well on Windows)

Contents: walkaway_ml wheel (from current trainer/ code), main.py, .env.example,
ML_API_PROTOCOL.md (API contract for ops), model artifacts (model.pkl required; feature_list.json, etc.),
generated requirements.txt (includes numba + pyarrow for serving parity with package/deploy), and local_state dir. On the target you only:
  pip install -r requirements.txt
  cp .env.example .env  &&  edit .env  (CH_* required; see .env.example for log level, scorer windows, paths)
  python main.py
Then GET /alerts and GET /validation are available at http://0.0.0.0:8001.

Usage (from repo root):
  python -m package.build_deploy_package
  python -m package.build_deploy_package --archive
  python -m package.build_deploy_package --model-source trainer/models --output-dir deploy_dist
  python -m package.build_deploy_package --data-source data --strict-data   # fail if no player_profile.parquet
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = REPO_ROOT / "package" / "deploy"
README_DEPLOY_SOURCE = PACKAGE_DIR / "README_DEPLOY.md"

logger = logging.getLogger(__name__)

# Model/bundle files (pkl, feature_list.json, feature_spec.yaml, etc.)
BUNDLE_FILES = [
    "model.pkl",
    "feature_list.json",
    "feature_spec.yaml",
    "model_version",
    "reason_code_map.json",
    "training_metrics.json",
    "training_metrics.v2.json",
    "feature_importance.json",
    "comparison_metrics.json",
    "pipeline_diagnostics.json",
]
MODEL_PKL_NAMES = ["model.pkl"]

# ---------------------------------------------------------------------------
# Serving data artifacts (deploy_dist/data/) — not raw ClickHouse mirrors
# ---------------------------------------------------------------------------
# Required for production-quality scoring when the model uses profile features.
SERVING_DATA_REQUIRED = ("player_profile.parquet",)
# Optional: provenance hash; canonical pair reduces cold-start recompute on target.
SERVING_DATA_RECOMMENDED = (
    "player_profile.schema_hash",
    "canonical_mapping.parquet",
    "canonical_mapping.cutoff.json",
)
# Local dev/training mirrors of CH tables — never ship in deploy bundle (size + freshness).
RAW_CH_MIRROR_FILES_EXCLUDED = (
    "gmwds_t_bet.parquet",
    "gmwds_t_session.parquet",
    "gmwds_t_game.parquet",
)


@dataclass
class ServingDataCopyResult:
    """Summary of copy_serving_data_artifacts for logging and tests."""

    shipped: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    missing_recommended: list[str] = field(default_factory=list)
    canonical_pair_skipped_incomplete_source: bool = False
    excluded_raw_present: list[str] = field(default_factory=list)
    profile_shipped: bool = False
    copy_errors: list[str] = field(default_factory=list)


def _resolve_data_source(data_source: Path) -> Path:
    """Resolve data artifact directory; relative paths are under REPO_ROOT."""
    if not data_source.is_absolute():
        return (REPO_ROOT / data_source).resolve()
    return data_source.resolve()


def copy_serving_data_artifacts(
    data_source: Path,
    data_dest: Path,
    *,
    strict_data: bool,
) -> ServingDataCopyResult:
    """Copy serving-only parquet/json from *data_source* into deploy *data_dest*.

    Canonical mapping is copied only when **both** parquet and cutoff sidecar exist,
    to avoid shipping a half-pair that the scorer cannot load.

    Raw ClickHouse mirror parquets (gmwds_t_*.parquet) are never copied; if present
    under *data_source*, their basenames are recorded in *excluded_raw_present*.
    """
    data_source = _resolve_data_source(data_source)
    data_dest.mkdir(parents=True, exist_ok=True)
    out = ServingDataCopyResult()

    for name in RAW_CH_MIRROR_FILES_EXCLUDED:
        if (data_source / name).is_file():
            out.excluded_raw_present.append(name)

    for name in SERVING_DATA_REQUIRED:
        src = data_source / name
        if not src.is_file():
            out.missing_required.append(name)
            continue
        dst = data_dest / name
        try:
            shutil.copy2(src, dst)
            out.shipped.append(name)
            if name == "player_profile.parquet":
                out.profile_shipped = True
        except OSError as e:
            out.copy_errors.append(f"{name}: {e}")
            out.missing_required.append(name)

    cmap_parquet = data_source / "canonical_mapping.parquet"
    cmap_json = data_source / "canonical_mapping.cutoff.json"
    if cmap_parquet.is_file() and cmap_json.is_file():
        try:
            shutil.copy2(cmap_parquet, data_dest / "canonical_mapping.parquet")
            shutil.copy2(cmap_json, data_dest / "canonical_mapping.cutoff.json")
            out.shipped.append("canonical_mapping.parquet")
            out.shipped.append("canonical_mapping.cutoff.json")
        except OSError as e:
            for n in ("canonical_mapping.parquet", "canonical_mapping.cutoff.json"):
                p = data_dest / n
                if p.is_file():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            out.copy_errors.append(f"canonical_mapping: {e}")
    elif cmap_parquet.is_file() or cmap_json.is_file():
        out.canonical_pair_skipped_incomplete_source = True
        out.missing_recommended.extend(
            [n for n in ("canonical_mapping.parquet", "canonical_mapping.cutoff.json") if not (data_source / n).is_file()]
        )
    else:
        out.missing_recommended.extend(["canonical_mapping.parquet", "canonical_mapping.cutoff.json"])

    schema_hash = data_source / "player_profile.schema_hash"
    if schema_hash.is_file():
        try:
            shutil.copy2(schema_hash, data_dest / "player_profile.schema_hash")
            out.shipped.append("player_profile.schema_hash")
        except OSError as e:
            out.copy_errors.append(f"player_profile.schema_hash: {e}")
            out.missing_recommended.append("player_profile.schema_hash")
    else:
        out.missing_recommended.append("player_profile.schema_hash")

    if strict_data and out.missing_required:
        missing = ", ".join(out.missing_required)
        raise FileNotFoundError(
            f"--strict-data: required serving data missing or failed to copy under {data_source}: {missing}"
        )

    return out


def _print_serving_data_summary(result: ServingDataCopyResult, resolved_data_source: Path) -> None:
    """Print shipped / missing / excluded summary to stdout and stderr as appropriate."""
    print(f"  [data] source: {resolved_data_source}")
    print("  [data] serving artifacts:")
    if result.shipped:
        for name in sorted(set(result.shipped)):
            print(f"    -> data/{name}")
    for name in SERVING_DATA_REQUIRED:
        if name not in result.shipped:
            print(f"    ! missing (required): {name}", file=sys.stderr)
    if result.missing_recommended:
        for name in sorted(set(result.missing_recommended)):
            print(f"    . optional not shipped: {name}")
    if result.canonical_pair_skipped_incomplete_source:
        print(
            "    . canonical_mapping: only one of parquet/cutoff.json present under source; "
            "skipped both (scorer needs both to load artifact).",
            file=sys.stderr,
        )
    if result.copy_errors:
        for msg in result.copy_errors:
            print(f"    ! copy error: {msg}", file=sys.stderr)
    if result.excluded_raw_present:
        print(
            "    . raw CH mirror files present under data source (not shipped): "
            + ", ".join(sorted(result.excluded_raw_present)),
        )
        print(
            "      (Runtime bet/session/game come from ClickHouse on the target machine.)",
        )


# Phase 2 P0-P1: mlflow for export script when run on deploy (cron/scheduler on same or another machine).
# Keep aligned with package/deploy/requirements.txt for serving: Parquet I/O (profile, canonical map)
# and numba-accelerated feature lookbacks in trainer.features; scorer logs numba availability at startup.
# optuna + duckdb: walkaway_ml imports trainer.training.trainer (optuna) and features paths (duckdb) at load time.
# Optional: enable SCORER_ENABLE_SHAP_REASON_CODES on target requires `pip install shap` (not bundled by default).
REQUIREMENTS_DEPS = [
    "Flask>=2.0",
    "mlflow",
    "pandas",
    "numpy",
    "joblib",
    "lightgbm",
    "pyyaml",
    "python-dotenv",
    "clickhouse-connect==0.13.0",
    "numba",
    "pyarrow",
    "optuna==4.7.0",
    "duckdb==1.4.4",
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
            f"Found files: {names!r}. Run trainer or set --model-source to a directory containing model.pkl (DEC-040: rated_model.pkl / walkaway_model.pkl are not accepted)."
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
    if not README_DEPLOY_SOURCE.is_file():
        raise FileNotFoundError(
            f"Missing deploy readme source {README_DEPLOY_SOURCE!s}; "
            "expected package/README_DEPLOY.md next to build_deploy_package.py."
        )
    dest = out_dir / "README_DEPLOY.md"
    dest.write_text(
        README_DEPLOY_SOURCE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def build_deploy_package(
    output_dir: Path,
    model_source: Path,
    create_archive: bool = False,
    data_source: Path = Path("data"),
    strict_data: bool = False,
) -> Path:
    """
    Build the deploy package into output_dir.
    Returns the path to the output directory.

    Parameters
    ----------
    data_source:
        Directory containing serving artifacts (``player_profile.parquet``, optional
        ``canonical_mapping.*``, ``player_profile.schema_hash``). Relative paths
        are resolved under ``REPO_ROOT``.
    strict_data:
        When True, missing required ``player_profile.parquet`` (or failed copy)
        raises ``FileNotFoundError`` so the process can exit non-zero.
    """
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve model_source: if relative, treat as relative to repo root so it works from any cwd
    if not model_source.is_absolute():
        model_source = (REPO_ROOT / model_source).resolve()
    else:
        model_source = model_source.resolve()

    resolved_data_source = _resolve_data_source(data_source)
    profile_src = resolved_data_source / "player_profile.parquet"
    if strict_data and not profile_src.is_file():
        raise FileNotFoundError(
            f"--strict-data: required player_profile.parquet not found at {profile_src}"
        )

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

    # 2a. API contract (GET /alerts, /validation) for operators and integrators
    _protocol_src = REPO_ROOT / "package" / "ML_API_PROTOCOL.md"
    if _protocol_src.is_file():
        shutil.copy2(_protocol_src, output_dir / "ML_API_PROTOCOL.md")
        print("  -> ML_API_PROTOCOL.md")
    else:
        logger.warning("Missing %s; deploy bundle will omit API protocol doc.", _protocol_src)

    # 2b. Serving data artifacts (player profile, canonical map, schema hash)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    serving_result = copy_serving_data_artifacts(
        data_source,
        data_dir,
        strict_data=strict_data,
    )
    _print_serving_data_summary(serving_result, resolved_data_source)

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
    print("  -> requirements.txt, README_DEPLOY.md")

    print(f"\nDeploy package ready: {output_dir}")
    print("  Copy this folder to the target machine, then follow README_DEPLOY.md")

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

    if not serving_result.profile_shipped and not strict_data:
        print(
            f"\nError: player_profile.parquet not found or not copied from {profile_src}; not shipped. "
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
    parser.add_argument(
        "--data-source",
        type=Path,
        default=Path("data"),
        help="Directory with serving data artifacts (default: ./data under repo root)",
    )
    parser.add_argument(
        "--strict-data",
        action="store_true",
        help="Fail the build if player_profile.parquet is missing or cannot be copied",
    )
    args = parser.parse_args()

    try:
        build_deploy_package(
            output_dir=args.output_dir,
            model_source=args.model_source,
            create_archive=args.archive,
            data_source=args.data_source,
            strict_data=args.strict_data,
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
