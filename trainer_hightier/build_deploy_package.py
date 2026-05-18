"""Build a portable ``trainer_hightier`` deploy folder (or .zip).

Layout (resolved ``--output-dir``, default ``trainer_hightier.config.DEFAULT_DEPLOY_OUTPUT_ROOT`` / bundle ``model_version``):

- ``models/`` — ``model.pkl``, ``training_metrics.json``, ``model_version``, …
- ``snapshots/active_manifest.json`` — paths rewritten to bundle-relative
- ``snapshots/artifacts/*.parquet`` — copied snapshot layers (slow / trial / allowlist)
- ``mapping/`` — canonical mapping Parquet
- ``local_state/`` — empty dir for ``state.db`` / ``feature_state.db`` at runtime
- ``bundle_info.json``, ``deploy_bundle_paths.json``, ``README_DEPLOY.md``, ``requirements.txt``
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hashlib

from trainer.core.model_bundle_paths import resolve_model_bundle_dir

from trainer_hightier import config as th_config
from trainer_hightier.config import default_hightier_serving_config
from trainer_hightier.serving.adt_allowlist import sha256_file
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    pr = argparse.ArgumentParser(
        description="Build trainer_hightier deploy bundle (Frozen artifact mode by default)."
    )
    mx = pr.add_mutually_exclusive_group(required=False)
    mx.add_argument(
        "--model-source",
        type=Path,
        default=None,
        help="Directory containing model.pkl (Step 5 bundle dir). Overrides --model-version.",
    )
    mx.add_argument(
        "--model-version",
        type=str,
        default=None,
        help="Single-segment bundle id under DEFAULT_MODEL_DIR (mutually exclusive with --model-source).",
    )
    pr.add_argument(
        "--snapshot-manifest-source",
        type=Path,
        default=None,
        help=(
            "Path to active_manifest.json or its parent directory. "
            "Default: default_hightier_serving_config().snapshot_manifest_dir."
        ),
    )
    pr.add_argument(
        "--mapping-source",
        type=Path,
        default=None,
        help="Canonical mapping Parquet file. Default: default_canonical_mapping_parquet_path().",
    )
    pr.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Deploy bundle output directory (must be absent or empty). "
            "Default: trainer_hightier.config.DEFAULT_DEPLOY_OUTPUT_ROOT / <model_version> "
            "(repo default: out/deploy_hightier/<run-id>/)."
        ),
    )
    pr.add_argument("--archive", action="store_true", help="Also write <output-dir-name>.zip beside output-dir.")
    pr.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail when required artifacts are missing (default: strict).",
    )
    return pr.parse_args(argv)


def _resolved_model_bundle_dir(*, model_source: Path | None, model_version: str | None) -> Path:
    """Resolve Step 5 bundle directory (--model-source > --model-version > latest manifest)."""

    def _explicit_dir_errors(p: Path) -> None:
        if not p.is_dir():
            raise NotADirectoryError(f"model source must be directory: {p}")
        if not (p / "model.pkl").is_file():
            raise FileNotFoundError(f"No model.pkl under resolved bundle dir {p}")

    if model_source is not None:
        p = Path(model_source).expanduser().resolve()
        _explicit_dir_errors(p)
        return p
    if model_version is not None:
        mv = str(model_version).strip()
        if not mv:
            raise ValueError("--model-version must be non-empty when provided")
        cand = resolve_model_bundle_dir(Path(th_config.DEFAULT_MODEL_DIR), model_version=mv)
        _explicit_dir_errors(cand)
        return cand
    latest = resolve_model_bundle_dir(Path(th_config.DEFAULT_MODEL_DIR))
    _explicit_dir_errors(latest)
    return latest


def _snapshot_manifest_origin(args: argparse.Namespace) -> Path:
    if args.snapshot_manifest_source is not None:
        return Path(args.snapshot_manifest_source).expanduser().resolve()
    srv = Path(default_hightier_serving_config().snapshot_manifest_dir).expanduser().resolve()
    if not srv.is_dir():
        raise FileNotFoundError(
            "default snapshot manifest dir missing (-directory); expected "
            f"{srv} (--snapshot-manifest-source to override). Run snapshot updater or pass an explicit manifest path."
        )
    return srv


def _canonical_mapping_origin(args: argparse.Namespace) -> Path:
    if args.mapping_source is not None:
        return Path(args.mapping_source).expanduser().resolve()
    return Path(default_canonical_mapping_parquet_path()).expanduser().resolve()


def _default_output_root_for_bundle(model_bundle: Path) -> Path:
    vf = Path(model_bundle) / "model_version"
    mv = vf.read_text(encoding="utf-8").strip() if vf.is_file() else ""
    name = mv or Path(model_bundle).name.strip()
    if not name:
        raise ValueError(f"cannot derive default --output-dir: empty model bundle name ({model_bundle})")
    root = Path(th_config.DEFAULT_DEPLOY_OUTPUT_ROOT) / name
    return root.expanduser().resolve()


def _resolve_output_dir(args: argparse.Namespace, *, model_bundle: Path) -> Path:
    if args.output_dir is not None:
        return Path(args.output_dir).expanduser().resolve()
    return _default_output_root_for_bundle(model_bundle)


def _resolve_manifest_path(snapshot_src: Path) -> Path:
    p = Path(snapshot_src).expanduser().resolve()
    if p.is_file():
        if p.name != "active_manifest.json":
            raise ValueError(f"Expected active_manifest.json; got {p.name!r}")
        return p
    if p.is_dir():
        cand = p / "active_manifest.json"
        if not cand.is_file():
            raise FileNotFoundError(f"active_manifest.json not under {p}")
        return cand.resolve()
    raise FileNotFoundError(f"snapshot source not found: {snapshot_src}")


def _load_manifest_dict(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid manifest JSON {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"manifest root must be object; got {type(raw)}")
    return raw


def _rel_posix(root: Path, target: Path) -> str:
    rel = target.resolve().relative_to(root.resolve())
    return rel.as_posix()


def _copy_parquet_map(
    src: Path,
    *,
    dest_artifacts_dir: Path,
    manifest_parent: Path,
) -> str:
    dest = dest_artifacts_dir / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return _rel_posix(manifest_parent, dest)


def _maybe_copy_layer(
    key: str,
    raw_path: Any,
    *,
    dest_artifacts_dir: Path,
    manifest_parent: Path,
    strict: bool,
) -> tuple[str | None, Path | None]:
    if raw_path is None or raw_path == "":
        return None, None
    src = Path(str(raw_path)).expanduser().resolve()
    if not src.is_file():
        if key == "trial_bet_behavior_parquet" and not strict:
            logger.warning("[pack] trial parquet missing (non-strict): %s", src)
            return None, None
        if key in ("slow_patron_parquet", "adt_allowlist_parquet"):
            raise FileNotFoundError(f"manifest key {key!r} points to missing file: {src}")
        if strict:
            raise FileNotFoundError(f"manifest key {key!r} points to missing file: {src}")
        logger.warning("[pack] skip missing optional %s=%s", key, src)
        return None, None
    rel = _copy_parquet_map(src, dest_artifacts_dir=dest_artifacts_dir, manifest_parent=manifest_parent)
    return rel, src


def _verify_allowlist_training_hash_or_raise(
    training_metrics: dict[str, Any],
    allowlist_pack_path: Path | None,
) -> None:
    exp = training_metrics.get("adt_allowlist_sha256")
    if not exp or not str(exp).strip():
        return
    if allowlist_pack_path is None or not allowlist_pack_path.is_file():
        raise ValueError(
            "training_metrics has adt_allowlist_sha256 but packaged allowlist path missing "
            f"(expected usable file; training expects {str(exp).strip()[:16]}…)"
        )
    act = sha256_file(allowlist_pack_path)
    e = str(exp).strip().lower()
    a = act.strip().lower()
    if e == a:
        return
    raise ValueError(
        f"adt_allowlist SHA256 mismatch: training_metrics expects {e[:16]}… "
        f"but packaged allowlist at {allowlist_pack_path} has {a[:16]}…"
    )


def _stable_fingerprint_payload(
    *,
    model_version: str,
    manifest_version: str,
    allowlist_sha: str | None,
    slow_sha: str | None,
    mapping_sha: str | None,
) -> str:
    stable = {
        "model_version": model_version,
        "manifest_version": manifest_version,
        "adt_allowlist_sha256": allowlist_sha,
        "slow_patron_sha256": slow_sha,
        "canonical_mapping_sha256": mapping_sha,
    }
    blob = json.dumps(stable, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _write_bundle_info(
    path: Path,
    *,
    model_version: str,
    manifest_version: str,
    allowlist_sha: str | None,
    slow_patron_sha: str | None,
    canonical_mapping_sha: str | None,
    frozen_fingerprint_sha256: str,
    build_time_iso: str,
) -> None:
    payload = {
        "model_version": model_version,
        "manifest_version": manifest_version,
        "allowlist_sha256": allowlist_sha,
        "slow_patron_sha256": slow_patron_sha,
        "canonical_mapping_sha256": canonical_mapping_sha,
        "frozen_fingerprint_sha256": frozen_fingerprint_sha256,
        "build_time": build_time_iso,
        "high_adt_only_default": True,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_deploy_paths(
    path: Path,
    *,
    mapping_name: str,
) -> None:
    payload = {
        "schema_version": 1,
        "model_bundle_dir": "models",
        "snapshot_manifest_dir": "snapshots",
        "canonical_mapping_parquet": f"mapping/{mapping_name}",
        "local_state_dir": "local_state",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_readme(path: Path) -> None:
    body = """# trainer_hightier deploy bundle

## Layout

- `models/` — `model.pkl`, `training_metrics.json`, `model_version`
- `snapshots/active_manifest.json` — layer paths are relative to the `snapshots/` directory
- `snapshots/artifacts/` — Parquet layers referenced by the manifest
- `mapping/` — canonical mapping Parquet
- `local_state/` — runtime SQLite (`state.db`, `feature_state.db`)

## Prerequisites

Python env must be able to import `trainer` and `trainer_hightier` (same repo revision
as the build machine, or compatible install). From repo root::

  pip install -r requirements.txt

## Run (unified)

From **repository root** (so `trainer` resolves)::

  python -m trainer_hightier.deploy.main --bundle-dir /path/to/this/folder

Or use individual services after `deploy.main` documents path defaults.

## Rollback

Keep the previous bundle directory. Stop processes, swap symlink or folder,
restart from the previous `README_DEPLOY` / `bundle_info.json` pair; verify
`/health` and scorer boot logs (model_version / manifest / allowlist).
"""
    path.write_text(body, encoding="utf-8")


def _copy_requirements(dest: Path) -> None:
    src = _REPO_ROOT / "requirements.txt"
    if src.is_file():
        shutil.copy2(src, dest)
    else:
        dest.write_text(
            "# Fallback: pip install from repo pyproject / trainer deps\npandas\npyarrow\nnumpy\nflask\n",
            encoding="utf-8",
        )


def _ensure_model_bundle(model_src: Path, models_dest: Path, *, strict: bool) -> str:
    if not model_src.is_dir():
        raise NotADirectoryError(f"model source must be directory: {model_src}")
    pkl = model_src / "model.pkl"
    if not pkl.is_file():
        raise FileNotFoundError(f"model.pkl missing under {model_src}")
    models_dest.mkdir(parents=True, exist_ok=True)
    for child in model_src.iterdir():
        if child.is_file():
            shutil.copy2(child, models_dest / child.name)
    ver_path = models_dest / "model_version"
    if not strict and not ver_path.is_file():
        return "unknown"
    if ver_path.is_file():
        return ver_path.read_text(encoding="utf-8").strip() or "unknown"
    return "unknown"


def _read_training_metrics(models_dest: Path) -> dict[str, Any]:
    p = models_dest / "training_metrics.json"
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def build_deploy_package(argv: list[str] | None = None) -> Path:
    """Build deploy tree at resolved ``--output-dir`` (or default); return resolved output path."""

    args = _parse_args(argv)
    strict = bool(args.strict)
    model_bundle = _resolved_model_bundle_dir(
        model_source=args.model_source,
        model_version=args.model_version,
    )
    map_src = _canonical_mapping_origin(args)
    if not map_src.is_file():
        raise FileNotFoundError(f"mapping parquet missing: {map_src}")

    snap_origin = _snapshot_manifest_origin(args)
    manifest_path = _resolve_manifest_path(snap_origin)
    man = _load_manifest_dict(manifest_path)

    root = _resolve_output_dir(args, model_bundle=model_bundle)

    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"output dir must be empty or absent: {root}")
    root.mkdir(parents=True, exist_ok=True)
    models_dir = root / "models"
    snap_dir = root / "snapshots"
    art_dir = snap_dir / "artifacts"
    map_dir = root / "mapping"
    local_dir = root / "local_state"
    for d in (art_dir, map_dir, local_dir):
        d.mkdir(parents=True, exist_ok=True)

    mver = _ensure_model_bundle(model_bundle, models_dir, strict=strict)
    metrics = _read_training_metrics(models_dir)

    allow_pack_path: Path | None = None
    slow_pack_path: Path | None = None
    new_man = dict(man)
    for key in ("slow_patron_parquet", "trial_bet_behavior_parquet", "adt_allowlist_parquet"):
        rel, _src = _maybe_copy_layer(
            key,
            man.get(key),
            dest_artifacts_dir=art_dir,
            manifest_parent=snap_dir,
            strict=strict,
        )
        if rel is not None:
            new_man[key] = rel
        elif man.get(key):
            new_man.pop(key, None)

    slow_rel = new_man.get("slow_patron_parquet")
    if strict and (not slow_rel or not (snap_dir / slow_rel).is_file()):
        raise FileNotFoundError(
            f"strict pack requires slow_patron_parquet present in bundle; got {slow_rel!r}"
        )
    if isinstance(slow_rel, str) and slow_rel:
        slow_pack_path = (snap_dir / slow_rel).resolve()

    if strict and man.get("trial_bet_behavior_parquet") and not new_man.get("trial_bet_behavior_parquet"):
        raise FileNotFoundError(
            "strict pack requires copying trial_bet_behavior_parquet when declared in manifest; "
            "source file missing or unreadable."
        )

    if strict and not new_man.get("adt_allowlist_parquet"):
        raise ValueError(
            "strict pack requires manifest adt_allowlist_parquet (high_adt_only deploy); "
            f"manifest keys={sorted(new_man.keys())}"
        )
    al_rel = new_man.get("adt_allowlist_parquet")
    if isinstance(al_rel, str) and al_rel:
        allow_pack_path = (snap_dir / al_rel).resolve()

    map_name = map_src.name
    map_dest_path = map_dir / map_name
    shutil.copy2(map_src, map_dest_path)

    out_manifest = snap_dir / "active_manifest.json"
    out_manifest.write_text(json.dumps(new_man, indent=2), encoding="utf-8")

    allow_sha = sha256_file(allow_pack_path) if allow_pack_path and allow_pack_path.is_file() else None
    slow_sha = sha256_file(slow_pack_path) if slow_pack_path and slow_pack_path.is_file() else None
    map_sha = sha256_file(map_dest_path) if map_dest_path.is_file() else None
    _verify_allowlist_training_hash_or_raise(metrics, allow_pack_path)

    man_version = str(new_man.get("version", "") or man.get("version", "") or "unknown")
    build_time_iso = datetime.now(timezone.utc).isoformat()
    fingerprint = _stable_fingerprint_payload(
        model_version=mver,
        manifest_version=man_version,
        allowlist_sha=allow_sha,
        slow_sha=slow_sha,
        mapping_sha=map_sha,
    )
    _write_bundle_info(
        root / "bundle_info.json",
        model_version=mver,
        manifest_version=man_version,
        allowlist_sha=allow_sha,
        slow_patron_sha=slow_sha,
        canonical_mapping_sha=map_sha,
        frozen_fingerprint_sha256=fingerprint,
        build_time_iso=build_time_iso,
    )
    _write_deploy_paths(root / "deploy_bundle_paths.json", mapping_name=map_name)
    _write_readme(root / "README_DEPLOY.md")
    _copy_requirements(root / "requirements.txt")

    logger.info("[pack] wrote bundle %s", root)
    if args.archive:
        zip_path = root.parent / f"{root.name}.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in root.rglob("*"):
                if f.is_file():
                    arc = f.relative_to(root).as_posix()
                    zf.write(f, arcname=arc)
        logger.info("[pack] archive %s", zip_path)
    return root


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        build_deploy_package(argv)
    except Exception as exc:
        logger.error("[pack] failed: %s", exc)
        raise SystemExit(1) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
