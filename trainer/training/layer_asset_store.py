"""Per-chunk layer asset manifest (partition_id + lineage index).

Writes one JSON sidecar next to each chunk Parquet so planners and operators can
resolve ``(layer, feature_id, partition_id)`` without scanning wide frames.
DuckDB PIT identity is recorded via *pit_identity_engine* on the manifest
(same contract as chunk cache components).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from trainer.features.features import get_candidate_feature_ids
from trainer.training.feature_materialization import per_feature_fingerprints

logger = logging.getLogger(__name__)

LAYER_ASSET_MANIFEST_VERSION: str = "layer_asset_manifest_v1"


def chunk_partition_id(window_start: Any, window_end: Any) -> str:
    """Stable partition id for the training time-chunk (aligns with chunk Parquet basename).

    Uses ``YYYYMMDD`` bounds matching :func:`trainer.training.trainer._chunk_parquet_path`.
    """
    ws = pd.Timestamp(window_start)
    if ws.tzinfo is not None:
        ws = ws.tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    else:
        ws = ws.replace(tzinfo=None) if hasattr(ws, "replace") else pd.Timestamp(ws).replace(tzinfo=None)
    we = pd.Timestamp(window_end)
    if we.tzinfo is not None:
        we = we.tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    else:
        we = we.replace(tzinfo=None) if hasattr(we, "replace") else pd.Timestamp(we).replace(tzinfo=None)
    return f"time_chunk:{ws.strftime('%Y%m%d')}:{we.strftime('%Y%m%d')}"


def chunk_partition_ids_for_windows(chunks: Sequence[Mapping[str, Any]]) -> List[str]:
    """Return ordered partition ids for each training window in *chunks*."""
    out: List[str] = []
    for c in chunks:
        out.append(chunk_partition_id(c["window_start"], c["window_end"]))
    return out


def layer_asset_manifest_path(chunk_parquet_path: Path) -> Path:
    """Sidecar path: ``chunk_YYYYMMDD_YYYYMMDD.layer_assets.json``."""
    return chunk_parquet_path.with_suffix(".layer_assets.json")


def _layer_fingerprint_digest(feature_ids: Sequence[str], fps: Mapping[str, str]) -> str:
    pairs = sorted((fid, fps.get(fid, "")) for fid in feature_ids)
    blob = json.dumps(pairs, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def build_chunk_layer_asset_manifest(
    *,
    chunk_parquet_path: Path,
    partition_id: str,
    labeled_columns: pd.Index,
    feature_spec: Optional[dict],
    source_snapshot_id: Optional[str],
    pit_policy_id: str,
    pit_identity_engine: str,
    row_count: int,
) -> Dict[str, Any]:
    """Build serialisable manifest body (caller writes JSON)."""
    cols = set(str(x) for x in labeled_columns)
    fps = per_feature_fingerprints(feature_spec) if isinstance(feature_spec, dict) else {}
    layers: Dict[str, Any] = {}
    if isinstance(feature_spec, dict):
        for track, layer_key in (
            ("track_llm", "bet"),
            ("track_human", "run"),
            ("track_profile", "player"),
        ):
            fids = [
                str(x)
                for x in get_candidate_feature_ids(feature_spec, track, screening_only=False)
                if str(x) in cols
            ]
            layers[layer_key] = {
                "feature_ids": sorted(fids)[:500],
                "feature_id_count": len(fids),
                "fingerprint_digest": _layer_fingerprint_digest(fids, fps),
            }
    trip_cols = sorted(c for c in cols if c.startswith("lda_"))[:32]
    layers["trip"] = {
        "lda_columns_present": trip_cols,
        "lda_column_count": len([c for c in cols if c.startswith("lda_")]),
    }
    declared_present = sorted(cols & set(fps.keys()))[:400]
    _in_chunk = [k for k in fps if k in cols]
    fp_subset = {k: fps[k] for k in sorted(_in_chunk)}
    fp_truncated = len(fp_subset) > 300
    if fp_truncated:
        fp_subset = dict(sorted(fp_subset.items())[:300])
    return {
        "manifest_version": LAYER_ASSET_MANIFEST_VERSION,
        "partition_id": partition_id,
        "chunk_parquet": chunk_parquet_path.name,
        "row_count": int(row_count),
        "source_snapshot_id": str(source_snapshot_id or "unknown"),
        "pit_policy_id": str(pit_policy_id),
        "pit_identity_engine": str(pit_identity_engine),
        "per_feature_fingerprints_subset": fp_subset,
        "per_feature_fingerprints_subset_truncated": fp_truncated,
        "layers": layers,
        "declared_feature_columns_present": declared_present,
    }


def write_chunk_layer_asset_manifest(
    *,
    chunk_parquet_path: Path,
    chunk: Mapping[str, Any],
    labeled_columns: pd.Index,
    feature_spec: Optional[dict],
    source_snapshot_id: Optional[str],
    pit_policy_id: str,
    pit_identity_engine: str,
    row_count: int,
) -> Path:
    """Write manifest next to chunk Parquet; returns manifest path."""
    pid = chunk_partition_id(chunk["window_start"], chunk["window_end"])
    body = build_chunk_layer_asset_manifest(
        chunk_parquet_path=chunk_parquet_path,
        partition_id=pid,
        labeled_columns=labeled_columns,
        feature_spec=feature_spec,
        source_snapshot_id=source_snapshot_id,
        pit_policy_id=pit_policy_id,
        pit_identity_engine=pit_identity_engine,
        row_count=row_count,
    )
    out = layer_asset_manifest_path(chunk_parquet_path)
    out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.debug("Wrote layer asset manifest %s", out)
    return out


def read_chunk_layer_asset_manifest(chunk_parquet_path: Path) -> Optional[Dict[str, Any]]:
    """Load manifest if present; returns None on missing/invalid."""
    p = layer_asset_manifest_path(chunk_parquet_path)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None
