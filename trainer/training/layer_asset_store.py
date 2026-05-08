"""Layer asset manifests, partition_id contract, and dev bundle index.

Supports:
- **Manifest v2**: portable ``asset_id``, ``partition_scheme``, ``coverage_*``,
  ``compute_policy_version``, ``fingerprint``, optional ``upstream_asset_ids``.
- **Manifest v1**: legacy chunk sidecars; :func:`normalize_layer_asset_manifest` upgrades view.
- **Bundle index**: ``layer_asset_bundle_index.json`` for shipping precomputed assets.
- **Online increment** partition ids and optional watermark sidecar paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from trainer.features.features import get_candidate_feature_ids
from trainer.training.feature_materialization import per_feature_fingerprints

logger = logging.getLogger(__name__)

LAYER_ASSET_MANIFEST_V1: str = "layer_asset_manifest_v1"
LAYER_ASSET_MANIFEST_V2: str = "layer_asset_manifest_v2"

BUNDLE_INDEX_VERSION: str = "layer_asset_bundle_index_v1"

# Partition schemes (contract vocabulary)
PARTITION_SCHEME_OFFLINE_TIME_CHUNK: str = "offline_time_chunk"
PARTITION_SCHEME_OFFLINE_GAMING_MONTH: str = "offline_gaming_month"
PARTITION_SCHEME_ONLINE_INCREMENT: str = "online_increment"

_ENV_BUNDLE_DIR: str = "TRAINER_LAYER_ASSET_BUNDLE_DIR"
_ENV_WATERMARK_PATH: str = "TRAINER_SCORER_WATERMARK_PATH"


def compute_deterministic_asset_id(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of canonical JSON (stable cross-process)."""
    blob = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def chunk_partition_id(window_start: Any, window_end: Any) -> str:
    """Stable partition id for the training time-chunk (aligns with chunk Parquet basename)."""
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


def gaming_month_partition_id(year: int, month: int) -> str:
    """Offline gaming-month style partition (YYYYMM)."""
    return f"gaming_month:{int(year):04d}{int(month):02d}"


def online_increment_partition_id(watermark_start: Any, watermark_end: Any) -> str:
    """Online micro-batch partition (UTC-naive ISO for stability in logs)."""
    a = pd.Timestamp(watermark_start)
    b = pd.Timestamp(watermark_end)
    if a.tzinfo is not None:
        a = a.tz_convert("UTC").replace(tzinfo=None)
    if b.tzinfo is not None:
        b = b.tz_convert("UTC").replace(tzinfo=None)
    return f"inc:{a.isoformat()}:{b.isoformat()}"


def parse_time_chunk_partition_coverage(partition_id: str) -> Optional[Tuple[str, str]]:
    """Parse ``time_chunk:YYYYMMDD:YYYYMMDD`` -> (coverage_start_iso, coverage_end_iso) naive dates."""
    m = re.match(r"^time_chunk:(\d{8}):(\d{8})$", str(partition_id).strip())
    if not m:
        return None
    d0 = pd.Timestamp(str(m.group(1)))
    d1 = pd.Timestamp(str(m.group(2)))
    return (d0.strftime("%Y-%m-%d"), d1.strftime("%Y-%m-%d"))


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


def normalize_layer_asset_manifest(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a v2-shaped manifest dict (copy); upgrade v1 in-memory."""
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    ver = str(out.get("manifest_version") or "")
    if ver == LAYER_ASSET_MANIFEST_V2:
        return out
    # v1 upgrade
    out["manifest_version"] = LAYER_ASSET_MANIFEST_V2
    out.setdefault("partition_scheme", PARTITION_SCHEME_OFFLINE_TIME_CHUNK)
    pid = str(out.get("partition_id") or "")
    cov = parse_time_chunk_partition_coverage(pid)
    if cov:
        out.setdefault("coverage_start", cov[0])
        out.setdefault("coverage_end", cov[1])
    out.setdefault("upstream_asset_ids", [])
    from trainer.training.feature_materialization import compute_policy_version as _cpv

    out.setdefault("compute_policy_version", _cpv())
    # Fingerprint: digest of per_feature subset if present else layer digests
    fps_sub = out.get("per_feature_fingerprints_subset")
    if isinstance(fps_sub, dict) and fps_sub:
        out["fingerprint"] = _layer_fingerprint_digest(sorted(fps_sub.keys()), {str(k): str(v) for k, v in fps_sub.items()})
    else:
        layers = out.get("layers") or {}
        parts: List[str] = []
        if isinstance(layers, dict):
            for lk, body in sorted(layers.items()):
                if isinstance(body, dict) and body.get("fingerprint_digest"):
                    parts.append(f"{lk}:{body.get('fingerprint_digest')}")
        out["fingerprint"] = hashlib.sha256("|".join(parts).encode()).hexdigest()[:24] if parts else "empty"
    aid_src = {
        "kind": "chunk_layer_bundle",
        "partition_id": out.get("partition_id"),
        "partition_scheme": out.get("partition_scheme"),
        "coverage_start": out.get("coverage_start"),
        "coverage_end": out.get("coverage_end"),
        "source_snapshot_id": out.get("source_snapshot_id"),
        "pit_policy_id": out.get("pit_policy_id"),
        "pit_identity_engine": out.get("pit_identity_engine"),
        "compute_policy_version": out.get("compute_policy_version"),
        "fingerprint": out.get("fingerprint"),
    }
    out["asset_id"] = compute_deterministic_asset_id(aid_src)
    return out


def build_chunk_layer_asset_manifest(
    *,
    chunk_parquet_path: Path,
    partition_id: str,
    partition_scheme: str,
    coverage_start_iso: str,
    coverage_end_iso: str,
    labeled_columns: pd.Index,
    feature_spec: Optional[dict],
    source_snapshot_id: Optional[str],
    pit_policy_id: str,
    pit_identity_engine: str,
    row_count: int,
) -> Dict[str, Any]:
    """Build serialisable manifest body v2 (caller may write JSON)."""
    from trainer.training.feature_materialization import compute_policy_version as _cpv

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
    fp_global_digest = _layer_fingerprint_digest(sorted(fp_subset.keys()), fp_subset)
    cpv = _cpv()
    body_pre_id = {
        "manifest_version": LAYER_ASSET_MANIFEST_V2,
        "partition_scheme": str(partition_scheme),
        "partition_id": str(partition_id),
        "coverage_start": str(coverage_start_iso),
        "coverage_end": str(coverage_end_iso),
        "chunk_parquet": chunk_parquet_path.name,
        "row_count": int(row_count),
        "source_snapshot_id": str(source_snapshot_id or "unknown"),
        "pit_policy_id": str(pit_policy_id),
        "pit_identity_engine": str(pit_identity_engine),
        "compute_policy_version": str(cpv),
        "fingerprint": fp_global_digest,
        "upstream_asset_ids": [],
        "per_feature_fingerprints_subset": fp_subset,
        "per_feature_fingerprints_subset_truncated": fp_truncated,
        "layers": layers,
        "declared_feature_columns_present": declared_present,
    }
    aid_src = {
        "kind": "chunk_layer_bundle",
        "partition_id": body_pre_id["partition_id"],
        "partition_scheme": body_pre_id["partition_scheme"],
        "coverage_start": body_pre_id["coverage_start"],
        "coverage_end": body_pre_id["coverage_end"],
        "source_snapshot_id": body_pre_id["source_snapshot_id"],
        "pit_policy_id": body_pre_id["pit_policy_id"],
        "pit_identity_engine": body_pre_id["pit_identity_engine"],
        "compute_policy_version": body_pre_id["compute_policy_version"],
        "fingerprint": body_pre_id["fingerprint"],
    }
    body = {**body_pre_id, "asset_id": compute_deterministic_asset_id(aid_src)}
    return body


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
    partition_scheme: str = PARTITION_SCHEME_OFFLINE_TIME_CHUNK,
) -> Path:
    """Write manifest next to chunk Parquet; returns manifest path."""
    ws = chunk["window_start"]
    we = chunk["window_end"]
    pid = chunk_partition_id(ws, we)
    ws_n = pd.Timestamp(ws)
    we_n = pd.Timestamp(we)
    if ws_n.tzinfo is not None:
        ws_n = ws_n.tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    if we_n.tzinfo is not None:
        we_n = we_n.tz_convert("Asia/Hong_Kong").replace(tzinfo=None)
    cov_start = ws_n.strftime("%Y-%m-%d")
    cov_end = we_n.strftime("%Y-%m-%d")
    body = build_chunk_layer_asset_manifest(
        chunk_parquet_path=chunk_parquet_path,
        partition_id=pid,
        partition_scheme=partition_scheme,
        coverage_start_iso=cov_start,
        coverage_end_iso=cov_end,
        labeled_columns=labeled_columns,
        feature_spec=feature_spec,
        source_snapshot_id=source_snapshot_id,
        pit_policy_id=pit_policy_id,
        pit_identity_engine=pit_identity_engine,
        row_count=row_count,
    )
    out = layer_asset_manifest_path(chunk_parquet_path)
    out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.debug("Wrote layer asset manifest v2 %s", out)
    return out


def read_chunk_layer_asset_manifest(chunk_parquet_path: Path) -> Optional[Dict[str, Any]]:
    """Load manifest if present; returns **v2-normalized** dict or None."""
    p = layer_asset_manifest_path(chunk_parquet_path)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return normalize_layer_asset_manifest(raw)


def default_watermark_sidecar_path(bundle_dir: Path) -> Path:
    """Default scorer/trainer watermark path under bundle dir."""
    return bundle_dir / ".scorer_watermark.json"


def read_watermark_cursor(bundle_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load watermark JSON if ``TRAINER_SCORER_WATERMARK_PATH`` or bundle default exists."""
    raw_path = (os.environ.get(_ENV_WATERMARK_PATH) or "").strip()
    p: Optional[Path] = None
    if raw_path:
        p = Path(raw_path)
    elif bundle_dir is not None:
        p = default_watermark_sidecar_path(bundle_dir)
    if p is None or not p.is_file():
        return None
    try:
        w = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return w if isinstance(w, dict) else None


def write_watermark_cursor(path: Path, payload: Mapping[str, Any]) -> None:
    """Persist watermark cursor (atomic replace via write to temp not required for MVP)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --- Bundle index (WS2 / WS3) -------------------------------------------------


def bundle_index_path(bundle_dir: Path) -> Path:
    return bundle_dir / "layer_asset_bundle_index.json"


def write_layer_asset_bundle_index(
    bundle_dir: Path,
    entries: Sequence[Mapping[str, Any]],
    *,
    aggregate_coverage_start: Optional[str] = None,
    aggregate_coverage_end: Optional[str] = None,
) -> Path:
    """Write ``layer_asset_bundle_index.json`` with file SHA-256 for integrity checks."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    resolved: List[Dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rel = str(e.get("relative_path") or "").strip()
        if not rel:
            continue
        fp = (bundle_dir / rel).resolve()
        if not fp.is_file():
            raise FileNotFoundError(f"bundle index entry missing file: {fp}")
        digest = hashlib.sha256(fp.read_bytes()).hexdigest()
        resolved.append({**dict(e), "sha256": digest, "relative_path": rel})
    body = {
        "bundle_index_version": BUNDLE_INDEX_VERSION,
        "aggregate_coverage_start": aggregate_coverage_start,
        "aggregate_coverage_end": aggregate_coverage_end,
        "entry_count": len(resolved),
        "entries": resolved,
    }
    out = bundle_index_path(bundle_dir)
    out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info("Wrote layer asset bundle index %s (%d entries)", out, len(resolved))
    return out


def validate_layer_asset_bundle_index(bundle_dir: Path) -> Tuple[bool, str]:
    """Verify index exists and every entry file matches recorded sha256."""
    p = bundle_index_path(bundle_dir)
    if not p.is_file():
        return False, f"missing bundle index: {p}"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid bundle index JSON: {exc}"
    if not isinstance(raw, dict):
        return False, "bundle index root must be object"
    entries = raw.get("entries") or []
    if not isinstance(entries, list):
        return False, "entries must be a list"
    for i, ent in enumerate(entries):
        if not isinstance(ent, dict):
            return False, f"entry {i} must be object"
        rel = str(ent.get("relative_path") or "").strip()
        exp = str(ent.get("sha256") or "").strip()
        if not rel or not exp:
            return False, f"entry {i} missing relative_path or sha256"
        fpath = (bundle_dir / rel).resolve()
        if not fpath.is_file():
            return False, f"missing bundle file: {fpath}"
        act = hashlib.sha256(fpath.read_bytes()).hexdigest()
        if act != exp:
            return False, f"sha256 mismatch for {rel}: expected {exp[:12]}… got {act[:12]}…"
    return True, f"ok ({len(entries)} entries)"


def load_layer_asset_bundle_index(bundle_dir: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Load and optionally validate when ``TRAINER_LAYER_ASSET_BUNDLE_STRICT=1``."""
    ok, msg = validate_layer_asset_bundle_index(bundle_dir)
    raw_path = bundle_index_path(bundle_dir)
    if not ok and _truthy_env("TRAINER_LAYER_ASSET_BUNDLE_STRICT"):
        return None, msg
    if not raw_path.is_file():
        return None, msg if not ok else None
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)
    if not isinstance(raw, dict):
        return None, "bundle index not an object"
    if not ok:
        logger.warning("layer asset bundle index validation advisory: %s", msg)
    return raw, None if ok else msg


def _truthy_env(name: str) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def summarize_bundle_for_audit(bundle_dir: Path) -> Dict[str, Any]:
    """JSON-safe summary for ``pipeline_diagnostics`` (WS6)."""
    idx, err = load_layer_asset_bundle_index(bundle_dir)
    wm = read_watermark_cursor(bundle_dir)
    out: Dict[str, Any] = {
        "bundle_dir": str(bundle_dir.resolve()),
        "index_present": bundle_index_path(bundle_dir).is_file(),
        "index_load_error": err,
        "aggregate_coverage_end": (idx or {}).get("aggregate_coverage_end") if idx else None,
        "entry_count": (idx or {}).get("entry_count") if idx else None,
        "watermark_present": wm is not None,
    }
    if wm:
        out["watermark_keys"] = sorted(str(k) for k in wm.keys())
    return out
