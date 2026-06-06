"""Content-addressed source parquet manifest (Phase 1: observability only)."""

from __future__ import annotations

import hashlib
import json
import logging
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
CACHE_REPORT_SCHEMA_VERSION: Final[int] = 1
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

    return {
        "source_manifest_v2_elapsed_seconds": round(time.perf_counter() - t0, 6),
        "source_manifest_v2_hashed_bytes": int(hashed_bytes),
        "source_manifest_v2_hash_elapsed_seconds": round(hash_elapsed, 6),
        "source_manifest_v2_diff_summary": diff.summary(),
        "source_manifest_v2_changed_partitions": changed_partitions,
        "source_manifest_v2_change_set_path": str(change_set_path.resolve()),
        "source_manifest_v2_cache_report_path": str(cache_report_path.resolve()),
        "source_manifest_v2_current_path": str(current_path.resolve()),
    }
