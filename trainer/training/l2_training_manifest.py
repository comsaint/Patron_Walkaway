"""L2 pre-assembled training bundle manifest (GitHub #16 TRN-16-03 / #17 TRN-17-01).

Reads ``l2_training_bundle.json`` under a bundle directory, validates snapshot lineage,
resolves parquet paths, and provides split-bytes OOM estimates (TRN-16-04).

``schema_version`` **1** keeps three monolithic split paths.  **2** adds
``split_day_manifest`` (per-day shard files) while retaining monolithic paths for
compatibility; export paths prefer day shards when present.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from trainer.training.l2_trainer_contracts import (
    OOM_ESTIMATE_STRATEGY_L2_SPLIT_FILES,
    SNAPSHOT_ID_ALIASES,
)

logger = logging.getLogger(__name__)

L2_TRAINING_BUNDLE_MANIFEST_FILE: str = "l2_training_bundle.json"


@dataclass(frozen=True)
class L2TrainingBundleManifest:
    """Validated training bundle contract."""

    bundle_dir: Path
    schema_version: str
    train_path: Path
    valid_path: Path
    test_path: Path
    train_export_paths: Tuple[Path, ...]
    valid_export_paths: Tuple[Path, ...]
    test_export_paths: Tuple[Path, ...]
    source_snapshot_id: str
    l2_snapshot_id: str
    train_end: str
    window_start: str
    window_end: str
    valid_full_unsampled: bool
    test_full_unsampled: bool
    train_sampling_applied: bool
    identity_mapping_mode: str
    label_asset_meta: Optional[Dict[str, Any]]
    per_feature_fingerprints: Optional[Dict[str, str]]
    split_calendar: Optional[Dict[str, Any]]
    split_day_manifest: Optional[Dict[str, Any]]


def _require_str(obj: Mapping[str, Any], key: str) -> str:
    v = obj.get(key)
    if v is None or not str(v).strip():
        raise ValueError(f"l2_training_bundle.json: missing or empty string field {key!r}")
    return str(v).strip()


def _optional_str(obj: Mapping[str, Any], key: str) -> Optional[str]:
    v = obj.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _resolve_day_manifest_rows(
    bundle_dir: Path,
    rows: Any,
    *,
    split_label: str,
) -> Tuple[Path, ...]:
    if not isinstance(rows, list) or not rows:
        raise ValueError(
            f"l2_training_bundle.json: split_day_manifest[{split_label!r}] must be a non-empty list"
        )
    out: list[Path] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"l2_training_bundle.json: split_day_manifest row {i} must be an object")
        rel = row.get("path")
        if rel is None or not str(rel).strip():
            raise ValueError(f"l2_training_bundle.json: split_day_manifest row {i} missing path")
        p = (bundle_dir / str(rel).strip()).resolve()
        if not p.is_file():
            raise ValueError(f"l2 training bundle: shard parquet not found for {split_label}: {p}")
        out.append(p)
    return tuple(out)


def load_and_validate_bundle(bundle_dir: Path) -> L2TrainingBundleManifest:
    """Load and validate ``l2_training_bundle.json`` under *bundle_dir*.

    Args:
        bundle_dir: Directory containing manifest and parquet files.

    Returns:
        Parsed manifest with absolute parquet paths.

    Raises:
        ValueError: On schema / snapshot consistency / missing files.
    """
    if not bundle_dir.is_dir():
        raise ValueError(f"l2 training bundle: not a directory: {bundle_dir}")
    bd = bundle_dir.resolve()
    mf_path = bd / L2_TRAINING_BUNDLE_MANIFEST_FILE
    if not mf_path.is_file():
        raise ValueError(
            f"l2 training bundle: missing {L2_TRAINING_BUNDLE_MANIFEST_FILE} under {bundle_dir}"
        )
    raw = json.loads(mf_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("l2_training_bundle.json: root must be an object")
    ver = str(raw.get("schema_version") or "1")
    if ver not in ("1", "2"):
        raise ValueError(f"l2_training_bundle.json: unsupported schema_version={ver!r} (expected '1' or '2')")

    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("l2_training_bundle.json: 'paths' must be an object")
    train_rel = _require_str(paths, "train")
    valid_rel = _require_str(paths, "valid")
    test_rel = _require_str(paths, "test")
    train_path = (bd / train_rel).resolve()
    valid_path = (bd / valid_rel).resolve()
    test_path = (bd / test_rel).resolve()
    for p, name in ((train_path, "train"), (valid_path, "valid"), (test_path, "test")):
        if not p.is_file():
            raise ValueError(f"l2 training bundle: {name} parquet not found: {p}")

    train_export_paths: Tuple[Path, ...] = (train_path,)
    valid_export_paths: Tuple[Path, ...] = (valid_path,)
    test_export_paths: Tuple[Path, ...] = (test_path,)
    split_calendar: Optional[Dict[str, Any]] = None
    split_day_manifest: Optional[Dict[str, Any]] = None

    if ver == "2":
        sdm = raw.get("split_day_manifest")
        if not isinstance(sdm, dict):
            raise ValueError("l2_training_bundle.json: schema_version 2 requires split_day_manifest object")
        split_day_manifest = dict(sdm)
        train_export_paths = _resolve_day_manifest_rows(bd, sdm.get("train"), split_label="train")
        valid_export_paths = _resolve_day_manifest_rows(bd, sdm.get("valid"), split_label="valid")
        test_export_paths = _resolve_day_manifest_rows(bd, sdm.get("test"), split_label="test")
        sc = raw.get("split_calendar")
        split_calendar = dict(sc) if isinstance(sc, dict) else None

    snap_vals: Dict[str, str] = {}
    for k in SNAPSHOT_ID_ALIASES:
        v = _optional_str(raw, k)
        if v:
            snap_vals[k] = v
    if len(set(snap_vals.values())) > 1:
        raise ValueError(
            "l2_training_bundle.json: conflicting snapshot ids "
            f"{snap_vals!r} (aliases {SNAPSHOT_ID_ALIASES})"
        )
    source_snapshot_id = _require_str(raw, "source_snapshot_id")
    if snap_vals:
        only = next(iter(snap_vals.values()))
        if only != source_snapshot_id:
            raise ValueError(
                f"l2_training_bundle.json: source_snapshot_id={source_snapshot_id!r} "
                f"does not match alias fields {snap_vals!r}"
            )

    l2_snapshot_id = _require_str(raw, "l2_snapshot_id")
    train_end = _require_str(raw, "train_end")
    window_start = _require_str(raw, "window_start")
    window_end = _require_str(raw, "window_end")

    split_sem = raw.get("split_semantics")
    if not isinstance(split_sem, dict):
        raise ValueError("l2_training_bundle.json: 'split_semantics' must be an object")
    valid_full = bool(split_sem.get("valid_full_unsampled"))
    test_full = bool(split_sem.get("test_full_unsampled"))
    train_samp = bool(split_sem.get("train_sampling_applied"))
    if not valid_full or not test_full:
        raise ValueError(
            "l2_training_bundle.json: split_semantics must assert "
            "valid_full_unsampled=true and test_full_unsampled=true"
        )

    idm = str(raw.get("identity_mapping_mode") or "cutoff_window").strip().lower()
    if idm not in ("pit_asof", "cutoff_window"):
        raise ValueError(f"l2_training_bundle.json: invalid identity_mapping_mode={idm!r}")

    label_meta = raw.get("label_asset")
    label_asset_meta: Optional[Dict[str, Any]] = None
    if label_meta is not None:
        if not isinstance(label_meta, dict):
            raise ValueError("l2_training_bundle.json: 'label_asset' must be an object or omitted")
        label_asset_meta = dict(label_meta)

    per_feature_fingerprints: Optional[Dict[str, str]] = None
    fl = raw.get("feature_lineage")
    if fl is not None:
        if not isinstance(fl, dict):
            raise ValueError("l2_training_bundle.json: 'feature_lineage' must be an object or omitted")
        raw_fp = fl.get("per_feature_fingerprints")
        if raw_fp is not None:
            if not isinstance(raw_fp, dict):
                raise ValueError(
                    "l2_training_bundle.json: feature_lineage.per_feature_fingerprints must be an object"
                )
            per_feature_fingerprints = {
                str(k): str(v) for k, v in raw_fp.items() if str(k).strip() and str(v).strip()
            }

    return L2TrainingBundleManifest(
        bundle_dir=bd,
        schema_version=ver,
        train_path=train_path,
        valid_path=valid_path,
        test_path=test_path,
        train_export_paths=train_export_paths,
        valid_export_paths=valid_export_paths,
        test_export_paths=test_export_paths,
        source_snapshot_id=source_snapshot_id,
        l2_snapshot_id=l2_snapshot_id,
        train_end=train_end,
        window_start=window_start,
        window_end=window_end,
        valid_full_unsampled=valid_full,
        test_full_unsampled=test_full,
        train_sampling_applied=train_samp,
        identity_mapping_mode=idm,
        label_asset_meta=label_asset_meta,
        per_feature_fingerprints=per_feature_fingerprints,
        split_calendar=split_calendar,
        split_day_manifest=split_day_manifest,
    )


def split_parquet_total_bytes(manifest: L2TrainingBundleManifest) -> int:
    """Return total on-disk bytes for train/valid/test monolithic split parquets."""
    total = 0
    for p in (manifest.train_path, manifest.valid_path, manifest.test_path):
        total += p.stat().st_size
    return total


def estimate_step7_peak_ram_gb_from_split_bytes(
    total_bytes: int,
    *,
    train_split_frac: float,
    use_duckdb: bool,
    chunk_concat_ram_factor: float,
) -> float:
    """Mirror trainer Step 7 peak heuristic using L2 split file sizes (TRN-16-04).

    Args:
        total_bytes: Sum of train + valid + test parquet file sizes.
        train_split_frac: TRAIN_SPLIT_FRAC from trainer config.
        use_duckdb: STEP7_USE_DUCKDB.
        chunk_concat_ram_factor: CHUNK_CONCAT_RAM_FACTOR (reuse naming for diagnostics).

    Returns:
        Estimated peak RSS in GiB (same order of magnitude as legacy chunk estimate).
    """
    if total_bytes < 0:
        raise ValueError(f"total_bytes must be non-negative, got {total_bytes}")
    tf = float(train_split_frac)
    if not (0.0 < tf <= 1.0):
        raise ValueError(f"train_split_frac must be in (0, 1], got {train_split_frac!r}")
    fac = float(chunk_concat_ram_factor)
    if use_duckdb:
        peak = total_bytes * fac * tf
    else:
        peak = total_bytes * fac * (1.0 + tf)
    return peak / (1024.0**3)
