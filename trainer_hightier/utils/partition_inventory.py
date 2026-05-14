"""Partition scanner, fingerprint manifest, and recompute-month selection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


_PART_MONTH_RE_BET = re.compile(r"t_bet__part_(\d{6})\.parquet$")
_PART_MONTH_RE_SESSION = re.compile(r"t_session__part_(\d{6})\.parquet$")


@dataclass(frozen=True)
class PartitionParquetStat:
    """One monthly partition parquet file fingerprint (v1: path/size/mtime/rows)."""

    path: Path
    yyyymm: str
    role: str
    mtime_ns: int
    size_bytes: int
    num_rows: int


def _stat_one_parquet(path: Path, role: str, month_extractor: re.Pattern[str]) -> PartitionParquetStat:
    """Build stats for one parquet filename; validates YYYYMM in name."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    name = p.name
    # Skip transient download suffixes user may omit from runs
    if name.endswith(".gstmp"):
        raise ValueError(f"Refusing incomplete parquet filename: {p}")
    m = month_extractor.match(name)
    if not m:
        raise ValueError(f"Filename does not match expected pattern {month_extractor.pattern!r}: {name}")
    ym = str(m.group(1))
    st = p.stat()
    meta = pq.ParquetFile(p).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    return PartitionParquetStat(
        path=p,
        yyyymm=ym,
        role=role,
        mtime_ns=int(st.st_mtime_ns),
        size_bytes=int(st.st_size),
        num_rows=nrows,
    )


def scan_partition_snapshot_dir(snapshot_dir: Path) -> tuple[list[PartitionParquetStat], list[PartitionParquetStat]]:
    """List bet and session partition parquets under a snapshot folder (sorted by month)."""
    root = Path(snapshot_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    bet_stats: list[PartitionParquetStat] = []
    sess_stats: list[PartitionParquetStat] = []
    for child in sorted(root.iterdir()):
        if not child.is_file() or not child.suffix.lower() == ".parquet":
            continue
        if child.name.startswith("t_bet__part_"):
            bet_stats.append(_stat_one_parquet(child, "t_bet", _PART_MONTH_RE_BET))
        elif child.name.startswith("t_session__part_"):
            sess_stats.append(_stat_one_parquet(child, "t_session", _PART_MONTH_RE_SESSION))
    bet_stats.sort(key=lambda x: x.yyyymm)
    sess_stats.sort(key=lambda x: x.yyyymm)
    return bet_stats, sess_stats


def fingerprint_partition_inventory(
    snapshot_id: str,
    *,
    snapshot_dir: Path,
    bet_stats: list[PartitionParquetStat],
    session_stats: list[PartitionParquetStat],
) -> str:
    """Deterministic fingerprint over snapshot id + sorted file stats."""
    lines: list[str] = []
    lines.append(str(snapshot_id).strip())
    lines.append(str(Path(snapshot_dir).resolve()))
    all_rows = list(bet_stats) + list(session_stats)
    all_rows.sort(key=lambda s: (s.role, s.yyyymm, str(s.path)))
    for r in all_rows:
        blob = "|".join(
            (
                r.role,
                r.yyyymm,
                str(r.path),
                str(r.mtime_ns),
                str(r.size_bytes),
                str(r.num_rows),
            )
        )
        lines.append(blob)
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest


def inventory_to_manifest_dict(
    snapshot_id: str,
    *,
    snapshot_dir: Path,
    bet_stats: list[PartitionParquetStat],
    session_stats: list[PartitionParquetStat],
) -> dict[str, Any]:
    """Serializable manifest payload for artifact write."""
    fp = fingerprint_partition_inventory(
        snapshot_id,
        snapshot_dir=snapshot_dir,
        bet_stats=bet_stats,
        session_stats=session_stats,
    )
    return {
        "manifest_kind": "trainer_hightier_partition_inventory_v1",
        "snapshot_id": str(snapshot_id).strip(),
        "snapshot_dir": str(Path(snapshot_dir).resolve()),
        "fingerprint_sha256_hex": fp,
        "tables": {
            "t_bet": [
                {
                    "yyyymm": s.yyyymm,
                    "path": str(s.path),
                    "mtime_ns": s.mtime_ns,
                    "size_bytes": s.size_bytes,
                    "num_rows": s.num_rows,
                }
                for s in bet_stats
            ],
            "t_session": [
                {
                    "yyyymm": s.yyyymm,
                    "path": str(s.path),
                    "mtime_ns": s.mtime_ns,
                    "size_bytes": s.size_bytes,
                    "num_rows": s.num_rows,
                }
                for s in session_stats
            ],
        },
    }


def write_partition_inventory_manifest(path: Path, payload: dict[str, Any]) -> Path:
    """Write JSON manifest; return resolved path."""
    p = Path(path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return p


def load_partition_inventory_manifest(path: Path) -> dict[str, Any]:
    """Load JSON manifest."""
    pp = Path(path).resolve()
    if not pp.is_file():
        raise FileNotFoundError(pp)
    return dict(json.loads(pp.read_text(encoding="utf-8")))


def partition_month_union_from_manifest(manifest: dict[str, Any]) -> set[str]:
    """Months present in manifest for either table."""
    out: set[str] = set()
    tables = manifest.get("tables")
    if not isinstance(tables, dict):
        return out
    for tbl in ("t_bet", "t_session"):
        rows = tables.get(tbl)
        if not isinstance(rows, list):
            continue
        for r in rows:
            if isinstance(r, dict) and "yyyymm" in r:
                out.add(str(r["yyyymm"]))
    return out


def months_added_or_changed(
    cur: dict[str, Any],
    prev: dict[str, Any] | None,
) -> set[str]:
    """Diff current vs previous fingerprint per (role, path); unchanged path + stat → skip."""

    def _index(m: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        ix: dict[tuple[str, str], dict[str, Any]] = {}
        tables = m.get("tables")
        if not isinstance(tables, dict):
            return ix
        for role in ("t_bet", "t_session"):
            rows = tables.get(role)
            if not isinstance(rows, list):
                continue
            for r in rows:
                if not isinstance(r, dict):
                    continue
                pth = str(r.get("path", ""))
                if not pth:
                    continue
                ix[(role, pth)] = r
        return ix

    if prev is None:
        return partition_month_union_from_manifest(cur)
    ix_cur = _index(cur)
    ix_prev = _index(prev)
    changed_months: set[str] = set()
    for k, rv in ix_cur.items():
        pv = ix_prev.get(k)
        ym = str(rv.get("yyyymm", "")).strip()
        if not ym:
            continue
        if pv is None:
            changed_months.add(ym)
            continue
        for field in ("mtime_ns", "size_bytes", "num_rows"):
            if rv.get(field) != pv.get(field):
                changed_months.add(ym)
                break
    return changed_months


def backfill_neighbor_months(months: set[str], *, backfill_count: int) -> set[str]:
    """Add up to ``backfill_count`` prior calendar months for each touched month."""

    def _dec_one(yyyymm: str) -> str:
        y = int(yyyymm[:4])
        m = int(yyyymm[4:6])
        m -= 1
        if m < 1:
            m = 12
            y -= 1
        return f"{y:04d}{m:02d}"

    out = set(months)
    bc = max(0, int(backfill_count))
    if bc == 0:
        return out
    seeds = sorted(out)
    for s in seeds:
        cur = s
        for _ in range(bc):
            cur = _dec_one(cur)
            out.add(cur)
    return out


def merge_correction_months(
    base: set[str],
    *,
    correction_months: tuple[str, ...],
) -> set[str]:
    """Union explicit correction ``YYYYMM`` strings (validated)."""
    out = set(base)
    for cm in correction_months:
        s = str(cm).strip()
        if len(s) != 6 or not s.isdigit():
            raise ValueError(f"correction_months entry must be YYYYMM digits, got {cm!r}")
        out.add(s)
    return out


def compute_recompute_months(
    *,
    current_manifest: dict[str, Any],
    previous_manifest: dict[str, Any] | None,
    correction_months: tuple[str, ...],
    backfill_month_count: int,
) -> list[str]:
    """Deterministic merged month list driving incremental stages."""
    changed = months_added_or_changed(current_manifest, previous_manifest)
    pool = merge_correction_months(
        backfill_neighbor_months(changed, backfill_count=backfill_month_count),
        correction_months=correction_months,
    )
    available = partition_month_union_from_manifest(current_manifest)
    # Only schedule months actually present on disk for this snapshot
    pool &= available
    return sorted(pool)


def default_partition_inventory_path(*, manifests_dir: Path, snapshot_id: str) -> Path:
    """Default ``artifacts/manifests/partition_inventory_{id}.json``."""
    slug = str(snapshot_id).replace("/", "_").replace("\\", "_")
    return Path(manifests_dir).resolve() / f"partition_inventory_{slug}.json"


def infer_snapshot_id(snapshot_dir: Path) -> str:
    """Infer snapshot folder name as snapshot id."""
    return Path(snapshot_dir).resolve().name


def repo_root_for_trainer_hightier() -> Path:
    """Repository root (parent of ``trainer_hightier``)."""
    return Path(__file__).resolve().parents[2]


def default_partition_snapshot_dir(*, repo_root: Path | None = None) -> Path | None:
    """Return ``<repo>/data/partitions`` when that directory exists; else ``None``.

    For **optional** checks (e.g. tests). Trainer default path uses
    :func:`expect_default_partition_snapshot_dir` which raises when missing.
    """
    root = Path(repo_root).resolve() if repo_root is not None else repo_root_for_trainer_hightier()
    candidate = root / "data" / "partitions"
    return candidate if candidate.is_dir() else None


def expect_default_partition_snapshot_dir(*, repo_root: Path | None = None) -> Path:
    """Return resolved ``<repo>/data/partitions`` or raise ``FileNotFoundError`` if absent.

    Used when the run relies on the conventional default layout (no ``--partition-snapshot-dir``,
    no ``--no-partition-snapshot``).
    """
    root = Path(repo_root).resolve() if repo_root is not None else repo_root_for_trainer_hightier()
    candidate = (root / "data" / "partitions").resolve()
    if not candidate.is_dir():
        raise FileNotFoundError(
            "Expected default partition snapshot directory is missing: "
            f"{candidate}. "
            "Create it and add t_bet__part_YYYYMM.parquet / t_session__part_YYYYMM.parquet shards, "
            "or pass --partition-snapshot-dir <dir> for a different folder, "
            "or --no-partition-snapshot to run without merging monthly shard Parquets."
        )
    return candidate


def expect_existing_partition_snapshot_dir(snapshot_dir: Path) -> Path:
    """Return resolved *snapshot_dir* or raise if it is not an existing directory.

    Used for explicit ``--partition-snapshot-dir`` so mis-typed paths fail before heavy IO.
    """
    p = Path(snapshot_dir).resolve()
    if not p.exists():
        raise FileNotFoundError(
            "--partition-snapshot-dir must be an existing directory; "
            f"missing path {p!r}."
        )
    if not p.is_dir():
        raise NotADirectoryError(
            "--partition-snapshot-dir must be a directory; "
            f"got {p!r} (exists=True, is_dir=False)."
        )
    return p


def resolve_partition_inventory_previous_for_run(
    *,
    manifests_dir: Path,
    snapshot_dir: Path,
    explicit_previous: Path | None,
) -> Path | None:
    """Pick baseline ``partition_inventory_*.json`` for ``compute_recompute_months`` diff.

    Resolution order:

    1. If *explicit_previous* is set and the path is an existing file, use it.
    2. Else if ``partition_inventory_{snapshot_id}.json`` already exists under *manifests_dir*
       (same folder basename as *snapshot_dir*), use it — typical **second run** on the same
       snapshot tree to shrink ``recompute_months`` vs treating everything as new.
    3. Else ``None`` (first run for this snapshot id, or manifest never written).

    Note: This only affects **inventory diff / recompute month logging** today; preprocess
    disk caches still follow their own manifest rules (fingerprint includes partition inventory).
    """
    if explicit_previous is not None:
        p = Path(explicit_previous).resolve()
        return p if p.is_file() else None
    snap_id = infer_snapshot_id(snapshot_dir)
    candidate = default_partition_inventory_path(manifests_dir=manifests_dir, snapshot_id=snap_id)
    return candidate if candidate.is_file() else None
