"""L2 pre-assembled training bundle manifest (GitHub #16 TRN-16-03 / #17 TRN-17-01).

Reads ``l2_training_bundle.json`` under a bundle directory, validates snapshot lineage,
resolves parquet paths, and provides split-bytes OOM estimates (TRN-16-04).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

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
    train_path: Path
    valid_path: Path
    test_path: Path
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
    mf_path = bundle_dir / L2_TRAINING_BUNDLE_MANIFEST_FILE
    if not mf_path.is_file():
        raise ValueError(
            f"l2 training bundle: missing {L2_TRAINING_BUNDLE_MANIFEST_FILE} under {bundle_dir}"
        )
    raw = json.loads(mf_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("l2_training_bundle.json: root must be an object")
    ver = raw.get("schema_version")
    if str(ver) != "1":
        raise ValueError(f"l2_training_bundle.json: unsupported schema_version={ver!r} (expected '1')")

    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("l2_training_bundle.json: 'paths' must be an object")
    train_rel = _require_str(paths, "train")
    valid_rel = _require_str(paths, "valid")
    test_rel = _require_str(paths, "test")
    train_path = (bundle_dir / train_rel).resolve()
    valid_path = (bundle_dir / valid_rel).resolve()
    test_path = (bundle_dir / test_rel).resolve()
    for p, name in ((train_path, "train"), (valid_path, "valid"), (test_path, "test")):
        if not p.is_file():
            raise ValueError(f"l2 training bundle: {name} parquet not found: {p}")

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

    return L2TrainingBundleManifest(
        bundle_dir=bundle_dir.resolve(),
        train_path=train_path,
        valid_path=valid_path,
        test_path=test_path,
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
    )


def split_parquet_total_bytes(manifest: L2TrainingBundleManifest) -> int:
    """Return total on-disk bytes for train/valid/test split parquets."""
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


