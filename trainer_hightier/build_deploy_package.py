"""Build a portable ``trainer_hightier`` deploy folder (or .zip).

Layout (resolved ``--output-dir``, default ``trainer_hightier.config.DEFAULT_DEPLOY_OUTPUT_ROOT`` / bundle ``model_version``):

- ``main.py`` — standalone entrypoint (``python main.py`` after ``pip install -r requirements.txt``)
- ``wheels/trainer_hightier-*.whl`` — serving package wheel (``pip wheel --no-deps`` from ``trainer_hightier/pyproject.toml``)
- ``requirements.txt`` — local wheel line (transitive deps from PyPI / internal index per wheel metadata)
- ``.env.example`` — optional ClickHouse / log overrides for :mod:`trainer_hightier.deploy.main`
- ``models/`` — ``model.pkl``, ``training_metrics.json``, ``feature_candidate_registry.snapshot.yaml`` (frozen feature registry YAML from Step 5), ``model_version``, …
- ``snapshots/active_manifest.json`` — metadata-only audit manifest (no snapshot parquet paths)
- ``mapping/`` — canonical mapping Parquet + fixed ADT allowlist
- ``local_state/`` — empty dir for ``state.db`` / ``feature_state.db`` at runtime
- ``bundle_info.json``, ``deploy_bundle_paths.json``, ``README_DEPLOY.md``
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import hashlib

import pyarrow.parquet as pq

from trainer_hightier.core.model_bundle_paths import (
    DEPLOY_E2E_GATE_REPORT_FILENAME,
    resolve_model_bundle_dir,
)

from trainer_hightier import config as th_config
from trainer_hightier.config import (
    FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME,
    MANIFEST_KEY_FE_SHORT_TERM,
    MANIFEST_KEY_MID_TERM_SNAPSHOT,
    MID_TERM_BOOTSTRAP_SEED_PARQUET_BASENAME,
    MID_TERM_SNAPSHOT_DEPLOY_PARQUET_BASENAME,
    Step6ParityConfig,
    default_hightier_serving_config,
)
from trainer_hightier.serving.candidate_registry_loader import (
    CandidateRegistrySnapshot,
    load_candidate_registry,
)
from trainer_hightier.serving.adt_allowlist import sha256_file
from trainer_hightier.serving.feature_supply import (
    MANIFEST_KEY_FE_DERIVED,
    assert_feature_supplyability_or_raise,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FEAST_REPO_SRC = _REPO_ROOT / "trainer_hightier" / "feast_repo"
_ADT_ALLOWLIST_BUNDLE_BASENAME = "adt_allowed_players_q0p99.parquet"
_PARQUET_MANIFEST_KEYS: frozenset[str] = frozenset(
    {
        "slow_patron_parquet",
        "trial_bet_behavior_parquet",
        "adt_allowlist_parquet",
        MANIFEST_KEY_FE_DERIVED,
        MANIFEST_KEY_FE_SHORT_TERM,
        MANIFEST_KEY_MID_TERM_SNAPSHOT,
    }
)
_METADATA_MANIFEST_PASS_THROUGH: frozenset[str] = frozenset(
    {
        "version",
        "model_version",
        "coverage_end_exclusive",
        "training_cutoff_iso",
        "adt_allowlist_version",
        "slow_patron_grain",
        "mid_term_grain",
        "mid_term_anchor_gaming_day_max",
        "mid_term_coverage_end_exclusive",
        "mid_term_generated_at",
        "mid_term_stale_hard_cap_days",
        "slow_anchor_gaming_day_max",
        "slow_generated_at",
        "slow_monthly_grace_days",
        "slow_stale_hard_cap_days",
        "sha256_by_layer",
    }
)


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
            "Default: `<model-bundle>/deploy_inputs/` when present else "
            "default_hightier_serving_config().snapshot_manifest_dir."
        ),
    )
    pr.add_argument(
        "--mapping-source",
        type=Path,
        default=None,
        help=(
            "Canonical mapping Parquet file. Default: `<model-bundle>/deploy_inputs/"
            "canonical_player_mapping.parquet` when present else default_canonical_mapping_parquet_path()."
        ),
    )
    pr.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "Deploy bundle output directory (replaced when non-empty unless --no-overwrite). "
            "Default: trainer_hightier.config.DEFAULT_DEPLOY_OUTPUT_ROOT / <model_version> "
            "(repo default: out/deploy_hightier/<run-id>/)."
        ),
    )
    pr.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Remove existing output directory before packing (default: overwrite).",
    )
    pr.add_argument("--archive", action="store_true", help="Also write <output-dir-name>.zip beside output-dir.")
    pr.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fail when required artifacts are missing (default: strict).",
    )
    pr.add_argument(
        "--skip-step6-gate",
        action="store_true",
        help="Skip Step 6 train/serve parity gate (default: require passing gate in strict mode).",
    )
    pr.add_argument(
        "--skip-deploy-e2e-gate",
        action="store_true",
        help="Skip deploy E2E gate report check (default: require pass in strict mode).",
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


def _snapshot_manifest_origin(args: argparse.Namespace, *, model_bundle: Path) -> Path:
    if args.snapshot_manifest_source is not None:
        return Path(args.snapshot_manifest_source).expanduser().resolve()
    bundled = Path(model_bundle).resolve() / "deploy_inputs"
    if (bundled / "active_manifest.json").is_file():
        return bundled.resolve()
    srv = Path(default_hightier_serving_config().snapshot_manifest_dir).expanduser().resolve()
    if not srv.is_dir():
        raise FileNotFoundError(
            "default snapshot manifest dir missing (-directory); expected "
            f"{srv} (--snapshot-manifest-source to override). Run snapshot updater or pass an explicit manifest path."
        )
    return srv


def _canonical_mapping_origin(args: argparse.Namespace, *, model_bundle: Path) -> Path:
    if args.mapping_source is not None:
        return Path(args.mapping_source).expanduser().resolve()
    bundled = Path(model_bundle).resolve() / "deploy_inputs" / "canonical_player_mapping.parquet"
    if bundled.is_file():
        return bundled.resolve()
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


def _copy_parquet_sidecars(src: Path, *, dest_artifacts_dir: Path) -> None:
    """Copy optional metadata sidecars next to a packaged parquet artifact."""

    for suffix in (".meta.json", ".production_meta.json"):
        side = src.parent / f"{src.stem}{suffix}"
        if side.is_file():
            shutil.copy2(side, dest_artifacts_dir / side.name)


def _maybe_copy_layer(
    key: str,
    raw_path: Any,
    *,
    dest_artifacts_dir: Path,
    manifest_parent: Path,
    manifest_basis_dir: Path,
    strict: bool,
) -> tuple[str | None, Path | None]:
    if raw_path is None or raw_path == "":
        return None, None
    p = Path(str(raw_path)).expanduser()
    if p.is_absolute():
        src = p.resolve()
    else:
        src = (Path(manifest_basis_dir).resolve() / p).resolve()
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
    if key in (MANIFEST_KEY_FE_SHORT_TERM, MANIFEST_KEY_MID_TERM_SNAPSHOT, MANIFEST_KEY_FE_DERIVED):
        _copy_parquet_sidecars(src, dest_artifacts_dir=dest_artifacts_dir)
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


def _model_feature_columns_from_pickle(models_dir: Path) -> tuple[str, ...]:
    """Ordered feature names stored in bundled ``model.pkl``."""

    pkl = Path(models_dir) / "model.pkl"
    raw = pickle.loads(pkl.read_bytes())
    if not isinstance(raw, dict):
        raise ValueError(f"{pkl}: expected dict pickle payload")
    feat = raw.get("feature_columns") or raw.get("feature_cols")
    if not feat:
        raise ValueError(f"{pkl} missing feature_columns/feature_cols")
    return tuple(str(x) for x in list(feat))


def _parquet_lower_column_index(path: Path) -> dict[str, str]:
    """Map lower-case Parquet column name -> original spelling."""

    names = pq.read_schema(path).names
    return {str(c).lower(): str(c) for c in names}


def _ensure_parquet_columns(path: Path, *, role: str, required: Iterable[str]) -> None:
    """Raise when ``path`` is missing required column spellings."""

    idx = _parquet_lower_column_index(path)
    miss = sorted({c for c in required if c.lower() not in idx})
    if miss:
        sample = sorted(idx.keys())
        tip = ", ".join(sample[:50])
        ellipsis = "" if len(sample) <= 50 else ", …"
        raise ValueError(
            f"[pack-schema] {role} parquet missing columns {miss} at {path}. "
            f"schema(lowercase-sample)=[{tip}{ellipsis}]"
        )


def _static_slow_minimum_contract_pack(slow_pack_path: Path) -> None:
    """Structural gate for bundled slow parquet (trainer_hightier materialization).

    Two supported shapes:

    - **Canonical active-month (production/training)** — ``canonical_id`` + ``anchor_gaming_day`` +
      ``event_timestamp`` (see ``contracts/slow_patron_180d_monthly_features.yaml``).
    - **Bet-grain (diagnostic only)** — one row per ``bet_id`` with PIT timestamps; must not ship in deploy.
    - **Legacy player-grain snapshot** — used by tests / older tooling: ``player_id`` + calendar anchor.
    """

    slow_idx = _parquet_lower_column_index(slow_pack_path)
    canonical_snapshot = ("canonical_id" in slow_idx) and ("anchor_gaming_day" in slow_idx) and (
        "bet_id" not in slow_idx
    )
    bet_snapshot = ("bet_id" in slow_idx) and ("prediction_visible_ts_cf" in slow_idx)
    player_snapshot = ("player_id" in slow_idx) and (
        ("anchor_gaming_day" in slow_idx) or ("gaming_day" in slow_idx)
    )

    if canonical_snapshot and not bet_snapshot:
        _ensure_parquet_columns(
            slow_pack_path,
            role="slow_patron",
            required=(
                "canonical_id",
                "anchor_gaming_day",
                "event_timestamp",
                "patron__theo_win_sum__w180d_m1snap",
                "patron__gaming_days_cnt__w180d_m1snap",
                "patron__adt__w180d_m1snap",
            ),
        )
        return

    if bet_snapshot and not player_snapshot and not canonical_snapshot:
        _ensure_parquet_columns(
            slow_pack_path,
            role="slow_patron",
            required=(
                "bet_id",
                "prediction_visible_ts_cf",
            ),
        )
        etl_hit = [
            cn
            for cn in sorted(slow_idx.keys())
            if "etl_insert" in cn and "synthetic" in cn
        ]
        if not etl_hit:
            sample = sorted(slow_idx.keys())
            tip = ", ".join(sample[:50])
            ellipsis = "" if len(sample) <= 50 else ", …"
            raise ValueError(
                "[pack-schema] slow_patron (bet-grain) parquet missing Feast created-timestamp "
                "(expected a column akin to '__etl_insert_Dtm_synthetic'). "
                f"schema(lowercase-sample)=[{tip}{ellipsis}] — file={slow_pack_path}"
            )
        return

    if player_snapshot and not bet_snapshot:
        _ensure_parquet_columns(slow_pack_path, role="slow_patron", required=("player_id",))
        if "anchor_gaming_day" not in slow_idx and "gaming_day" not in slow_idx:
            raise ValueError(
                "[pack-schema] slow parquet (player-grain) must expose anchor_gaming_day or gaming_day "
                f"for legacy ASOF join; got {slow_pack_path}"
            )
        return

    sample = sorted(slow_idx.keys())
    tip = ", ".join(sample[:50])
    ellipsis = "" if len(sample) <= 50 else ", …"
    raise ValueError(
        "[pack-schema] slow_patron parquet unsupported structure: "
        "need canonical active-month (canonical_id + anchor_gaming_day + event_timestamp + patron__*), "
        "diagnostic bet-grain (bet_id + prediction_visible_ts_cf + __etl_insert_*synthetic*), "
        "or legacy player snapshot (player_id + gaming_day|anchor_gaming_day); "
        f"schema(lowercase-sample)=[{tip}{ellipsis}] — file={slow_pack_path}"
    )


def _static_parquet_minimum_contracts(
    *,
    strict: bool,
    slow_pack_path: Path | None,
    map_dest_path: Path,
    allow_pack_path: Path | None,
    feast_bundle: bool = False,
) -> None:
    """Structural gates independent of frozen registry (keys + slow anchor)."""

    # Legacy bundles may omit ``casino_player_id``; serving falls back to ``canonical_id``.
    _ensure_parquet_columns(map_dest_path, role="canonical_mapping", required=("player_id", "canonical_id"))
    if allow_pack_path is not None and allow_pack_path.is_file():
        _ensure_parquet_columns(allow_pack_path, role="adt_allowlist", required=("player_id",))
    if slow_pack_path is None or not slow_pack_path.is_file():
        if strict and not feast_bundle:
            raise FileNotFoundError("[pack-schema] slow patron parquet unresolved for static gate")
        return
    _static_slow_minimum_contract_pack(slow_pack_path)


def _resolve_manifest_layer_file(raw_path: object, *, manifest_basis_dir: Path) -> Path | None:
    """Resolve a manifest parquet path (absolute or relative to manifest parent)."""
    if raw_path is None or raw_path == "":
        return None
    p = Path(str(raw_path)).expanduser()
    if p.is_absolute():
        src = p.resolve()
    else:
        src = (Path(manifest_basis_dir).resolve() / p).resolve()
    return src if src.is_file() else None


def _resolve_allowlist_source_for_pack(
    man: dict[str, Any],
    *,
    manifest_basis_dir: Path,
    model_bundle: Path,
    strict: bool,
) -> Path | None:
    """Locate ADT allowlist parquet for Feast-only bundle (never copied under snapshots/)."""
    raw = man.get("adt_allowlist_parquet")
    if raw:
        found = _resolve_manifest_layer_file(raw, manifest_basis_dir=manifest_basis_dir)
        if found is not None:
            return found
    deploy_inputs = Path(model_bundle).resolve() / "deploy_inputs"
    for candidate in (
        deploy_inputs / _ADT_ALLOWLIST_BUNDLE_BASENAME,
        deploy_inputs / "adt_allowlist.parquet",
    ):
        if candidate.is_file():
            return candidate.resolve()
    if isinstance(raw, str) and raw.strip():
        p = Path(raw.strip())
        if p.is_file():
            return p.resolve()
    if strict:
        raise FileNotFoundError(
            "[pack] ADT allowlist parquet missing for Feast-only bundle; "
            "expected deploy_inputs allowlist or resolvable manifest adt_allowlist_parquet"
        )
    return None


def _build_metadata_only_manifest(
    source_man: dict[str, Any],
    *,
    model_version: str,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """Build M1 metadata-only manifest (no ``*_parquet`` layer paths)."""
    out: dict[str, Any] = {}
    for key in _METADATA_MANIFEST_PASS_THROUGH:
        if key in source_man and source_man[key] is not None:
            out[key] = source_man[key]
    out["version"] = str(source_man.get("version") or model_version)
    out["model_version"] = str(source_man.get("model_version") or model_version)
    if "coverage_end_exclusive" not in out:
        out["coverage_end_exclusive"] = datetime.now(timezone.utc).isoformat()
    cutoff = metrics.get("training_cutoff_iso") or source_man.get("training_cutoff_iso")
    if cutoff and "training_cutoff_iso" not in out:
        out["training_cutoff_iso"] = str(cutoff)
    for key in list(out.keys()):
        if key.endswith("_parquet") or key in _PARQUET_MANIFEST_KEYS:
            out.pop(key, None)
    return out


def _rewrite_feast_feature_store_yaml(feast_repo: Path) -> None:
    """Point Feast offline staging at bundle-local ``artifacts/feast``."""
    yaml_path = feast_repo / "feature_store.yaml"
    if not yaml_path.is_file():
        return
    text = yaml_path.read_text(encoding="utf-8")
    text = text.replace(
        "staging_location: ../tmp/feast_duckdb_staging",
        "staging_location: ../artifacts/feast/duckdb_staging",
    )
    yaml_path.write_text(text, encoding="utf-8")


def _copy_feast_repo_to_bundle(bundle_root: Path) -> Path:
    """Copy ``trainer_hightier/feast_repo`` into deploy bundle."""
    if not _FEAST_REPO_SRC.is_dir():
        raise FileNotFoundError(f"Feast repo missing at {_FEAST_REPO_SRC}")
    dest = bundle_root / "feast_repo"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        _FEAST_REPO_SRC,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "registry.db", "online_store.db"),
    )
    (dest / "data").mkdir(parents=True, exist_ok=True)
    _rewrite_feast_feature_store_yaml(dest)
    from trainer_hightier.serving.feast_online_adapter import reset_feast_repo_runtime_state

    reset_feast_repo_runtime_state(dest)
    return dest


def _load_and_verify_frozen_registry(
    *,
    models_dir: Path,
    metrics: dict[str, Any],
    strict: bool,
) -> CandidateRegistrySnapshot | None:
    """Parse frozen YAML next to ``model.pkl`` when present; optionally verify SHA-256."""

    snap_p = Path(models_dir) / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    if not snap_p.is_file():
        if strict:
            raise FileNotFoundError(
                f"[pack-schema] missing {FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME} under models dir {models_dir}; "
                "retrain or copy snapshot from Step 5 bundle. Use --no-strict to bypass dynamic gate temporarily."
            )
        logger.warning(
            "[pack-schema] skipping dynamic feature gate (%s absent); rebuild with recent trainer.",
            FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME,
        )
        return None
    act = sha256_file(snap_p)
    expected = metrics.get("feature_candidate_registry_sha256") if metrics else None
    if expected and isinstance(expected, str) and expected.strip():
        e = expected.strip().lower()
        if e != act.strip().lower():
            raise ValueError(
                f"[pack-schema] feature registry SHA mismatch: training_metrics expects {e[:16]}… "
                f"but models/{FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME} hashes to {act[:16]}…"
            )
    return load_candidate_registry(snap_p)


def _dynamic_registry_parquet_gate(
    snap: CandidateRegistrySnapshot,
    *,
    model_feats: tuple[str, ...],
    slow_pack_path: Path | None,
    trial_pack_path: Path | None,
    feast_only: bool = True,
) -> None:
    """Ensure Parquet-backed features align with frozen registry rows × model.pkl columns."""

    by_id = {r.feature_id: r for r in snap.rows}
    skip_sources = frozenset({"baseline_model"})
    for feat in model_feats:
        row = by_id.get(feat)
        if row is None:
            raise ValueError(
                f"[pack-schema] model.pkl lists feature_columns={feat!r} "
                "not present in frozen feature_candidate_registry snapshot"
            )
        src = row.source
        if src in skip_sources:
            continue
        if src == "fe_derived":
            continue
        if src == "feast_slow_180d":
            if feast_only:
                continue
            if slow_pack_path is None or not slow_pack_path.is_file():
                raise FileNotFoundError(f"[pack-schema] model expects {feat!r} (feast_slow_180d) but slow parquet missing")
            _ensure_parquet_columns(slow_pack_path, role="slow_patron", required=(feat,))
        elif src == "feast_trial_1h":
            if trial_pack_path is not None and trial_pack_path.is_file():
                _ensure_parquet_columns(trial_pack_path, role="trial_bet_behavior_1h", required=(feat,))
            else:
                logger.info(
                    "[pack-schema] feast_trial_1h feature %s: no trial parquet in bundle (trial features recomputed serving-side)",
                    feat,
                )
        else:
            logger.warning("[pack-schema] unknown registry source=%r for %s; skipping Parquet gate", src, feat)


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
    feature_candidate_registry_sha256: str | None = None,
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
    if feature_candidate_registry_sha256 is not None:
        payload["feature_candidate_registry_sha256"] = feature_candidate_registry_sha256
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_deploy_paths(
    path: Path,
    *,
    mapping_name: str,
    adt_allowlist_basename: str = _ADT_ALLOWLIST_BUNDLE_BASENAME,
) -> None:
    payload = {
        "schema_version": 2,
        "model_bundle_dir": "models",
        "snapshot_manifest_dir": "snapshots",
        "canonical_mapping_parquet": f"mapping/{mapping_name}",
        "local_state_dir": "local_state",
        "feast_repo_dir": "feast_repo",
        "feast_artifacts_dir": "artifacts/feast",
        "feast_readiness_path": "artifacts/feast/feast_online_readiness.json",
        "adt_allowlist_parquet": f"mapping/{adt_allowlist_basename}",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_readme(path: Path) -> None:
    body = """# trainer_hightier deploy bundle (standalone, scorer v2 Feast)

## Layout (no repo checkout required)

- `main.py` — unified entrypoint (`python main.py --mode all`)
- `collect_diag.py` — incident debug bundle collector (`python collect_diag.py`)
- `wheels/` — `trainer_hightier` wheel (install from this directory)
- `requirements.txt` — local wheel + PyPI / internal-index deps (must install `feast` for refresh)
- `.env.example` — copy to `.env`; set ClickHouse credentials
- `models/` — `model.pkl`, `training_metrics.json`, frozen registry snapshot, `model_version`
- `mapping/` — canonical mapping Parquet + fixed ADT allowlist (`adt_allowed_players_q0p99.parquet`)
- `snapshots/` — metadata-only `active_manifest.json` (audit; **no** snapshot parquet layers)
- `feast_repo/` — Feast definitions + bundle-local online store / registry paths (required for scorer v2)
- `artifacts/feast/` — writable Feast readiness + refresh reports (bundle-local paths)
- `local_state/` — `state.db`, `feature_state.db`, `prediction_log.db`, `logs/deploy_main.log`

## Logging

`python main.py` writes logs to **both** the current CMD window and a bundle-local file. No shell redirect (`> file 2>&1`) is required.

- Default log file: `local_state/logs/deploy_main.log` (append mode)
- Format: `%(asctime)s %(levelname)s %(name)s: %(message)s`
- If the log file cannot be created (permissions/disk), deploy continues with console logging only

Tail the log on Windows (PowerShell, separate window):

```powershell
Get-Content -Path local_state\\logs\\deploy_main.log -Wait -Tail 50
```

## Incident debug bundle

From **this bundle root** (requires ClickHouse for full audits; zip is still produced on partial failure):

```cmd
python collect_diag.py
```

Output: `local_state/diag_exports/prod_diag_<model_version>_<timestamp>.zip`

The zip includes SQLite Parquet exports (`prediction_log`, `state`, `feature_state`), audit reports, identity/runtime files, and `deploy_main.log`. When MLflow credentials are configured under `local_state/mlflow.env`, the zip is uploaded to the training run matching `models/model_version` (upload failure does not block zip creation). Only the latest 3 zip files are kept locally.

Optional flags: `--skip-mlflow-upload`, `--output-zip PATH`.

## Prerequisites

- Python 3.9+
- ClickHouse reachable from the production host (credentials in `.env`)
- `pip install -r requirements.txt` using PyPI or an internal package index
- `feast` CLI on PATH (used by startup refresh)

## Install

```bash
cd /path/to/this/bundle
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
# Set at minimum: CH_USER, CH_PASS (or CH_PASSWORD)
```

Offline / fully vendored third-party wheels are optional backup SOP. The first production slice assumes the host can
install transitive dependencies from PyPI or an internal index because the final production OS / architecture may vary.

## Run (scorer v2 production)

From **this bundle root**:

```bash
python main.py --mode all
```

Default behavior for `mode=all` / `mode=scorer`:

1. Preflight model, mapping, allowlist, `feast_repo`
2. Resolve Feast repo / registry / online-store paths bundle-locally
3. If Feast readiness missing/stale (or `--force-feast-refresh`): acquire a short-timeout lock and run **startup Feast online refresh** from ClickHouse
4. Persist latest readiness payload/hash in `feature_state.db`, then atomically publish `artifacts/feast/feast_online_readiness.json`
5. Run deploy Feast readiness + allowlist online smoke
6. Start **Feast refresh supervisor** daemon (default; mid+slow eligibility poll every 300s; fail-soft on errors)
7. Start API (background), validator (background), scorer (foreground)

If lock acquisition, refresh, readiness persistence/publish, or smoke fails, startup **aborts** (no partial scorer).

Flags:

- `--no-feast-startup-refresh` — skip startup refresh (debug only; scorer will likely fail readiness gate)
- `--force-feast-refresh` — force refresh even if readiness looks fresh
- `--no-feast-refresh-supervisor` — disable post-startup daemon (debug; use only if external cron owns refresh)
- `--host` / `--port` — API bind address

Do **not** run external cron refresh concurrently with the in-process supervisor (same bundle lock).

Manual refresh (ops fallback):

```bash
python -m trainer_hightier.serving.feast_online_refresh \\
  --source clickhouse --layers mid,slow \\
  --adt-allowlist mapping/adt_allowed_players_q0p99.parquet \\
  --canonical-mapping mapping/canonical_player_mapping.parquet
```

Supervisor observability: `feature_state.db` → `feature_state_meta` keys `feast_refresh_supervisor_last_check_iso`,
`feast_refresh_supervisor_last_attempt_iso`, `feast_refresh_supervisor_last_success_iso`.

## Legacy note

Older docs may describe Parquet snapshot refresh supervisor as the primary refresh path. Scorer v2 adopted path is **Feast online**, not manifest Parquet fallback at score time.

## Rollback

Keep previous bundle; stop processes; swap directory; restart `python main.py --mode all`.
"""
    path.write_text(body, encoding="utf-8")


def _clean_trainer_hightier_staging_build_dir() -> None:
    """Remove package-local ``build/`` so setuptools/PyPI tooling never packs deep ``build/lib`` trees.

    Mitigates Windows MAX_PATH (~260) failures when ``pip`` unpacks wheels under long venv paths.
    """
    pkg_root = _REPO_ROOT / "trainer_hightier"
    staging = pkg_root / "build"
    if staging.is_dir():
        shutil.rmtree(staging)
        logger.info(
            "[pack] removed %s before pip wheel (Phase B: exclude setuptools staging from artifact)",
            staging,
        )


def _build_trainer_hightier_wheel(*, wheels_dir: Path) -> str:
    """Build ``trainer_hightier`` wheel into *wheels_dir*; return wheel filename."""
    wheels_dir.mkdir(parents=True, exist_ok=True)
    pkg_root = _REPO_ROOT / "trainer_hightier"
    pyproj = pkg_root / "pyproject.toml"
    if not pyproj.is_file():
        raise FileNotFoundError(f"trainer_hightier pyproject missing: {pyproj}")
    _clean_trainer_hightier_staging_build_dir()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "-w", str(wheels_dir), "--no-deps"],
        check=True,
        cwd=str(pkg_root),
    )
    matches = sorted(wheels_dir.glob("trainer_hightier-*.whl"))
    if not matches:
        raise FileNotFoundError(f"No trainer_hightier wheel found in {wheels_dir}")
    whl_path = matches[-1]
    logger.info(
        "[pack] serving wheel written %s (%s bytes)",
        whl_path.name,
        whl_path.stat().st_size,
    )
    return whl_path.name


def _write_bundle_collect_diag_py(path: Path) -> None:
    """Write bundle root ``collect_diag.py`` for one-click incident debug bundle."""
    path.write_text(
        '''"""Standalone incident debug bundle entry."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    """Collect debug zip with ``--bundle-dir`` fixed to this directory."""
    root = Path(__file__).resolve().parent
    argv = ["--bundle-dir", str(root), *sys.argv[1:]]
    from trainer_hightier.serving.collect_debug_bundle import run_collect_debug_bundle

    return int(run_collect_debug_bundle(argv))


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )


def _write_bundle_main_py(path: Path) -> None:
    """Write bundle root ``main.py`` that delegates to :mod:`trainer_hightier.deploy.main`."""
    path.write_text(
        '''"""Standalone bundle entry: ``pip install -r requirements.txt`` then ``python main.py``."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    """Run deploy with ``--bundle-dir`` fixed to this directory."""
    root = Path(__file__).resolve().parent
    argv = ["--bundle-dir", str(root), *sys.argv[1:]]
    from trainer_hightier.deploy.main import main as deploy_main

    return int(deploy_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )


def _write_dotenv_example(path: Path) -> None:
    """Write ``.env.example`` beside bundle ``main.py`` (optional overrides on target)."""
    path.write_text(
        """# =============================================================================
# trainer_hightier deploy — copy to ".env" beside main.py (optional).
# Overrides defaults in trainer_hightier.config.HightierServingConfig when set.
# =============================================================================

# CH_HOST=gdpedw
# CH_PORT=8123
CH_USER=
CH_PASS=
# CH_PASSWORD=

# CH_SECURE=false
# SOURCE_DB=GDP_GMWDS_Raw

# DEPLOY_LOG_LEVEL=INFO
""",
        encoding="utf-8",
    )


def _write_standalone_requirements(dest: Path, *, wheel_filename: str) -> None:
    """Write ``requirements.txt`` with a relative wheel path and comment header."""
    wheel_line = f"wheels/{wheel_filename}"
    dest.write_text(
        "# trainer_hightier standalone: local wheel; transitive deps from PyPI/internal index (wheel metadata).\n"
        "# Run from THIS directory (bundle root): pip install -r requirements.txt\n"
        f"{wheel_line}\n",
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


_STEP6_PARITY_JSON = "feature_parity_verification.json"


def _assert_step6_parity_gate_or_raise(
    model_bundle: Path,
    *,
    strict: bool,
    skip_step6_gate: bool,
    parity_cfg: Step6ParityConfig | None = None,
) -> None:
    """Require Step 6 parity artifact and enforce configured hard-fail gates."""
    if skip_step6_gate or not strict:
        if skip_step6_gate:
            logger.warning("[pack-step6] skipping train/serve parity gate (--skip-step6-gate)")
        return
    cfg = parity_cfg or Step6ParityConfig()
    parity_path = Path(model_bundle) / _STEP6_PARITY_JSON
    if not parity_path.is_file():
        raise FileNotFoundError(
            f"[pack-step6] missing {_STEP6_PARITY_JSON} under {model_bundle}; "
            "retrain with Step 6 enabled or pass --skip-step6-gate (non-production only)."
        )
    try:
        report = json.loads(parity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"[pack-step6] invalid {_STEP6_PARITY_JSON}: {parity_path}") from exc
    if not isinstance(report, dict):
        raise ValueError(f"[pack-step6] expected JSON object in {parity_path}")
    slow_failed = int(report.get("n_failed_slow_gate", 0))
    all_failed = int(report.get("n_failed_all_feature_gate", 0))
    if cfg.hard_fail_slow_gate and slow_failed > 0:
        raise ValueError(
            f'[pack-step6] slow-feature parity gate failed '
            f"(n_failed_slow_gate={slow_failed}); see {parity_path}"
        )
    if cfg.hard_fail_all_feature_gate and all_failed > 0:
        raise ValueError(
            '[pack-step6] all-feature parity gate failed '
            f"(n_failed_all_feature_gate={all_failed}); see {parity_path}"
        )
    logger.info(
        "[pack-step6] parity gate passed (slow_failed=%s all_feature_failed=%s)",
        slow_failed,
        all_failed,
    )


def _assert_step6_deploy_e2e_gate_or_raise(
    model_bundle: Path,
    *,
    strict: bool,
    skip_deploy_e2e_gate: bool,
) -> None:
    """Require deploy E2E report with ``verdict=pass`` beside the model bundle."""
    if skip_deploy_e2e_gate or not strict:
        if skip_deploy_e2e_gate:
            logger.warning("[pack-step6] skipping deploy E2E gate (--skip-deploy-e2e-gate)")
        return
    report_path = Path(model_bundle) / DEPLOY_E2E_GATE_REPORT_FILENAME
    if not report_path.is_file():
        raise FileNotFoundError(
            f"[pack-step6] missing {DEPLOY_E2E_GATE_REPORT_FILENAME} under {model_bundle}; "
            "retrain with Step 6 deploy E2E enabled or pass --skip-deploy-e2e-gate (non-production only)."
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"[pack-step6] invalid {DEPLOY_E2E_GATE_REPORT_FILENAME}: {report_path}") from exc
    if not isinstance(report, dict):
        raise ValueError(f"[pack-step6] expected JSON object in {report_path}")
    verdict = str(report.get("verdict") or "fail")
    if verdict != "pass":
        raise ValueError(
            f"[pack-step6] deploy E2E gate failed (verdict={verdict!r}); see {report_path}",
        )
    logger.info("[pack-step6] deploy E2E gate passed (verdict=pass)")


def _copy_bootstrap_mid_seed(
    model_bundle: Path,
    feast_art_dir: Path,
    metrics: dict[str, Any],
) -> Path | None:
    """Best-effort copy training mid snapshot for Feast bootstrap seed."""
    mb = Path(model_bundle).resolve()
    candidates: list[Path] = []
    metric_path = metrics.get("main_trainer_mid_term_snapshot_parquet")
    if isinstance(metric_path, str) and metric_path.strip():
        candidates.append(Path(metric_path.strip()).expanduser())
    candidates.extend(
        [
            mb / "_main_trainer_mid_term_daily_snapshot.parquet",
            mb / "deploy_inputs" / MID_TERM_SNAPSHOT_DEPLOY_PARQUET_BASENAME,
        ]
    )
    for src in candidates:
        resolved = src.resolve()
        if resolved.is_file():
            dest = feast_art_dir / MID_TERM_BOOTSTRAP_SEED_PARQUET_BASENAME
            shutil.copy2(resolved, dest)
            logger.info("[pack] copied mid bootstrap seed -> %s", dest.name)
            return dest
    logger.warning("[pack] no training mid snapshot found for Feast bootstrap seed")
    return None


def build_deploy_package(argv: list[str] | None = None) -> Path:
    """Build deploy tree at resolved ``--output-dir`` (or default); return resolved output path."""

    args = _parse_args(argv)
    strict = bool(args.strict)
    model_bundle = _resolved_model_bundle_dir(
        model_source=args.model_source,
        model_version=args.model_version,
    )
    _assert_step6_parity_gate_or_raise(
        model_bundle,
        strict=strict,
        skip_step6_gate=bool(args.skip_step6_gate),
    )
    _assert_step6_deploy_e2e_gate_or_raise(
        model_bundle,
        strict=strict,
        skip_deploy_e2e_gate=bool(args.skip_deploy_e2e_gate),
    )
    map_src = _canonical_mapping_origin(args, model_bundle=model_bundle)
    if not map_src.is_file():
        raise FileNotFoundError(f"mapping parquet missing: {map_src}")

    snap_origin = _snapshot_manifest_origin(args, model_bundle=model_bundle)
    manifest_path = _resolve_manifest_path(snap_origin)
    man = _load_manifest_dict(manifest_path)

    root = _resolve_output_dir(args, model_bundle=model_bundle)

    if root.exists() and any(root.iterdir()):
        if not args.overwrite:
            raise FileExistsError(
                f"output dir must be empty or absent (or pass --overwrite): {root}"
            )
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    _copy_feast_repo_to_bundle(root)
    wheels_dir = root / "wheels"
    wheel_name = _build_trainer_hightier_wheel(wheels_dir=wheels_dir)
    _write_bundle_main_py(root / "main.py")
    _write_bundle_collect_diag_py(root / "collect_diag.py")
    _write_dotenv_example(root / ".env.example")
    _write_standalone_requirements(root / "requirements.txt", wheel_filename=wheel_name)
    models_dir = root / "models"
    snap_dir = root / "snapshots"
    art_dir = snap_dir / "artifacts"
    map_dir = root / "mapping"
    local_dir = root / "local_state"
    feast_art_dir = root / "artifacts" / "feast"
    for d in (art_dir, map_dir, local_dir, feast_art_dir):
        d.mkdir(parents=True, exist_ok=True)

    mver = _ensure_model_bundle(model_bundle, models_dir, strict=strict)
    metrics = _read_training_metrics(models_dir)
    _copy_bootstrap_mid_seed(model_bundle, feast_art_dir, metrics)

    allow_src = _resolve_allowlist_source_for_pack(
        man,
        manifest_basis_dir=manifest_path.parent,
        model_bundle=model_bundle,
        strict=strict,
    )

    map_name = map_src.name
    map_dest_path = map_dir / map_name
    shutil.copy2(map_src, map_dest_path)

    fixed_allow_path = map_dir / _ADT_ALLOWLIST_BUNDLE_BASENAME
    if allow_src is not None and allow_src.is_file():
        if allow_src.resolve() != fixed_allow_path.resolve():
            shutil.copy2(allow_src, fixed_allow_path)
        allow_pack_path = fixed_allow_path
    elif strict:
        raise ValueError(
            "strict pack requires ADT allowlist parquet for Feast scorer v2 bundle"
        )
    else:
        allow_pack_path = None

    metadata_manifest = _build_metadata_only_manifest(man, model_version=mver, metrics=metrics)
    if allow_pack_path is not None and allow_pack_path.is_file():
        al_sha = sha256_file(allow_pack_path)
        exp_ver = str(man.get("adt_allowlist_version") or "").strip()
        metadata_manifest["adt_allowlist_version"] = exp_ver or al_sha[:16]

    model_cols = _model_feature_columns_from_pickle(models_dir)
    _static_parquet_minimum_contracts(
        strict=strict,
        slow_pack_path=None,
        map_dest_path=map_dest_path,
        allow_pack_path=allow_pack_path,
        feast_bundle=True,
    )
    frozen_snap = _load_and_verify_frozen_registry(models_dir=models_dir, metrics=metrics, strict=strict)
    if frozen_snap is not None:
        _dynamic_registry_parquet_gate(
            frozen_snap,
            model_feats=model_cols,
            slow_pack_path=None,
            trial_pack_path=None,
            feast_only=True,
        )
        assert_feature_supplyability_or_raise(
            frozen_snap,
            model_cols,
            slow_pack_path=None,
            trial_pack_path=None,
            fe_pack_path=None,
            fe_short_term_pack_path=None,
            mid_term_pack_path=None,
            manifest=metadata_manifest,
            validation_stage="package",
            scorer_v2_feast_mode=True,
        )

    out_manifest = snap_dir / "active_manifest.json"
    out_manifest.write_text(json.dumps(metadata_manifest, indent=2), encoding="utf-8")

    allow_sha = sha256_file(allow_pack_path) if allow_pack_path and allow_pack_path.is_file() else None
    slow_sha = None
    map_sha = sha256_file(map_dest_path) if map_dest_path.is_file() else None
    _verify_allowlist_training_hash_or_raise(metrics, allow_pack_path)

    reg_sha_metric = metrics.get("feature_candidate_registry_sha256") if metrics else None
    reg_sha_s = reg_sha_metric.strip() if isinstance(reg_sha_metric, str) and reg_sha_metric.strip() else None

    man_version = str(metadata_manifest.get("version", "") or "unknown")
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
        feature_candidate_registry_sha256=reg_sha_s,
    )
    _write_deploy_paths(root / "deploy_bundle_paths.json", mapping_name=map_name)
    _write_readme(root / "README_DEPLOY.md")

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
