"""Materialize ``l2_training_bundle.json`` + split parquets from chunk Step 7 outputs (#17 / E2E).

Writes under a bundle directory (default ``<data>/l2_training_bundle``) so
``pipeline_l2_bundle.execute_l2_training_bundle`` can consume the same contract as
hand-authored bundles.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

import pandas as pd

from trainer.training import data_sources

L2_BUNDLE_CACHE_KEY_FILE: str = ".l2_bundle_cache_key.json"
TRAIN_SPLIT_NAME: str = "train.parquet"
VALID_SPLIT_NAME: str = "valid.parquet"
TEST_SPLIT_NAME: str = "test.parquet"


def default_auto_bundle_dir() -> Path:
    """Directory used for ``--use-local-parquet`` auto L2 bundle materialization."""
    return data_sources.LOCAL_PARQUET_DIR / "l2_training_bundle"


def read_bridge_source_snapshot_id() -> Optional[str]:
    """Return ``source_snapshot_id`` from trainer local bridge manifest if present."""
    p = data_sources.trainer_local_parquet_bridge_manifest_path()
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    sid = raw.get("source_snapshot_id")
    if sid is not None and str(sid).strip():
        return str(sid).strip()
    return None


def stable_cache_key_fingerprint(key: Mapping[str, Any]) -> str:
    """Return first 12 hex chars of SHA-256 over canonical JSON of *key*."""
    blob = json.dumps(dict(key), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def read_cached_bundle_key(bundle_dir: Path) -> Optional[dict[str, Any]]:
    """Load ``.l2_bundle_cache_key.json`` if it exists and is valid JSON object."""
    p = bundle_dir / L2_BUNDLE_CACHE_KEY_FILE
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def auto_bundle_cache_is_current(*, bundle_dir: Path, expected_key: Mapping[str, Any]) -> bool:
    """Return True when *bundle_dir* contains a valid bundle whose cache key matches *expected_key*."""
    mf = bundle_dir / "l2_training_bundle.json"
    tr = bundle_dir / TRAIN_SPLIT_NAME
    va = bundle_dir / VALID_SPLIT_NAME
    te = bundle_dir / TEST_SPLIT_NAME
    if not (mf.is_file() and tr.is_file() and va.is_file() and te.is_file()):
        return False
    cached = read_cached_bundle_key(bundle_dir)
    if cached is None:
        return False
    return json.dumps(cached, sort_keys=True, separators=(",", ":"), default=str) == json.dumps(
        dict(expected_key), sort_keys=True, separators=(",", ":"), default=str
    )


def build_auto_l2_cache_key(
    *,
    bridge_manifest_stat: Optional[str],
    window_start_iso: str,
    window_end_iso: str,
    recent_chunks: Optional[int],
    train_split_frac: float,
    valid_split_frac: float,
    neg_sample_frac_config: float,
    feature_spec_fingerprint: str,
    rebuild_canonical_mapping: bool,
    identity_mapping_mode: str,
    force_recompute: bool,
) -> dict[str, Any]:
    """Inputs that must invalidate an auto-built L2 bundle when they change."""
    return {
        "kind": "trainer_auto_l2_bundle_v1",
        "bridge_manifest_stat": bridge_manifest_stat,
        "window_start_iso": window_start_iso,
        "window_end_iso": window_end_iso,
        "recent_chunks": recent_chunks,
        "train_split_frac": float(train_split_frac),
        "valid_split_frac": float(valid_split_frac),
        "neg_sample_frac_config": float(neg_sample_frac_config),
        "feature_spec_fingerprint": str(feature_spec_fingerprint),
        "rebuild_canonical_mapping": bool(rebuild_canonical_mapping),
        "identity_mapping_mode": str(identity_mapping_mode),
        "force_recompute": bool(force_recompute),
    }


def bridge_manifest_stat_token() -> Optional[str]:
    p = data_sources.trainer_local_parquet_bridge_manifest_path()
    if not p.is_file():
        return None
    try:
        st = p.stat()
        return f"{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return None


def fingerprint_feature_spec(path: Path) -> str:
    """Short stable fingerprint for feature spec file (mtime+size, not full hash)."""
    if not path.is_file():
        return "missing"
    try:
        st = path.stat()
        return f"{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return "unreadable"


def materialize_l2_training_bundle_dir(
    bundle_dir: Path,
    *,
    train_df: Optional[pd.DataFrame],
    valid_df: Optional[pd.DataFrame],
    test_df: Optional[pd.DataFrame],
    train_path: Optional[Path],
    valid_path: Optional[Path],
    test_path: Optional[Path],
    source_snapshot_id: str,
    train_end: Any,
    window_start: Any,
    window_end: Any,
    identity_mapping_mode: str,
    train_sampling_applied: bool,
    cache_key: Mapping[str, Any],
    label_asset_parquet: Optional[Union[str, Path]] = None,
    label_asset_meta: Optional[Mapping[str, Any]] = None,
) -> Path:
    """Write train/valid/test parquets + ``l2_training_bundle.json`` + cache sidecar.

    Either in-memory frames (*train_df* …) or on-disk Step 7 paths must be provided.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    out_train = bundle_dir / TRAIN_SPLIT_NAME
    out_valid = bundle_dir / VALID_SPLIT_NAME
    out_test = bundle_dir / TEST_SPLIT_NAME

    if train_path is not None:
        if valid_path is None or test_path is None:
            raise ValueError("train_path set but valid_path or test_path is None")
        shutil.copy2(train_path, out_train)
        shutil.copy2(valid_path, out_valid)
        shutil.copy2(test_path, out_test)
    elif train_df is not None and valid_df is not None and test_df is not None:
        train_df.to_parquet(out_train, index=False)
        valid_df.to_parquet(out_valid, index=False)
        test_df.to_parquet(out_test, index=False)
    else:
        raise ValueError("materialize_l2_training_bundle_dir: need either paths or dataframes")

    fp12 = stable_cache_key_fingerprint(cache_key)
    l2_snapshot_id = f"l2auto_{source_snapshot_id}_{fp12}"

    def _iso(x: Any) -> str:
        if hasattr(x, "isoformat"):
            return str(x.isoformat())
        return str(x)

    manifest: dict[str, Any] = {
        "schema_version": "1",
        "source_snapshot_id": str(source_snapshot_id),
        "l2_snapshot_id": l2_snapshot_id,
        "train_end": _iso(train_end),
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "paths": {
            "train": TRAIN_SPLIT_NAME,
            "valid": VALID_SPLIT_NAME,
            "test": TEST_SPLIT_NAME,
        },
        "split_semantics": {
            "valid_full_unsampled": True,
            "test_full_unsampled": True,
            "train_sampling_applied": bool(train_sampling_applied),
        },
        "identity_mapping_mode": str(identity_mapping_mode),
    }
    if label_asset_parquet is not None:
        src = Path(label_asset_parquet)
        if src.is_file():
            dest = bundle_dir / "label_asset.parquet"
            shutil.copy2(src, dest)
            lam: Dict[str, Any] = {"path": "label_asset.parquet"}
            if label_asset_meta:
                lam.update(dict(label_asset_meta))
            manifest["label_asset"] = lam
    (bundle_dir / "l2_training_bundle.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (bundle_dir / L2_BUNDLE_CACHE_KEY_FILE).write_text(
        json.dumps(dict(cache_key), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return bundle_dir.resolve()


def touch_bundle_built_at(bundle_dir: Path) -> None:
    """Optional marker for operators (not consumed by trainer)."""
    p = bundle_dir / ".l2_bundle_built_at_utc.txt"
    p.write_text(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8")
