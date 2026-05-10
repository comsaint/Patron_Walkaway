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
from trainer.training.l2_day_shard import (
    copy_parquet_with_canonical_l2_columns,
    l2_bundle_column_rename_map,
    min_max_day_from_manifest_rows,
    shard_split_parquet_by_day,
)

L2_BUNDLE_CACHE_KEY_FILE: str = ".l2_bundle_cache_key.json"
TRAINER_IMPACT_CHECKPOINT_FILE: str = "last_impact_checkpoint.json"
TRAIN_SPLIT_NAME: str = "train.parquet"


def trainer_impact_checkpoint_path() -> Path:
    """Path under ``trainer/.data`` for persisted per-feature fingerprints (impact planner)."""
    root = Path(__file__).resolve().parent.parent
    out = root / ".data"
    out.mkdir(parents=True, exist_ok=True)
    return out / TRAINER_IMPACT_CHECKPOINT_FILE


def read_trainer_impact_checkpoint() -> Optional[dict[str, Any]]:
    """Load last Step-7 checkpoint for ``plan_impacted_materialization_work``."""
    p = trainer_impact_checkpoint_path()
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def write_trainer_impact_checkpoint(
    *,
    per_feature_fingerprints: Mapping[str, str],
    source_snapshot_id: str,
) -> None:
    """Persist fingerprints + snapshot after a successful auto L2 bundle materialization."""
    p = trainer_impact_checkpoint_path()
    payload = {
        "per_feature_fingerprints": dict(per_feature_fingerprints),
        "source_snapshot_id": str(source_snapshot_id or "unknown").strip() or "unknown",
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


VALID_SPLIT_NAME: str = "valid.parquet"
TEST_SPLIT_NAME: str = "test.parquet"


def l2_bundle_split_files_present(bundle_dir: Path) -> bool:
    """Return True when bundle manifest and split parquet files exist and schema2 day refs are valid."""
    mf = bundle_dir / "l2_training_bundle.json"
    tr = bundle_dir / TRAIN_SPLIT_NAME
    va = bundle_dir / VALID_SPLIT_NAME
    te = bundle_dir / TEST_SPLIT_NAME
    if not (mf.is_file() and tr.is_file() and va.is_file() and te.is_file()):
        return False
    try:
        raw_mf = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw_mf, dict):
        return False
    ver = str(raw_mf.get("schema_version") or "1")
    if ver == "2":
        sdm = raw_mf.get("split_day_manifest")
        if not isinstance(sdm, dict):
            return False
        for k in ("train", "valid", "test"):
            rows = sdm.get(k)
            if not isinstance(rows, list) or not rows:
                return False
            for row in rows:
                if not isinstance(row, dict):
                    return False
                rel = row.get("path")
                if not rel or not (bundle_dir / str(rel)).is_file():
                    return False
    return True


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
    from trainer.core import _config_training_domain as _tdom
    from trainer.training.l2_reuse_keys import (
        normalize_auto_l2_cache_key,
        source_invariant_match,
        window_view_match,
    )

    if not l2_bundle_split_files_present(bundle_dir):
        return False
    cached = read_cached_bundle_key(bundle_dir)
    if cached is None:
        return False
    if not bool(getattr(_tdom, "L2_REUSE_V3_CACHE_KEYS", True)):
        return json.dumps(cached, sort_keys=True, separators=(",", ":"), default=str) == json.dumps(
            dict(expected_key), sort_keys=True, separators=(",", ":"), default=str
        )
    exp_n = normalize_auto_l2_cache_key(expected_key)
    cache_n = normalize_auto_l2_cache_key(cached)
    if not (source_invariant_match(cache_n, exp_n) and window_view_match(cache_n, exp_n)):
        return False
    return True


def resolve_l2_auto_bundle_cache(bundle_dir: Path, expected_key: Mapping[str, Any]) -> dict[str, Any]:
    """Diagnostics for L2 auto-cache (source vs window layer)."""
    from trainer.training.l2_reuse_keys import resolve_l2_auto_cache

    files_ok = l2_bundle_split_files_present(bundle_dir)
    return resolve_l2_auto_cache(bundle_dir=bundle_dir, expected_key=expected_key, bundle_files_ok=files_ok)


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
    pit_identity_engine: str = "cutoff_window_map",
    source_snapshot_id: Optional[str] = None,
) -> dict[str, Any]:
    """Inputs that must invalidate an auto-built L2 bundle when they change."""
    from trainer.core import _config_training_domain as _tdom
    from trainer.core._config_training_domain import (
        ALERT_HORIZON_MIN,
        LABEL_LOOKAHEAD_MIN,
        WALKAWAY_GAP_MIN,
    )
    from trainer.training.feature_materialization import compute_policy_version
    from trainer.training.label_asset_cache import label_definition_version
    from trainer.training.l2_reuse_keys import (
        AUTO_L2_KIND_V2,
        build_auto_l2_cache_key_v3,
        censoring_policy_id_from_semantics,
    )

    if not bool(getattr(_tdom, "L2_REUSE_V3_CACHE_KEYS", True)):
        return {
            "kind": AUTO_L2_KIND_V2,
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
    _snap = source_snapshot_id if source_snapshot_id is not None else read_bridge_source_snapshot_id()
    _snap = str(_snap).strip() if _snap is not None else "unknown"
    if not _snap:
        _snap = "unknown"
    _censor = censoring_policy_id_from_semantics(
        walkaway_gap_min=int(WALKAWAY_GAP_MIN),
        alert_horizon_min=int(ALERT_HORIZON_MIN),
        label_lookahead_min=int(LABEL_LOOKAHEAD_MIN),
    )
    return build_auto_l2_cache_key_v3(
        bridge_manifest_stat=bridge_manifest_stat,
        window_start_iso=window_start_iso,
        window_end_iso=window_end_iso,
        recent_chunks=recent_chunks,
        train_split_frac=float(train_split_frac),
        valid_split_frac=float(valid_split_frac),
        neg_sample_frac_config=float(neg_sample_frac_config),
        feature_spec_fingerprint=str(feature_spec_fingerprint),
        rebuild_canonical_mapping=bool(rebuild_canonical_mapping),
        identity_mapping_mode=str(identity_mapping_mode),
        pit_identity_engine=str(pit_identity_engine),
        source_snapshot_id=_snap,
        label_definition_version=label_definition_version(),
        censoring_policy_id=_censor,
        compute_policy_version=compute_policy_version(),
        force_recompute=bool(force_recompute),
    )


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


def _canonicalize_bundle_dataframe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *df* with canonical lower-case L2 bundle column names."""
    rename_map = l2_bundle_column_rename_map(list(df.columns))
    if all(src == dst for src, dst in rename_map.items()):
        return df.copy()
    return df.rename(columns=rename_map).copy()


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
    per_feature_fingerprints: Optional[Mapping[str, str]] = None,
) -> Path:
    """Write train/valid/test parquets + ``l2_training_bundle.json`` + cache sidecar.

    Either in-memory frames (*train_df* …) or on-disk Step 7 paths must be provided.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    out_train = bundle_dir / TRAIN_SPLIT_NAME
    out_valid = bundle_dir / VALID_SPLIT_NAME
    out_test = bundle_dir / TEST_SPLIT_NAME

    # Canonical writer contract: every L2 bundle Parquet written here uses
    # explicit lower-case column names so downstream pandas/pyarrow/DuckDB reads
    # observe the same schema without case-dependent drift.
    if train_path is not None:
        if valid_path is None or test_path is None:
            raise ValueError("train_path set but valid_path or test_path is None")
        copy_parquet_with_canonical_l2_columns(train_path, out_train)
        copy_parquet_with_canonical_l2_columns(valid_path, out_valid)
        copy_parquet_with_canonical_l2_columns(test_path, out_test)
    elif train_df is not None and valid_df is not None and test_df is not None:
        _canonicalize_bundle_dataframe_columns(train_df).to_parquet(out_train, index=False)
        _canonicalize_bundle_dataframe_columns(valid_df).to_parquet(out_valid, index=False)
        _canonicalize_bundle_dataframe_columns(test_df).to_parquet(out_test, index=False)
    else:
        raise ValueError("materialize_l2_training_bundle_dir: need either paths or dataframes")

    fp12 = stable_cache_key_fingerprint(cache_key)
    l2_snapshot_id = f"l2auto_{source_snapshot_id}_{fp12}"

    def _iso(x: Any) -> str:
        if hasattr(x, "isoformat"):
            return str(x.isoformat())
        return str(x)

    dm_train = shard_split_parquet_by_day(bundle_dir, "train", out_train)
    dm_valid = shard_split_parquet_by_day(bundle_dir, "valid", out_valid)
    dm_test = shard_split_parquet_by_day(bundle_dir, "test", out_test)
    tr_lo, tr_hi = min_max_day_from_manifest_rows(dm_train)
    va_lo, va_hi = min_max_day_from_manifest_rows(dm_valid)
    te_lo, te_hi = min_max_day_from_manifest_rows(dm_test)
    split_calendar = {
        "train": {
            "gaming_day_min": tr_lo,
            "gaming_day_max": tr_hi,
            "policy": "row_fraction_step7_then_day_shard",
        },
        "valid": {"gaming_day_min": va_lo, "gaming_day_max": va_hi, "policy": "row_fraction_step7_then_day_shard"},
        "test": {"gaming_day_min": te_lo, "gaming_day_max": te_hi, "policy": "row_fraction_step7_then_day_shard"},
    }

    manifest: dict[str, Any] = {
        "schema_version": "2",
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
        "split_day_manifest": {
            "train": dm_train,
            "valid": dm_valid,
            "test": dm_test,
        },
        "split_calendar": split_calendar,
        "split_semantics": {
            "valid_full_unsampled": True,
            "test_full_unsampled": True,
            "train_sampling_applied": bool(train_sampling_applied),
        },
        "identity_mapping_mode": str(identity_mapping_mode),
    }
    _src_inv = cache_key.get("source_invariant") if isinstance(cache_key, dict) else None
    if isinstance(_src_inv, dict):
        _im_rev = _src_inv.get("identity_mapping_revision")
        if _im_rev:
            manifest["identity_mapping_revision"] = str(_im_rev)
    if label_asset_parquet is not None:
        src = Path(label_asset_parquet)
        if src.is_file():
            dest = bundle_dir / "label_asset.parquet"
            shutil.copy2(src, dest)
            lam: Dict[str, Any] = {"path": "label_asset.parquet"}
            if label_asset_meta:
                lam.update(dict(label_asset_meta))
            manifest["label_asset"] = lam
    if per_feature_fingerprints:
        manifest["feature_lineage"] = {
            "manifest_version": "feature_lineage_v1",
            "per_feature_fingerprints": dict(per_feature_fingerprints),
        }
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
