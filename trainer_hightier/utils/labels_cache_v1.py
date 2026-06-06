"""Walkaway labels cache v1 (L4): manifest + cache hit around existing label materializer."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    ALERT_HORIZON_MIN,
    DuckDbRuntimeConfig,
    LABELS_CANONICAL_SHARD_COUNT,
    WALKAWAY_GAP_MIN,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
from trainer_hightier.utils.source_manifest_v2 import default_cache_root, write_json_atomic
from trainer_hightier.utils.walkaway_labels import (
    default_walkaway_labels_parquet_path,
    list_joined_bet_payout_months,
    load_joined_bets_dataframe,
    materialize_walkaway_labels_from_cleaned_bet,
    write_walkaway_labels_from_joined_dataframe,
)

logger = logging.getLogger("trainer_hightier")

LABELS_KIND: Final[str] = "walkaway_labels_v1"
LABELS_SHARDED_KIND: Final[str] = "walkaway_labels_v1_sharded"
LABELS_SCHEMA_VERSION: Final[int] = 1
LABELS_SHARDED_SCHEMA_VERSION: Final[int] = 2
AGGREGATE_MANIFEST_NAME: Final[str] = "aggregate_manifest.json"


def default_labels_cache_root(*, package_dir: Path | None = None) -> Path:
    """Return ``trainer_hightier/artifacts/cache/labels_v1``."""
    return default_cache_root(package_dir=package_dir) / "labels_v1"


def label_semantic_fingerprint() -> str:
    """SHA-256 over label compute module bytes + gap/horizon constants."""
    mod = Path(__file__).resolve().parents[1] / "walkaway_compute_labels.py"
    digest = hashlib.sha256(mod.read_bytes()).hexdigest()
    blob = f"{digest}|gap={WALKAWAY_GAP_MIN}|horizon={ALERT_HORIZON_MIN}"
    return hashlib.sha256(blob.encode()).hexdigest()


def labels_policy_dir(
    *,
    cache_root: Path,
    entity_set_fingerprint: str,
) -> Path:
    """Directory for one labels cache policy keyed by entity set fingerprint."""
    fp = str(entity_set_fingerprint).strip()
    if not fp:
        raise ValueError("entity_set_fingerprint must be non-empty")
    return Path(cache_root).resolve() / f"entity_set={fp[:16]}"


def labels_shard_dir(*, policy_dir: Path, month: str, canonical_shard: int) -> Path:
    """Return ``month=YYYYMM/canonical_shard=N`` directory under a labels policy."""
    ym = str(month).strip()
    if len(ym) != 6 or not ym.isdigit():
        raise ValueError(f"month must be six YYYYMM digits, got {month!r}")
    shard = int(canonical_shard)
    if shard < 0:
        raise ValueError(f"canonical_shard must be >= 0, got {canonical_shard!r}")
    return Path(policy_dir).resolve() / f"month={ym}" / f"canonical_shard={shard}"


def load_labels_manifest(path: Path) -> dict[str, Any] | None:
    """Load labels sidecar manifest or ``None`` when missing/corrupt."""
    p = Path(path).resolve()
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def labels_cache_is_hit(
    *,
    manifest_path: Path,
    labels_parquet_path: Path,
    entity_set_fingerprint: str,
    label_semantic_fp: str,
) -> bool:
    """Return True when monolithic manifest matches policy and labels parquet exists."""
    dst = Path(labels_parquet_path).resolve()
    if not dst.is_file():
        return False
    prev = load_labels_manifest(manifest_path)
    if prev is None:
        return False
    return (
        str(prev.get("entity_set_fingerprint")) == str(entity_set_fingerprint)
        and str(prev.get("label_semantic_fingerprint")) == str(label_semantic_fp)
        and int(prev.get("walkaway_gap_min", -1)) == int(WALKAWAY_GAP_MIN)
        and int(prev.get("alert_horizon_min", -1)) == int(ALERT_HORIZON_MIN)
    )


def labels_shard_cache_is_hit(
    *,
    shard_manifest_path: Path,
    shard_data_path: Path,
    entity_set_fingerprint: str,
    label_semantic_fp: str,
    month: str,
    canonical_shard: int,
) -> bool:
    """Return True when one month×shard labels cache entry is fresh."""
    if not Path(shard_data_path).is_file():
        return False
    prev = load_labels_manifest(shard_manifest_path)
    if prev is None:
        return False
    return (
        str(prev.get("entity_set_fingerprint")) == str(entity_set_fingerprint)
        and str(prev.get("label_semantic_fingerprint")) == str(label_semantic_fp)
        and str(prev.get("month", "")).strip() == str(month).strip()
        and int(prev.get("canonical_shard", -1)) == int(canonical_shard)
        and int(prev.get("walkaway_gap_min", -1)) == int(WALKAWAY_GAP_MIN)
        and int(prev.get("alert_horizon_min", -1)) == int(ALERT_HORIZON_MIN)
    )


def _labels_row_count(path: Path) -> int:
    """Return parquet row count for labels output."""
    p = Path(path).resolve()
    meta = pq.ParquetFile(p).metadata
    return int(meta.num_rows) if meta is not None else 0


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _query_global_label_window_ends(
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return global ``(window_end, extended_end)`` over all joined bets."""
    df = load_joined_bets_dataframe(
        cleaned_bet_parquet,
        canonical_mapping_parquet,
        duckdb_runtime=duckdb_runtime,
    )
    if df.empty:
        now = pd.Timestamp.utcnow().tz_localize(None)
        return now, now
    max_pcd = pd.Timestamp(df["payout_complete_dtm"].max())
    return max_pcd, max_pcd


def _write_labels_shard_manifest(
    *,
    shard_dir: Path,
    entity_set_fingerprint: str,
    label_semantic_fp: str,
    month: str,
    canonical_shard: int,
    row_count: int,
    data_path: Path,
) -> Path:
    """Write per-shard manifest JSON."""
    manifest_path = Path(shard_dir).resolve() / "manifest.json"
    payload = {
        "schema_version": LABELS_SHARDED_SCHEMA_VERSION,
        "kind": LABELS_SHARDED_KIND,
        "month": str(month),
        "canonical_shard": int(canonical_shard),
        "entity_set_fingerprint": str(entity_set_fingerprint),
        "label_semantic_fingerprint": str(label_semantic_fp),
        "walkaway_gap_min": int(WALKAWAY_GAP_MIN),
        "alert_horizon_min": int(ALERT_HORIZON_MIN),
        "row_count": int(row_count),
        "data_path": str(Path(data_path).resolve()),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(manifest_path, payload)
    return manifest_path


def _materialize_labels_shard(
    *,
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    policy_dir: Path,
    month: str,
    canonical_shard: int,
    shard_count: int,
    entity_set_fingerprint: str,
    label_semantic_fp: str,
    window_end: pd.Timestamp,
    extended_end: pd.Timestamp,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Materialize one month×canonical_shard labels parquet."""
    shard_dir = labels_shard_dir(
        policy_dir=policy_dir,
        month=month,
        canonical_shard=canonical_shard,
    )
    shard_dir.mkdir(parents=True, exist_ok=True)
    data_path = shard_dir / "data.parquet"
    joined = load_joined_bets_dataframe(
        cleaned_bet_parquet,
        canonical_mapping_parquet,
        duckdb_runtime=duckdb_runtime,
        payout_yyyymm=month,
        canonical_shard=canonical_shard,
        canonical_shard_count=shard_count,
    )
    write_walkaway_labels_from_joined_dataframe(
        joined,
        data_path,
        window_end=window_end,
        extended_end=extended_end,
    )
    row_n = _labels_row_count(data_path)
    _write_labels_shard_manifest(
        shard_dir=shard_dir,
        entity_set_fingerprint=entity_set_fingerprint,
        label_semantic_fp=label_semantic_fp,
        month=month,
        canonical_shard=canonical_shard,
        row_count=row_n,
        data_path=data_path,
    )
    return data_path


def _assemble_labels_shards(
    shard_paths: tuple[Path, ...],
    *,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> int:
    """Merge shard parquets into the legacy monolithic labels output."""
    paths = [Path(p).resolve() for p in shard_paths if Path(p).is_file()]
    if not paths:
        empty = pd.DataFrame(
            {
                "bet_id": pd.Series(dtype="float64"),
                "canonical_id": pd.Series(dtype="string"),
                "payout_complete_dtm": pd.Series(dtype="datetime64[ns]"),
                "label": pd.Series(dtype="int8"),
                "censored": pd.Series(dtype=bool),
            }
        )
        dst = Path(out_parquet).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        empty.to_parquet(dst, index=False)
        return 0
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst_esc = _path_esc(dst)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        if len(paths) == 1:
            src_esc = _path_esc(paths[0])
            con.execute(
                f"COPY (SELECT * FROM read_parquet('{src_esc}')) "
                f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
            )
        else:
            paths_esc = "[" + ",".join(f"'{_path_esc(p)}'" for p in paths) + "]"
            con.execute(
                f"COPY (SELECT * FROM read_parquet({paths_esc})) "
                f"TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
            )
        row_n = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{dst_esc}')").fetchone()[0])
    finally:
        con.close()
    return row_n


def materialize_labels_v1_sharded_cached(
    *,
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    entity_set_fingerprint: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    cache_root: Path | None = None,
    out_parquet: Path | None = None,
    invalid_months: tuple[str, ...] = (),
    canonical_shard_count: int = LABELS_CANONICAL_SHARD_COUNT,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Materialize labels per ``month × canonical_shard``; assemble monolithic output."""
    t0 = time.perf_counter()
    entity_fp = str(entity_set_fingerprint).strip()
    if not entity_fp:
        raise ValueError("entity_set_fingerprint must be non-empty")
    shard_n = int(canonical_shard_count)
    if shard_n < 1:
        raise ValueError(f"canonical_shard_count must be >= 1, got {shard_n}")
    semantic_fp = label_semantic_fingerprint()
    dst = Path(out_parquet).resolve() if out_parquet is not None else default_walkaway_labels_parquet_path()
    root = default_labels_cache_root() if cache_root is None else Path(cache_root).resolve()
    policy = labels_policy_dir(cache_root=root, entity_set_fingerprint=entity_fp)
    policy.mkdir(parents=True, exist_ok=True)
    aggregate_path = policy / AGGREGATE_MANIFEST_NAME
    months = list_joined_bet_payout_months(
        cleaned_bet_parquet,
        canonical_mapping_parquet,
        duckdb_runtime=duckdb_runtime,
    )
    invalid_set = {str(m).strip() for m in invalid_months if str(m).strip()}
    window_end, extended_end = _query_global_label_window_ends(
        cleaned_bet_parquet,
        canonical_mapping_parquet,
        duckdb_runtime=duckdb_runtime,
    )
    hit_shards: list[str] = []
    miss_shards: list[str] = []
    shard_paths: list[Path] = []
    for month in months:
        for shard in range(shard_n):
            shard_dir = labels_shard_dir(policy_dir=policy, month=month, canonical_shard=shard)
            data_path = shard_dir / "data.parquet"
            manifest_path = shard_dir / "manifest.json"
            force = month in invalid_set
            cache_hit = (
                use_cache
                and not force
                and labels_shard_cache_is_hit(
                    shard_manifest_path=manifest_path,
                    shard_data_path=data_path,
                    entity_set_fingerprint=entity_fp,
                    label_semantic_fp=semantic_fp,
                    month=month,
                    canonical_shard=shard,
                )
            )
            key = f"{month}:{shard}"
            if cache_hit:
                hit_shards.append(key)
            else:
                miss_shards.append(key)
                data_path = _materialize_labels_shard(
                    cleaned_bet_parquet=cleaned_bet_parquet,
                    canonical_mapping_parquet=canonical_mapping_parquet,
                    policy_dir=policy,
                    month=month,
                    canonical_shard=shard,
                    shard_count=shard_n,
                    entity_set_fingerprint=entity_fp,
                    label_semantic_fp=semantic_fp,
                    window_end=window_end,
                    extended_end=extended_end,
                    duckdb_runtime=duckdb_runtime,
                )
            shard_paths.append(data_path)
    row_n = _assemble_labels_shards(
        tuple(shard_paths),
        out_parquet=dst,
        duckdb_runtime=duckdb_runtime,
    )
    aggregate = {
        "schema_version": LABELS_SHARDED_SCHEMA_VERSION,
        "kind": LABELS_SHARDED_KIND,
        "grain": "month_x_canonical_shard",
        "entity_set_fingerprint": entity_fp,
        "label_semantic_fingerprint": semantic_fp,
        "walkaway_gap_min": int(WALKAWAY_GAP_MIN),
        "alert_horizon_min": int(ALERT_HORIZON_MIN),
        "canonical_shard_count": int(shard_n),
        "label_months": list(months),
        "invalid_months": list(invalid_set),
        "window_end": str(window_end),
        "extended_end": str(extended_end),
        "row_count": int(row_n),
        "labels_parquet_path": str(dst.resolve()),
        "hit_shards": hit_shards,
        "miss_shards": miss_shards,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(aggregate_path, aggregate)
    full_hit = len(miss_shards) == 0 and bool(months)
    return {
        "labels_cache_hit": bool(full_hit),
        "labels_cache_elapsed_seconds": round(time.perf_counter() - t0, 6),
        "labels_row_count": int(row_n),
        "labels_manifest_path": str(aggregate_path.resolve()),
        "labels_parquet_path": str(dst.resolve()),
        "labels_invalid_months": sorted(invalid_set),
        "labels_semantic_fingerprint": semantic_fp,
        "labels_grain": "month_x_canonical_shard",
        "labels_shard_count": int(shard_n) * len(months),
        "labels_cache_hit_shards": hit_shards,
        "labels_cache_miss_shards": miss_shards,
        "labels_sharded": True,
    }


def materialize_labels_v1_cached(
    *,
    cleaned_bet_parquet: Path,
    canonical_mapping_parquet: Path,
    entity_set_fingerprint: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    cache_root: Path | None = None,
    out_parquet: Path | None = None,
    invalid_months: tuple[str, ...] = (),
    use_cache: bool = True,
    use_sharded_cache: bool = False,
    canonical_shard_count: int = LABELS_CANONICAL_SHARD_COUNT,
) -> dict[str, Any]:
    """Materialize or reuse walkaway labels; optional month×shard cache (trainer off by default)."""
    if use_sharded_cache:
        return materialize_labels_v1_sharded_cached(
            cleaned_bet_parquet=cleaned_bet_parquet,
            canonical_mapping_parquet=canonical_mapping_parquet,
            entity_set_fingerprint=entity_set_fingerprint,
            duckdb_runtime=duckdb_runtime,
            cache_root=cache_root,
            out_parquet=out_parquet,
            invalid_months=invalid_months,
            canonical_shard_count=canonical_shard_count,
            use_cache=use_cache,
        )
    t0 = time.perf_counter()
    entity_fp = str(entity_set_fingerprint).strip()
    if not entity_fp:
        raise ValueError("entity_set_fingerprint must be non-empty")
    semantic_fp = label_semantic_fingerprint()
    dst = Path(out_parquet).resolve() if out_parquet is not None else default_walkaway_labels_parquet_path()
    root = default_labels_cache_root() if cache_root is None else Path(cache_root).resolve()
    policy = labels_policy_dir(cache_root=root, entity_set_fingerprint=entity_fp)
    policy.mkdir(parents=True, exist_ok=True)
    manifest_path = policy / "manifest.json"
    cache_hit = use_cache and labels_cache_is_hit(
        manifest_path=manifest_path,
        labels_parquet_path=dst,
        entity_set_fingerprint=entity_fp,
        label_semantic_fp=semantic_fp,
    )
    if not cache_hit:
        materialize_walkaway_labels_from_cleaned_bet(
            cleaned_bet_parquet=cleaned_bet_parquet,
            canonical_mapping_parquet=canonical_mapping_parquet,
            out_parquet=dst,
            duckdb_runtime=duckdb_runtime,
        )
        row_n = _labels_row_count(dst)
        manifest = {
            "schema_version": LABELS_SCHEMA_VERSION,
            "kind": LABELS_KIND,
            "entity_set_fingerprint": entity_fp,
            "label_semantic_fingerprint": semantic_fp,
            "walkaway_gap_min": int(WALKAWAY_GAP_MIN),
            "alert_horizon_min": int(ALERT_HORIZON_MIN),
            "invalid_months": list(invalid_months),
            "row_count": int(row_n),
            "labels_parquet_path": str(dst.resolve()),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json_atomic(manifest_path, manifest)
    else:
        prev = load_labels_manifest(manifest_path) or {}
        row_n = int(prev.get("row_count") or _labels_row_count(dst))
    return {
        "labels_cache_hit": bool(cache_hit),
        "labels_cache_elapsed_seconds": round(time.perf_counter() - t0, 6),
        "labels_row_count": int(row_n),
        "labels_manifest_path": str(manifest_path.resolve()),
        "labels_parquet_path": str(dst.resolve()),
        "labels_invalid_months": list(invalid_months),
        "labels_semantic_fingerprint": semantic_fp,
        "labels_sharded": False,
    }
