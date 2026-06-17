"""Content-addressed source parquet manifest (Phase 1: observability only)."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import pyarrow.parquet as pq

from trainer_hightier.utils.partition_inventory import (
    PartitionParquetStat,
    detect_partition_layout,
    infer_snapshot_id,
    scan_partition_snapshot_dir,
)

logger = logging.getLogger("trainer_hightier")

SOURCE_FILE_RECORD_SCHEMA_VERSION: Final[int] = 2
SOURCE_MANIFEST_SCHEMA_VERSION: Final[int] = 1
SOURCE_CHANGE_SET_SCHEMA_VERSION: Final[int] = 1
CACHE_REPORT_SCHEMA_VERSION: Final[int] = 2
HASH_ALGORITHM: Final[str] = "sha256_file_bytes_v1"
MANIFEST_KIND: Final[str] = "trainer_hightier_source_manifest_v2"

CHANGE_ADDED: Final[str] = "added"
CHANGE_REMOVED: Final[str] = "removed"
CHANGE_MODIFIED: Final[str] = "modified"
CHANGE_UNCHANGED: Final[str] = "unchanged"


@dataclass(frozen=True)
class SourceManifestDiff:
    """Diff result between previous and current source manifests."""

    added: tuple[dict[str, Any], ...]
    removed: tuple[dict[str, Any], ...]
    modified: tuple[dict[str, Any], ...]
    unchanged: tuple[dict[str, Any], ...]

    def summary(self) -> dict[str, int]:
        """Return counts for added/removed/modified/unchanged."""
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "modified": len(self.modified),
            "unchanged": len(self.unchanged),
        }


def default_cache_root(*, package_dir: Path | None = None) -> Path:
    """Return ``trainer_hightier/artifacts/cache``."""
    base = Path(package_dir).resolve() if package_dir is not None else Path(__file__).resolve().parents[1]
    return (base / "artifacts" / "cache").resolve()


def sha256_file_bytes(path: Path) -> str:
    """Return SHA-256 hex digest of file bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def schema_sha256_hex(path: Path) -> str:
    """Return SHA-256 hex of parquet schema serialized bytes."""
    schema = pq.read_schema(Path(path).resolve())
    return hashlib.sha256(schema.serialize().to_pybytes()).hexdigest()


def relative_path_under_snapshot(path: Path, snapshot_dir: Path) -> str:
    """POSIX relative path from snapshot root."""
    rel = Path(path).resolve().relative_to(Path(snapshot_dir).resolve())
    return rel.as_posix()


def validate_partition_yyyymm(yyyymm: str, *, path: Path) -> str:
    """Validate six-digit ``YYYYMM`` or raise."""
    ym = str(yyyymm).strip()
    if len(ym) != 6 or not ym.isdigit():
        raise ValueError(f"partition_yyyymm must be six digits for {path}; got {ym!r}")
    return ym


def build_source_file_record(
    stat: PartitionParquetStat,
    *,
    snapshot_dir: Path,
    file_sha256: str,
) -> dict[str, Any]:
    """Build one content-addressed file record."""
    p = Path(stat.path).resolve()
    meta = pq.ParquetFile(p).metadata
    row_groups = int(meta.num_row_groups) if meta is not None else 0
    ym = validate_partition_yyyymm(stat.yyyymm, path=p)
    return {
        "schema_version": SOURCE_FILE_RECORD_SCHEMA_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "table": str(stat.role),
        "relative_path": relative_path_under_snapshot(p, snapshot_dir),
        "partition_yyyymm": ym,
        "size_bytes": int(stat.size_bytes),
        "num_rows": int(stat.num_rows),
        "row_group_count": row_groups,
        "schema_sha256": schema_sha256_hex(p),
        "file_sha256": str(file_sha256),
        "mtime_ns_diagnostic": int(stat.mtime_ns),
    }


def hash_partition_stats(
    stats: list[PartitionParquetStat],
    *,
    snapshot_dir: Path,
) -> tuple[list[dict[str, Any]], int, float]:
    """Hash partition stats; return sorted records, total bytes, elapsed seconds."""
    t0 = time.perf_counter()
    records: list[dict[str, Any]] = []
    hashed_bytes = 0
    for stat in stats:
        p = Path(stat.path).resolve()
        digest = sha256_file_bytes(p)
        hashed_bytes += int(stat.size_bytes)
        records.append(build_source_file_record(stat, snapshot_dir=snapshot_dir, file_sha256=digest))
    records.sort(key=lambda r: (str(r["table"]), str(r["relative_path"])))
    return records, hashed_bytes, time.perf_counter() - t0


def build_source_manifest_v2(
    *,
    snapshot_dir: Path,
    snapshot_id: str,
    bet_stats: list[PartitionParquetStat] | None = None,
    session_stats: list[PartitionParquetStat] | None = None,
) -> tuple[dict[str, Any], int, float]:
    """Scan snapshot parquets and build a content-addressed manifest."""
    sd = Path(snapshot_dir).resolve()
    if bet_stats is None or session_stats is None:
        bet_stats, session_stats = scan_partition_snapshot_dir(sd)
    all_stats = list(bet_stats) + list(session_stats)
    files, hashed_bytes, hash_elapsed = hash_partition_stats(all_stats, snapshot_dir=sd)
    manifest = {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "snapshot_id": str(snapshot_id),
        "snapshot_dir": str(sd),
        "layout_kind": detect_partition_layout(sd),
        "hash_algorithm": HASH_ALGORITHM,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "hash_elapsed_seconds": round(hash_elapsed, 6),
        "hashed_bytes": int(hashed_bytes),
        "files": files,
    }
    return manifest, hashed_bytes, hash_elapsed


def aggregate_source_files_fingerprint_sha256_hex(manifest: dict[str, Any]) -> str:
    """SHA-256 hex over sorted ``(table, relative_path, file_sha256)`` tuples."""
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest.files must be a list for aggregate fingerprint")
    tuples = [
        (
            str(row["table"]),
            str(row["relative_path"]),
            str(row["file_sha256"]),
        )
        for row in files
        if isinstance(row, dict)
    ]
    tuples.sort()
    blob = json.dumps(tuples, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_source_manifest_v2(path: Path) -> dict[str, Any] | None:
    """Load manifest JSON; return ``None`` when missing or corrupt."""
    p = Path(path).resolve()
    if not p.is_file():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Corrupt source manifest v2 at %s; treating as missing.", p)
        return None
    if not isinstance(obj, dict) or "files" not in obj:
        logger.warning("Invalid source manifest v2 shape at %s; treating as missing.", p)
        return None
    return obj


def _file_index(files: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Index file records by ``(table, relative_path)``."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in files:
        key = (str(rec["table"]), str(rec["relative_path"]))
        out[key] = rec
    return out


def diff_source_manifests(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> SourceManifestDiff:
    """Classify file records as added/removed/modified/unchanged."""
    prev_files = list((previous or {}).get("files") or [])
    cur_files = list(current.get("files") or [])
    prev_idx = _file_index(prev_files)
    cur_idx = _file_index(cur_files)
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    for key, rec in cur_idx.items():
        prev_rec = prev_idx.get(key)
        if prev_rec is None:
            added.append(rec)
        elif str(prev_rec.get("file_sha256")) != str(rec.get("file_sha256")):
            modified.append(rec)
        else:
            unchanged.append(rec)
    for key, rec in prev_idx.items():
        if key not in cur_idx:
            removed.append(rec)
    return SourceManifestDiff(
        added=tuple(added),
        removed=tuple(removed),
        modified=tuple(modified),
        unchanged=tuple(unchanged),
    )


def changed_files_from_diff(diff: SourceManifestDiff) -> list[dict[str, Any]]:
    """Flatten diff into changed file entries with ``change_kind``."""
    out: list[dict[str, Any]] = []
    for kind, rows in (
        (CHANGE_ADDED, diff.added),
        (CHANGE_REMOVED, diff.removed),
        (CHANGE_MODIFIED, diff.modified),
    ):
        for rec in rows:
            out.append(
                {
                    "table": str(rec["table"]),
                    "relative_path": str(rec["relative_path"]),
                    "partition_yyyymm": str(rec["partition_yyyymm"]),
                    "change_kind": kind,
                },
            )
    out.sort(key=lambda r: (str(r["table"]), str(r["relative_path"]), str(r["change_kind"])))
    return out


def changed_partitions_from_changed_files(
    changed_files: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Map changed files to sorted unique ``YYYYMM`` per table."""
    buckets: dict[str, set[str]] = {"t_bet": set(), "t_session": set()}
    for row in changed_files:
        table = str(row["table"])
        ym = str(row["partition_yyyymm"])
        if table in buckets:
            buckets[table].add(ym)
    return {k: sorted(v) for k, v in buckets.items()}


def write_json_atomic(path: Path, obj: dict[str, Any]) -> Path:
    """Atomically write JSON to *path*."""
    dest = Path(path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(obj, indent=2, sort_keys=True, default=str).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        prefix=dest.name + ".",
        suffix=".tmp.json",
        delete=False,
        dir=str(dest.parent),
    ) as tmp:
        tmp.write(blob)
        tmp_path = Path(tmp.name)
    try:
        tmp_path.replace(dest)
    finally:
        if tmp_path.is_file():
            try:
                tmp_path.unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                if tmp_path.exists():
                    tmp_path.unlink()
    return dest


def source_manifest_v2_dir(cache_root: Path) -> Path:
    """Return ``source_manifest_v2`` directory under cache root."""
    return Path(cache_root).resolve() / "source_manifest_v2"


def source_change_sets_dir(cache_root: Path) -> Path:
    """Return ``source_change_sets`` directory under cache root."""
    return Path(cache_root).resolve() / "source_change_sets"


def cache_reports_dir(cache_root: Path) -> Path:
    """Return ``reports`` directory under cache root."""
    return Path(cache_root).resolve() / "reports"


def publish_source_manifest_v2(
    *,
    cache_root: Path,
    current_manifest: dict[str, Any],
) -> tuple[Path, Path]:
    """Publish ``current.json`` and update ``previous.json`` baseline."""
    root = source_manifest_v2_dir(cache_root)
    current_path = write_json_atomic(root / "current.json", current_manifest)
    previous_path = write_json_atomic(root / "previous.json", current_manifest)
    return current_path, previous_path


def write_source_change_set(
    *,
    cache_root: Path,
    snapshot_id: str,
    snapshot_dir: Path,
    layout_kind: str,
    diff: SourceManifestDiff,
    hash_elapsed_seconds: float,
    hashed_bytes: int,
) -> Path:
    """Write ``source_change_set_<snapshot_id>_<utc>.json``."""
    changed_files = changed_files_from_diff(diff)
    utc_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_name = f"source_change_set_{snapshot_id}_{utc_tag}.json"
    payload = {
        "schema_version": SOURCE_CHANGE_SET_SCHEMA_VERSION,
        "snapshot_id": str(snapshot_id),
        "snapshot_dir": str(Path(snapshot_dir).resolve()),
        "layout_kind": str(layout_kind),
        "hash_algorithm": HASH_ALGORITHM,
        "diff_summary": diff.summary(),
        "changed_files": changed_files,
        "changed_partitions": changed_partitions_from_changed_files(changed_files),
        "hash_elapsed_seconds": round(float(hash_elapsed_seconds), 6),
        "hashed_bytes": int(hashed_bytes),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return write_json_atomic(source_change_sets_dir(cache_root) / out_name, payload)


def _cache_layer_entry(
    layer: str,
    *,
    hit: bool | None,
    elapsed_seconds: float | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build one cache-report layer dict when *hit* is known."""
    if hit is None:
        return None
    entry: dict[str, Any] = {
        "layer": layer,
        "hit": bool(hit),
        "miss": not bool(hit),
        "reason": reason,
        "elapsed_seconds": elapsed_seconds,
    }
    if extra:
        entry.update(extra)
    return entry


def build_pipeline_cache_layers(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Build L1–L5 layer entries from trainer *metrics* (Phase 2/3 observability)."""
    layers: list[dict[str, Any]] = []
    specs: list[tuple[str, str, str | None]] = [
        ("l1_session_clean", "session_clean_cache_hit", "session_clean_cache_miss"),
        ("l1_bet_base_clean", "bet_base_clean_cache_hit", "bet_base_clean_cache_miss"),
        ("l1_bet_segment_legacy", "bet_segment_clean_cache_hit", "bet_segment_clean_cache_miss"),
        ("l1_bet_clean", "bet_clean_cache_hit", "bet_clean_cache_miss"),
        ("l2_universe_adt_rank", "universe_adt_rank_cache_hit", "universe_adt_rank_cache_miss"),
        ("l3_entity_set_v1", "entity_set_cache_hit", "entity_set_cache_miss"),
        ("l4_walkaway_labels_v1", "labels_cache_hit", "labels_cache_miss"),
    ]
    for layer_name, hit_key, miss_reason in specs:
        if hit_key not in metrics:
            continue
        hit = bool(metrics.get(hit_key))
        elapsed_key = {
            "l2_universe_adt_rank": "universe_adt_rank_elapsed_seconds",
            "l3_entity_set_v1": "entity_set_elapsed_seconds",
            "l4_walkaway_labels_v1": "labels_cache_elapsed_seconds",
        }.get(layer_name)
        elapsed_raw = metrics.get(elapsed_key) if elapsed_key else None
        elapsed = float(elapsed_raw) if elapsed_raw is not None else None
        extra: dict[str, Any] | None = None
        if layer_name == "l4_walkaway_labels_v1":
            extra = {
                "invalid_months": metrics.get("labels_invalid_months"),
                "semantic_fingerprint": metrics.get("labels_semantic_fingerprint"),
                "grain": metrics.get("labels_grain"),
                "hit_shards": metrics.get("labels_cache_hit_shards"),
                "miss_shards": metrics.get("labels_cache_miss_shards"),
                "sharded": metrics.get("labels_sharded"),
            }
        if layer_name == "l3_entity_set_v1":
            extra = {
                "policy_fingerprint_sha256_hex": metrics.get(
                    "entity_set_policy_fingerprint_sha256_hex",
                ),
                "legacy_fallback_used": metrics.get("bet_segment_legacy_fallback_used"),
            }
        entry = _cache_layer_entry(
            layer_name,
            hit=hit,
            elapsed_seconds=elapsed,
            reason=None if hit else miss_reason,
            extra=extra,
        )
        if entry is not None:
            layers.append(entry)
    short_cache = metrics.get("main_trainer_fe_short_term_cache")
    if isinstance(short_cache, dict):
        hit = bool(short_cache.get("cache_hit"))
        layers.append(
            {
                "layer": "l5_short_term_pit_primitive",
                "hit": hit,
                "miss": not hit,
                "reason": None if hit else "short_term_pit_shard_miss",
                "elapsed_seconds": None,
                "hit_ratio": short_cache.get("short_term_pit_primitive_hit_ratio")
                or short_cache.get("cache_hit_ratio"),
                "hit_shards": short_cache.get("cache_hit_shards"),
                "miss_shards": short_cache.get("cache_miss_shards"),
                "reason_counts": short_cache.get("cache_reason_counts"),
                "recompute_months": metrics.get("short_term_pit_recompute_months"),
                "source_invalid_months": metrics.get("short_term_pit_source_invalid_months"),
                "entity_set_fingerprint_sha256_hex": metrics.get(
                    "entity_set_policy_fingerprint_sha256_hex",
                ),
            },
        )
    return layers


def finalize_cache_report_from_metrics(metrics: dict[str, Any]) -> Path | None:
    """Merge pipeline L1–L5 layers into the run ``cache_report_*.json``."""
    report_raw = metrics.get("source_manifest_v2_cache_report_path")
    if report_raw is None or not str(report_raw).strip():
        return None
    path = Path(str(report_raw)).resolve()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("[cache_report] skip finalize; corrupt JSON at %s", path)
        return None
    if not isinstance(payload, dict):
        return None
    existing = payload.get("layers")
    keep: list[dict[str, Any]] = []
    if isinstance(existing, list):
        keep = [row for row in existing if isinstance(row, dict) and row.get("layer") == "source_manifest_v2"]
    payload["schema_version"] = CACHE_REPORT_SCHEMA_VERSION
    payload["layers"] = keep + build_pipeline_cache_layers(metrics)
    payload["pipeline_layers_finalized_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload["l1_recompute_months"] = metrics.get("l1_recompute_months")
    return write_json_atomic(path, payload)


def write_cache_report_skeleton(
    *,
    cache_root: Path,
    run_id: str,
    change_set_path: Path,
    diff_summary: dict[str, int],
    changed_partitions: dict[str, list[str]],
    elapsed_seconds: float,
    hashed_bytes: int,
) -> Path:
    """Write ``cache_report_<run_id>.json`` observability skeleton."""
    payload = {
        "schema_version": CACHE_REPORT_SCHEMA_VERSION,
        "run_id": str(run_id),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "layers": [
            {
                "layer": "source_manifest_v2",
                "hit": False,
                "miss": True,
                "reason": "phase1_observability_only",
                "elapsed_seconds": round(float(elapsed_seconds), 6),
                "hashed_bytes": int(hashed_bytes),
            },
        ],
        "source_change_set_path": str(Path(change_set_path).resolve()),
        "diff_summary": dict(diff_summary),
        "changed_partitions": dict(changed_partitions),
    }
    safe_run_id = str(run_id).replace(":", "").replace("/", "_")
    return write_json_atomic(cache_reports_dir(cache_root) / f"cache_report_{safe_run_id}.json", payload)


def materialize_source_manifest_v2_phase1(
    *,
    snapshot_dir: Path,
    snapshot_id: str | None = None,
    cache_root: Path | None = None,
    run_id: str | None = None,
    bet_stats: list[PartitionParquetStat] | None = None,
    session_stats: list[PartitionParquetStat] | None = None,
) -> dict[str, Any]:
    """Run Phase 1 source manifest scan, diff, and report (read-only side effects)."""
    t0 = time.perf_counter()
    sd = Path(snapshot_dir).resolve()
    sid = str(snapshot_id or infer_snapshot_id(sd))
    root = default_cache_root() if cache_root is None else Path(cache_root).resolve()
    rid = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    current, hashed_bytes, hash_elapsed = build_source_manifest_v2(
        snapshot_dir=sd,
        snapshot_id=sid,
        bet_stats=bet_stats,
        session_stats=session_stats,
    )
    previous = load_source_manifest_v2(source_manifest_v2_dir(root) / "previous.json")
    diff = diff_source_manifests(previous, current)
    change_set_path = write_source_change_set(
        cache_root=root,
        snapshot_id=sid,
        snapshot_dir=sd,
        layout_kind=str(current.get("layout_kind") or ""),
        diff=diff,
        hash_elapsed_seconds=hash_elapsed,
        hashed_bytes=hashed_bytes,
    )
    changed_files = changed_files_from_diff(diff)
    changed_partitions = changed_partitions_from_changed_files(changed_files)
    cache_report_path = write_cache_report_skeleton(
        cache_root=root,
        run_id=rid,
        change_set_path=change_set_path,
        diff_summary=diff.summary(),
        changed_partitions=changed_partitions,
        elapsed_seconds=time.perf_counter() - t0,
        hashed_bytes=hashed_bytes,
    )
    current_path, _ = publish_source_manifest_v2(cache_root=root, current_manifest=current)
    aggregate_fp = aggregate_source_files_fingerprint_sha256_hex(current)

    return {
        "source_manifest_v2_elapsed_seconds": round(time.perf_counter() - t0, 6),
        "source_manifest_v2_aggregate_fingerprint_sha256_hex": aggregate_fp,
        "source_manifest_v2_hashed_bytes": int(hashed_bytes),
        "source_manifest_v2_hash_elapsed_seconds": round(hash_elapsed, 6),
        "source_manifest_v2_diff_summary": diff.summary(),
        "source_manifest_v2_changed_partitions": changed_partitions,
        "source_manifest_v2_change_set_path": str(change_set_path.resolve()),
        "source_manifest_v2_cache_report_path": str(cache_report_path.resolve()),
        "source_manifest_v2_current_path": str(current_path.resolve()),
    }


def default_artifacts_root(*, package_dir: Path | None = None) -> Path:
    """Return ``trainer_hightier/artifacts``."""
    base = Path(package_dir).resolve() if package_dir is not None else Path(__file__).resolve().parents[1]
    return (base / "artifacts").resolve()


def default_training_caches_backup_root(*, package_dir: Path | None = None) -> Path:
    """Return readonly full-cache backup root under ``artifacts/``."""
    from trainer_hightier.config import TRAINING_CACHES_READONLY_BACKUP_DIRNAME

    return default_artifacts_root(package_dir=package_dir) / TRAINING_CACHES_READONLY_BACKUP_DIRNAME


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


def _dir_size_bytes(root: Path) -> int:
    """Return total file bytes under ``root`` (best-effort)."""
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += int(path.stat().st_size)
            except OSError:
                continue
    return total


def _copy_tree_excluding(
    src: Path,
    dst: Path,
    *,
    exclude_dir_names: frozenset[str],
) -> None:
    """Copy directory tree, skipping immediate child directory names in ``exclude_dir_names``."""
    src_r = Path(src).resolve()
    dst_r = Path(dst).resolve()
    if not src_r.is_dir():
        raise FileNotFoundError(f"backup source directory not found: {src_r}")

    def _ignore(_directory: str, names: list[str]) -> list[str]:
        return [name for name in names if name in exclude_dir_names]

    shutil.copytree(src_r, dst_r, ignore=_ignore if exclude_dir_names else None)


def backup_all_training_caches_readonly(
    *,
    package_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Copy all training cache trees once into a readonly backup root (idempotent).

    Backs up:
    - ``artifacts/cache`` (L2–L4 shared caches; skips prior labels-only backup dir)
    - ``artifacts/training_data/cache`` (Feast month-group + short-term PIT)
    - ``artifacts/labels`` (walkaway labels parquet)
    - ``artifacts/cleaned`` (L1 cleaned bet/session)
    """
    from trainer_hightier.config import (
        LABELS_CACHE_READONLY_BACKUP_DIRNAME,
        TRAINING_CACHES_BACKUP_MANIFEST_BASENAME,
    )

    t0 = time.perf_counter()
    artifacts_root = default_artifacts_root(package_dir=package_dir)
    backup_root = default_training_caches_backup_root(package_dir=package_dir)
    manifest_path = backup_root / TRAINING_CACHES_BACKUP_MANIFEST_BASENAME
    if manifest_path.is_file() and not force:
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            prev = None
        if isinstance(prev, dict) and prev.get("status") == "completed":
            logger.info(
                "training caches readonly backup already exists at %s; skipping",
                backup_root.resolve(),
            )
            return prev

    specs: tuple[tuple[str, str, Path, frozenset[str]], ...] = (
        (
            "shared_l2_l4_cache",
            "cache",
            artifacts_root / "cache",
            frozenset({LABELS_CACHE_READONLY_BACKUP_DIRNAME}),
        ),
        (
            "step3_feast_and_short_pit_cache",
            "training_data/cache",
            artifacts_root / "training_data" / "cache",
            frozenset(),
        ),
        ("labels_parquet", "labels", artifacts_root / "labels", frozenset()),
        ("l1_cleaned_source", "cleaned", artifacts_root / "cleaned", frozenset()),
    )
    entries: list[dict[str, Any]] = []
    backup_root.mkdir(parents=True, exist_ok=True)
    for label, rel_dest, source, exclude in specs:
        dst = backup_root / rel_dest
        entry: dict[str, Any] = {
            "label": label,
            "source_path": str(source.resolve()),
            "backup_path": str(dst.resolve()),
        }
        if not source.exists():
            entry["status"] = "skipped_missing_source"
            entries.append(entry)
            logger.warning("cache backup skip missing source %s", source.resolve())
            continue
        if dst.exists() and not force:
            entry["status"] = "reused_existing"
            entry["size_bytes"] = _dir_size_bytes(dst)
            entries.append(entry)
            logger.info("cache backup reuse existing %s", dst.resolve())
            continue
        if dst.exists() and force:
            shutil.rmtree(dst)
        logger.info("cache backup copying %s -> %s", source.resolve(), dst.resolve())
        t_copy = time.perf_counter()
        _copy_tree_excluding(source, dst, exclude_dir_names=exclude)
        _make_path_tree_readonly(dst)
        entry["status"] = "copied"
        entry["copy_elapsed_seconds"] = round(time.perf_counter() - t_copy, 3)
        entry["size_bytes"] = _dir_size_bytes(dst)
        entries.append(entry)
        logger.info(
            "cache backup done %s size_bytes=%d elapsed=%.1fs",
            label,
            int(entry["size_bytes"]),
            float(entry["copy_elapsed_seconds"]),
        )

    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "training_caches_readonly_backup_v1",
        "status": "completed",
        "backup_root": str(backup_root.resolve()),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.perf_counter() - t0, 3),
        "entries": entries,
        "total_size_bytes": int(sum(int(e.get("size_bytes") or 0) for e in entries)),
    }
    write_json_atomic(manifest_path, payload)
    _make_path_tree_readonly(backup_root)
    logger.info(
        "training caches readonly backup completed at %s (%.1fs, %d bytes)",
        backup_root.resolve(),
        float(payload["elapsed_seconds"]),
        int(payload["total_size_bytes"]),
    )
    return payload
