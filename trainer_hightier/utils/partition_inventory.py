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
_TABLE_PARTITION_MONTH_RE = re.compile(r"partition_(\d{6})$")

LAYOUT_LEGACY_MONTHLY_PARQUET: str = "legacy_monthly_parquet"
LAYOUT_TABLE_PARTITION_SHARDS: str = "table_partition_shards"


@dataclass(frozen=True)
class PartitionParquetStat:
    """One monthly partition parquet file fingerprint (v1: path/size/mtime/rows)."""

    path: Path
    yyyymm: str
    role: str
    mtime_ns: int
    size_bytes: int
    num_rows: int


def _stat_one_parquet_with_yyyymm(path: Path, role: str, yyyymm: str) -> PartitionParquetStat:
    """Build stats for one parquet shard with an explicit ``YYYYMM`` month key."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    name = p.name
    if name.endswith(".gstmp"):
        raise ValueError(f"Refusing incomplete parquet filename: {p}")
    st = p.stat()
    meta = pq.ParquetFile(p).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    return PartitionParquetStat(
        path=p,
        yyyymm=str(yyyymm),
        role=role,
        mtime_ns=int(st.st_mtime_ns),
        size_bytes=int(st.st_size),
        num_rows=nrows,
    )


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
    return _stat_one_parquet_with_yyyymm(p, role, ym)


def _table_partition_roots(data_root: Path) -> tuple[Path, Path]:
    """Return ``(t_bet, t_session)`` table roots under a repo ``data`` directory."""
    root = Path(data_root).resolve()
    return root / "t_bet", root / "t_session"


def _has_table_partition_shards(table_root: Path) -> bool:
    """True when *table_root* contains ``partition_YYYYMM/`` monthly folders."""
    tr = Path(table_root).resolve()
    if not tr.is_dir():
        return False
    return any(p.is_dir() and _TABLE_PARTITION_MONTH_RE.match(p.name) for p in tr.iterdir())


def has_table_dir_partition_layout(data_root: Path) -> bool:
    """True when ``data/t_bet`` and ``data/t_session`` both expose monthly partition folders."""
    root = Path(data_root).resolve()
    bet_root, sess_root = _table_partition_roots(root)
    return _has_table_partition_shards(bet_root) and _has_table_partition_shards(sess_root)


def table_dir_preprocess_read_root(snapshot_dir: Path, table_name: str) -> Path:
    """Return the DuckDB-readable table root for table-dir layout preprocess."""
    root = Path(snapshot_dir).resolve()
    if root.name == table_name:
        return root
    return root / table_name


def collapsed_preprocess_read_sources(
    *,
    snapshot_dir: Path,
    table_name: str,
    legacy_paths: tuple[Path, ...] = (),
) -> tuple[Path, ...]:
    """Collapse table-dir monthly shards to one directory path for DuckDB reads.

    Per-shard inventory stats remain in partition inventory manifests; this helper
    is only for preprocess IO so ``read_parquet('<table_root>')`` scans all shards.
    """
    root = Path(snapshot_dir).resolve()
    if detect_partition_layout(root) == LAYOUT_TABLE_PARTITION_SHARDS:
        table_root = table_dir_preprocess_read_root(root, table_name)
        if not table_root.is_dir():
            raise FileNotFoundError(table_root)
        return (table_root,)
    if not legacy_paths:
        return ()
    return tuple(sorted({Path(p).resolve() for p in legacy_paths}, key=str))


def _has_legacy_partition_files(snapshot_dir: Path) -> bool:
    """True when *snapshot_dir* contains legacy ``t_*__part_YYYYMM.parquet`` shards."""
    root = Path(snapshot_dir).resolve()
    if not root.is_dir():
        return False
    for child in root.rglob("*.parquet"):
        if child.is_file() and (
            child.name.startswith("t_bet__part_") or child.name.startswith("t_session__part_")
        ):
            return True
    return False


def detect_partition_layout(snapshot_dir: Path) -> str:
    """Detect inventory layout for *snapshot_dir*.

    When both table-dir (``data/t_bet``, ``data/t_session``) and legacy monthly
    parquet shards coexist under the same repo ``data`` root, table-dir wins so
    the newer source layout is used without silently mixing both.
    """
    root = Path(snapshot_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)

    if has_table_dir_partition_layout(root):
        return LAYOUT_TABLE_PARTITION_SHARDS
    if root.name in {"t_bet", "t_session"} and _has_table_partition_shards(root):
        return LAYOUT_TABLE_PARTITION_SHARDS
    if _has_legacy_partition_files(root):
        return LAYOUT_LEGACY_MONTHLY_PARQUET
    raise FileNotFoundError(
        f"No partition shards found under {root}. "
        "Expected legacy t_bet__part_YYYYMM.parquet / t_session__part_YYYYMM.parquet, "
        "or table-dir data/t_bet/partition_YYYYMM/part_*.parquet layout."
    )


def _scan_table_partition_shards(table_root: Path, role: str) -> list[PartitionParquetStat]:
    """Scan ``partition_YYYYMM/part_*.parquet`` shards under one table root."""
    root = Path(table_root).resolve()
    if not root.is_dir():
        return []
    stats: list[PartitionParquetStat] = []
    for part_dir in sorted(root.iterdir()):
        if not part_dir.is_dir():
            continue
        m = _TABLE_PARTITION_MONTH_RE.match(part_dir.name)
        if not m:
            continue
        ym = str(m.group(1))
        for shard in sorted(part_dir.glob("*.parquet")):
            if not shard.is_file() or shard.name.endswith(".gstmp"):
                continue
            stats.append(_stat_one_parquet_with_yyyymm(shard, role, ym))
    return stats


def _scan_legacy_partition_shards(snapshot_dir: Path) -> tuple[list[PartitionParquetStat], list[PartitionParquetStat]]:
    """Scan legacy ``t_*__part_YYYYMM.parquet`` shards recursively under *snapshot_dir*."""
    root = Path(snapshot_dir).resolve()
    bet_stats: list[PartitionParquetStat] = []
    sess_stats: list[PartitionParquetStat] = []
    for child in sorted(root.rglob("*.parquet")):
        if not child.is_file():
            continue
        if child.name.startswith("t_bet__part_"):
            bet_stats.append(_stat_one_parquet(child, "t_bet", _PART_MONTH_RE_BET))
        elif child.name.startswith("t_session__part_"):
            sess_stats.append(_stat_one_parquet(child, "t_session", _PART_MONTH_RE_SESSION))
    bet_stats.sort(key=lambda x: (x.yyyymm, str(x.path)))
    sess_stats.sort(key=lambda x: (x.yyyymm, str(x.path)))
    return bet_stats, sess_stats


def scan_partition_snapshot_dir(snapshot_dir: Path) -> tuple[list[PartitionParquetStat], list[PartitionParquetStat]]:
    """List bet/session partition Parquets under a snapshot folder.

    Supports:

    - legacy monthly parquet: ``partitions/t_bet__part_YYYYMM.parquet``
    - table-dir monthly shards: ``data/t_bet/partition_YYYYMM/part_*.parquet``
    - nested dated export subfolders for the legacy layout
    """
    root = Path(snapshot_dir).resolve()
    layout = detect_partition_layout(root)
    if layout == LAYOUT_TABLE_PARTITION_SHARDS:
        if root.name in {"t_bet", "t_session"}:
            role = "t_bet" if root.name == "t_bet" else "t_session"
            stats = _scan_table_partition_shards(root, role)
            if role == "t_bet":
                return stats, []
            return [], stats
        bet_root, sess_root = _table_partition_roots(root)
        bet_stats = _scan_table_partition_shards(bet_root, "t_bet")
        sess_stats = _scan_table_partition_shards(sess_root, "t_session")
        if not bet_stats and not sess_stats:
            raise FileNotFoundError(
                f"table-dir layout detected under {root} but no partition shards were found "
                f"under {bet_root} / {sess_root}"
            )
        return bet_stats, sess_stats
    return _scan_legacy_partition_shards(root)


def fingerprint_partition_inventory(
    snapshot_id: str,
    *,
    snapshot_dir: Path,
    bet_stats: list[PartitionParquetStat],
    session_stats: list[PartitionParquetStat],
    layout_kind: str | None = None,
) -> str:
    """Deterministic fingerprint over snapshot id + sorted file stats."""
    lines: list[str] = []
    lines.append(str(snapshot_id).strip())
    lines.append(str(Path(snapshot_dir).resolve()))
    lk = layout_kind or detect_partition_layout(Path(snapshot_dir))
    lines.append(str(lk))
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
    layout_kind: str | None = None,
) -> dict[str, Any]:
    """Serializable manifest payload for artifact write."""
    lk = layout_kind or detect_partition_layout(Path(snapshot_dir))
    fp = fingerprint_partition_inventory(
        snapshot_id,
        snapshot_dir=snapshot_dir,
        bet_stats=bet_stats,
        session_stats=session_stats,
        layout_kind=lk,
    )
    return {
        "manifest_kind": "trainer_hightier_partition_inventory_v1",
        "snapshot_id": str(snapshot_id).strip(),
        "snapshot_dir": str(Path(snapshot_dir).resolve()),
        "layout_kind": lk,
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
    prev_layout = str(prev.get("layout_kind", LAYOUT_LEGACY_MONTHLY_PARQUET)).strip()
    cur_layout = str(cur.get("layout_kind", LAYOUT_LEGACY_MONTHLY_PARQUET)).strip()
    if prev_layout != cur_layout:
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
    """Return default partition snapshot root when a supported layout exists.

    Prefers table-dir ``<repo>/data`` when ``data/t_bet`` and ``data/t_session`` both
    contain ``partition_YYYYMM/`` folders; otherwise falls back to legacy
    ``<repo>/data/partitions``.

    For **optional** checks (e.g. tests). Trainer default path uses
    :func:`expect_default_partition_snapshot_dir` which raises when missing.
    """
    root = Path(repo_root).resolve() if repo_root is not None else repo_root_for_trainer_hightier()
    data_root = root / "data"
    if has_table_dir_partition_layout(data_root):
        return data_root.resolve()
    legacy = root / "data" / "partitions"
    return legacy.resolve() if legacy.is_dir() else None


def expect_default_partition_snapshot_dir(*, repo_root: Path | None = None) -> Path:
    """Return resolved default partition snapshot root or raise when absent.

    Used when the run relies on the conventional default layout (no ``--partition-snapshot-dir``,
    no ``--no-partition-snapshot``).
    """
    root = Path(repo_root).resolve() if repo_root is not None else repo_root_for_trainer_hightier()
    got = default_partition_snapshot_dir(repo_root=root)
    if got is None:
        data_root = (root / "data").resolve()
        legacy = (root / "data" / "partitions").resolve()
        raise FileNotFoundError(
            "Expected default partition snapshot layout is missing. "
            f"Need either table-dir shards under {data_root / 't_bet'} and {data_root / 't_session'}, "
            f"or legacy monthly parquet shards under {legacy}. "
            "Alternatively pass --partition-snapshot-dir <dir>, "
            "or --no-partition-snapshot to run without merging monthly shard Parquets."
        )
    return got


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
