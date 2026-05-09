"""Shard monolithic split Parquets by calendar day for L2 manifest v2 (GitHub #16).

Uses DuckDB ``read_parquet`` + per-day ``COPY`` so we avoid loading full splits into
pandas.  Day key prefers ``gaming_day`` when present; otherwise
``date_trunc('day', payout_complete_dtm)``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Mapping, MutableSequence, Sequence, Tuple

logger = logging.getLogger(__name__)


def _duckdb_escape_path(p: Path) -> str:
    return str(p).replace("'", "''")


def _day_column_sql(available: frozenset[str]) -> str:
    """Return SQL expression for calendar day (no alias)."""
    if "gaming_day" in available:
        return "try_cast(gaming_day AS DATE)"
    if "payout_complete_dtm" in available:
        return (
            "CAST(date_trunc('day', try_cast(payout_complete_dtm AS TIMESTAMP)) AS DATE)"
        )
    raise ValueError(
        "l2_day_shard: parquet must contain gaming_day or payout_complete_dtm for day sharding; "
        f"columns={sorted(available)[:40]}"
    )


def shard_split_parquet_by_day(
    bundle_dir: Path,
    split_name: str,
    src_parquet: Path,
) -> List[Mapping[str, Any]]:
    """Write one Parquet per distinct day under ``day_shards/<split_name>/``.

    Args:
        bundle_dir: L2 bundle root (manifest lives here).
        split_name: ``train`` | ``valid`` | ``test``.
        src_parquet: Monolithic split file produced by Step 7.

    Returns:
        Sorted list of ``{"day": "YYYY-MM-DD", "path": "<relative bundle path>"}``.
    """
    import duckdb

    if not src_parquet.is_file():
        raise FileNotFoundError(f"l2_day_shard: missing source parquet: {src_parquet}")
    root = bundle_dir / "day_shards" / split_name
    if root.exists():
        for child in list(root.iterdir()):
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                for sub in sorted(child.rglob("*"), reverse=True):
                    if sub.is_file():
                        sub.unlink(missing_ok=True)
                try:
                    child.rmdir()
                except OSError:
                    pass
    root.mkdir(parents=True, exist_ok=True)

    sp = _duckdb_escape_path(src_parquet.resolve())
    con = duckdb.connect(":memory:")
    try:
        cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{sp}')").fetchall()
        available = frozenset(str(r[0]) for r in cols)
        col_sql = _day_column_sql(available)
        days = con.execute(
            f"SELECT DISTINCT {col_sql} AS d FROM read_parquet('{sp}') "
            f"WHERE {col_sql} IS NOT NULL ORDER BY 1"
        ).fetchall()
        if not days:
            raise ValueError(f"l2_day_shard: no distinct days in {src_parquet}")
        rel_manifest: MutableSequence[Mapping[str, Any]] = []
        for (dval,) in days:
            if dval is None:
                continue
            day_s = dval.isoformat() if hasattr(dval, "isoformat") else str(dval)[:10]
            out_dir = root / f"day={day_s}"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file = out_dir / "part.parquet"
            con.execute(
                f"COPY (SELECT * FROM read_parquet('{sp}') WHERE {col_sql} = DATE '{day_s}') "
                f"TO '{_duckdb_escape_path(out_file)}' (FORMAT PARQUET)"
            )
            rel = f"day_shards/{split_name}/day={day_s}/part.parquet"
            rel_manifest.append({"day": day_s, "path": rel})
            logger.info("l2_day_shard: %s day=%s -> %s", split_name, day_s, rel)
    finally:
        con.close()
    return sorted(rel_manifest, key=lambda x: str(x["day"]))


def min_max_day_from_manifest_rows(rows: Sequence[Mapping[str, Any]]) -> Tuple[str, str]:
    """Return (min_day, max_day) inclusive strings from shard manifest rows."""
    if not rows:
        raise ValueError("min_max_day_from_manifest_rows: empty rows")
    ds = sorted(str(r["day"]) for r in rows)
    return ds[0], ds[-1]
