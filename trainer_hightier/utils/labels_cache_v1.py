"""Walkaway labels cache v1 (L4): manifest + cache hit around existing label materializer."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DEFAULT_WALKAWAY_LABEL_CONTRACT,
    DuckDbRuntimeConfig,
    LABELS_CACHE_READONLY_BACKUP_DIRNAME,
    LABELS_CANONICAL_SHARD_COUNT,
    WALKAWAY_GAP_MIN,
    WALKAWAY_LABELS_READONLY_BACKUP_BASENAME,
    WalkawayLabelContract,
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


def label_semantic_fingerprint(
    label_contract: WalkawayLabelContract | None = None,
) -> str:
    """SHA-256 over label compute module bytes + gap/horizon constants."""
    contract = label_contract or DEFAULT_WALKAWAY_LABEL_CONTRACT
    mod = Path(__file__).resolve().parents[1] / "walkaway_compute_labels.py"
    digest = hashlib.sha256(mod.read_bytes()).hexdigest()
    blob = (
        f"{digest}|gap={int(contract.walkaway_gap_min)}"
        f"|horizon={int(contract.alert_horizon_min)}"
        f"|extended_end_policy=v2"
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _make_path_tree_readonly(root: Path) -> None:
    """Best-effort read-only chmod for a copied backup tree (Windows + POSIX)."""
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_file():
            path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(
                stat.S_IREAD
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH,
            )


def backup_existing_labels_cache_readonly(
    *,
    cache_root: Path | None = None,
    labels_parquet: Path | None = None,
) -> dict[str, str | None]:
    """Copy pre–gap-partition ``labels_v1`` and default labels parquet once (idempotent).

    Returns paths written (or ``None`` when source missing / backup already exists).
    """
    root = default_labels_cache_root() if cache_root is None else Path(cache_root).resolve()
    cache_parent = root.parent
    src_cache = root
    dst_cache = cache_parent / LABELS_CACHE_READONLY_BACKUP_DIRNAME
    out: dict[str, str | None] = {
        "labels_cache_backup_path": None,
        "labels_parquet_backup_path": None,
    }
    if src_cache.is_dir() and not dst_cache.exists():
        shutil.copytree(src_cache, dst_cache)
        _make_path_tree_readonly(dst_cache)
        out["labels_cache_backup_path"] = str(dst_cache.resolve())
        logger.info(
            "labels cache readonly backup created at %s",
            dst_cache.resolve(),
        )
    elif dst_cache.is_dir():
        out["labels_cache_backup_path"] = str(dst_cache.resolve())

    default_labels = (
        default_walkaway_labels_parquet_path()
        if labels_parquet is None
        else Path(labels_parquet).resolve()
    )
    if default_labels.is_file():
        backup_labels = default_labels.parent / WALKAWAY_LABELS_READONLY_BACKUP_BASENAME
        if not backup_labels.is_file():
            shutil.copy2(default_labels, backup_labels)
            backup_labels.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            logger.info(
                "walkaway labels parquet readonly backup created at %s",
                backup_labels.resolve(),
            )
        out["labels_parquet_backup_path"] = str(backup_labels.resolve())
    return out


def labels_policy_dir(
    *,
    cache_root: Path,
    entity_set_fingerprint: str,
    walkaway_gap_min: int,
) -> Path:
    """Directory for one labels cache policy keyed by gap and entity set fingerprint."""
    fp = str(entity_set_fingerprint).strip()
    if not fp:
        raise ValueError("entity_set_fingerprint must be non-empty")
    gap = int(walkaway_gap_min)
    if gap < 1:
        raise ValueError(f"walkaway_gap_min must be >= 1, got {walkaway_gap_min!r}")
    return Path(cache_root).resolve() / f"gap={gap}" / f"entity_set={fp[:16]}"


def legacy_labels_policy_dir(
    *,
    cache_root: Path,
    entity_set_fingerprint: str,
) -> Path:
    """Pre–Phase-1 layout: ``entity_set=`` only (implicit gap=30)."""
    fp = str(entity_set_fingerprint).strip()
    if not fp:
        raise ValueError("entity_set_fingerprint must be non-empty")
    return Path(cache_root).resolve() / f"entity_set={fp[:16]}"


def labels_policy_lookup_dirs(
    *,
    cache_root: Path,
    entity_set_fingerprint: str,
    walkaway_gap_min: int,
) -> tuple[Path, ...]:
    """Return policy dirs to consult for cache hits (primary gap-partitioned, then legacy gap=30)."""
    primary = labels_policy_dir(
        cache_root=cache_root,
        entity_set_fingerprint=entity_set_fingerprint,
        walkaway_gap_min=walkaway_gap_min,
    )
    if int(walkaway_gap_min) != int(WALKAWAY_GAP_MIN):
        return (primary,)
    legacy = legacy_labels_policy_dir(
        cache_root=cache_root,
        entity_set_fingerprint=entity_set_fingerprint,
    )
    if legacy.is_dir():
        return (primary, legacy)
    return (primary,)


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
    label_contract: WalkawayLabelContract,
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
        and int(prev.get("walkaway_gap_min", -1)) == int(label_contract.walkaway_gap_min)
        and int(prev.get("alert_horizon_min", -1)) == int(label_contract.alert_horizon_min)
    )


def _monolithic_labels_cache_is_hit(
    *,
    policy_dirs: tuple[Path, ...],
    labels_parquet_path: Path,
    entity_set_fingerprint: str,
    label_semantic_fp: str,
    label_contract: WalkawayLabelContract,
) -> tuple[bool, Path | None]:
    """Check monolithic manifest across primary and legacy policy directories."""
    for policy in policy_dirs:
        manifest_path = policy / "manifest.json"
        if labels_cache_is_hit(
            manifest_path=manifest_path,
            labels_parquet_path=labels_parquet_path,
            entity_set_fingerprint=entity_set_fingerprint,
            label_semantic_fp=label_semantic_fp,
            label_contract=label_contract,
        ):
            return True, policy
    return False, None


def labels_shard_cache_is_hit(
    *,
    shard_manifest_path: Path,
    shard_data_path: Path,
    entity_set_fingerprint: str,
    label_semantic_fp: str,
    month: str,
    canonical_shard: int,
    label_contract: WalkawayLabelContract,
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
        and int(prev.get("walkaway_gap_min", -1)) == int(label_contract.walkaway_gap_min)
        and int(prev.get("alert_horizon_min", -1)) == int(label_contract.alert_horizon_min)
    )


def _resolve_shard_cache_paths(
    *,
    policy_dirs: tuple[Path, ...],
    month: str,
    canonical_shard: int,
    entity_set_fingerprint: str,
    label_semantic_fp: str,
    label_contract: WalkawayLabelContract,
) -> tuple[Path, Path] | None:
    """Return ``(shard_dir, data_path)`` for the first matching shard cache hit."""
    for policy in policy_dirs:
        shard_dir = labels_shard_dir(
            policy_dir=policy,
            month=month,
            canonical_shard=canonical_shard,
        )
        data_path = shard_dir / "data.parquet"
        if labels_shard_cache_is_hit(
            shard_manifest_path=shard_dir / "manifest.json",
            shard_data_path=data_path,
            entity_set_fingerprint=entity_set_fingerprint,
            label_semantic_fp=label_semantic_fp,
            month=month,
            canonical_shard=canonical_shard,
            label_contract=label_contract,
        ):
            return shard_dir, data_path
    return None


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
    label_contract: WalkawayLabelContract,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Return global ``(window_end, extended_end)`` over all joined bets."""
    from trainer_hightier.utils.walkaway_labels import label_window_ends_from_max_payout

    df = load_joined_bets_dataframe(
        cleaned_bet_parquet,
        canonical_mapping_parquet,
        duckdb_runtime=duckdb_runtime,
    )
    if df.empty:
        now = pd.Timestamp.utcnow().tz_localize(None)
        return now, now
    max_pcd = pd.Timestamp(df["payout_complete_dtm"].max())
    return label_window_ends_from_max_payout(max_pcd, label_contract=label_contract)


def _write_labels_shard_manifest(
    *,
    shard_dir: Path,
    entity_set_fingerprint: str,
    label_semantic_fp: str,
    month: str,
    canonical_shard: int,
    row_count: int,
    data_path: Path,
    label_contract: WalkawayLabelContract,
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
        "label_contract_id": str(label_contract.contract_id),
        "walkaway_gap_min": int(label_contract.walkaway_gap_min),
        "alert_horizon_min": int(label_contract.alert_horizon_min),
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
    label_contract: WalkawayLabelContract,
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
        label_contract=label_contract,
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
        label_contract=label_contract,
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
    label_contract: WalkawayLabelContract | None = None,
) -> dict[str, Any]:
    """Materialize labels per ``month × canonical_shard``; assemble monolithic output."""
    contract = label_contract or DEFAULT_WALKAWAY_LABEL_CONTRACT
    backup_existing_labels_cache_readonly(cache_root=cache_root, labels_parquet=out_parquet)
    t0 = time.perf_counter()
    entity_fp = str(entity_set_fingerprint).strip()
    if not entity_fp:
        raise ValueError("entity_set_fingerprint must be non-empty")
    shard_n = int(canonical_shard_count)
    if shard_n < 1:
        raise ValueError(f"canonical_shard_count must be >= 1, got {shard_n}")
    semantic_fp = label_semantic_fingerprint(contract)
    dst = (
        Path(out_parquet).resolve()
        if out_parquet is not None
        else default_walkaway_labels_parquet_path(label_contract=contract)
    )
    root = default_labels_cache_root() if cache_root is None else Path(cache_root).resolve()
    policy_dirs = labels_policy_lookup_dirs(
        cache_root=root,
        entity_set_fingerprint=entity_fp,
        walkaway_gap_min=int(contract.walkaway_gap_min),
    )
    write_policy = labels_policy_dir(
        cache_root=root,
        entity_set_fingerprint=entity_fp,
        walkaway_gap_min=int(contract.walkaway_gap_min),
    )
    write_policy.mkdir(parents=True, exist_ok=True)
    aggregate_path = write_policy / AGGREGATE_MANIFEST_NAME
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
        label_contract=contract,
    )
    hit_shards: list[str] = []
    miss_shards: list[str] = []
    shard_paths: list[Path] = []
    for month in months:
        for shard in range(shard_n):
            force = month in invalid_set
            resolved = None if force else _resolve_shard_cache_paths(
                policy_dirs=policy_dirs,
                month=month,
                canonical_shard=shard,
                entity_set_fingerprint=entity_fp,
                label_semantic_fp=semantic_fp,
                label_contract=contract,
            )
            key = f"{month}:{shard}"
            if use_cache and resolved is not None:
                hit_shards.append(key)
                shard_paths.append(resolved[1])
                continue
            miss_shards.append(key)
            data_path = _materialize_labels_shard(
                cleaned_bet_parquet=cleaned_bet_parquet,
                canonical_mapping_parquet=canonical_mapping_parquet,
                policy_dir=write_policy,
                month=month,
                canonical_shard=shard,
                shard_count=shard_n,
                entity_set_fingerprint=entity_fp,
                label_semantic_fp=semantic_fp,
                window_end=window_end,
                extended_end=extended_end,
                duckdb_runtime=duckdb_runtime,
                label_contract=contract,
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
        "label_contract_id": str(contract.contract_id),
        "walkaway_gap_min": int(contract.walkaway_gap_min),
        "alert_horizon_min": int(contract.alert_horizon_min),
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
        "label_contract_id": str(contract.contract_id),
        "walkaway_gap_min": int(contract.walkaway_gap_min),
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
    label_contract: WalkawayLabelContract | None = None,
) -> dict[str, Any]:
    """Materialize or reuse walkaway labels; optional month×shard cache (trainer off by default)."""
    contract = label_contract or DEFAULT_WALKAWAY_LABEL_CONTRACT
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
            label_contract=contract,
        )
    backup_existing_labels_cache_readonly(cache_root=cache_root, labels_parquet=out_parquet)
    t0 = time.perf_counter()
    entity_fp = str(entity_set_fingerprint).strip()
    if not entity_fp:
        raise ValueError("entity_set_fingerprint must be non-empty")
    semantic_fp = label_semantic_fingerprint(contract)
    dst = (
        Path(out_parquet).resolve()
        if out_parquet is not None
        else default_walkaway_labels_parquet_path(label_contract=contract)
    )
    root = default_labels_cache_root() if cache_root is None else Path(cache_root).resolve()
    policy_dirs = labels_policy_lookup_dirs(
        cache_root=root,
        entity_set_fingerprint=entity_fp,
        walkaway_gap_min=int(contract.walkaway_gap_min),
    )
    write_policy = labels_policy_dir(
        cache_root=root,
        entity_set_fingerprint=entity_fp,
        walkaway_gap_min=int(contract.walkaway_gap_min),
    )
    write_policy.mkdir(parents=True, exist_ok=True)
    cache_hit = False
    hit_policy: Path | None = None
    if use_cache:
        cache_hit, hit_policy = _monolithic_labels_cache_is_hit(
            policy_dirs=policy_dirs,
            labels_parquet_path=dst,
            entity_set_fingerprint=entity_fp,
            label_semantic_fp=semantic_fp,
            label_contract=contract,
        )
    manifest_path = (hit_policy or write_policy) / "manifest.json"
    if not cache_hit:
        materialize_walkaway_labels_from_cleaned_bet(
            cleaned_bet_parquet=cleaned_bet_parquet,
            canonical_mapping_parquet=canonical_mapping_parquet,
            out_parquet=dst,
            duckdb_runtime=duckdb_runtime,
            label_contract=contract,
        )
        row_n = _labels_row_count(dst)
        manifest = {
            "schema_version": LABELS_SCHEMA_VERSION,
            "kind": LABELS_KIND,
            "entity_set_fingerprint": entity_fp,
            "label_semantic_fingerprint": semantic_fp,
            "label_contract_id": str(contract.contract_id),
            "walkaway_gap_min": int(contract.walkaway_gap_min),
            "alert_horizon_min": int(contract.alert_horizon_min),
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
        "label_contract_id": str(contract.contract_id),
        "walkaway_gap_min": int(contract.walkaway_gap_min),
        "labels_sharded": False,
    }
