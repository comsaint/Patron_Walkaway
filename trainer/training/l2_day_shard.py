"""Shard monolithic split Parquets by calendar day for L2 manifest v2 (GitHub #16).

Uses DuckDB ``read_parquet`` + per-day ``COPY`` so we avoid loading full splits into
pandas.  Day key prefers ``gaming_day`` when present; otherwise
``date_trunc('day', payout_complete_dtm)``.

Canonical writer contract: L2 bundle Parquet outputs use explicit lower-case column
aliases so pandas / pyarrow / DuckDB readers all observe the same names.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableSequence, Sequence, Tuple

logger = logging.getLogger(__name__)


def _duckdb_escape_path(p: Path) -> str:
    return str(p).replace("'", "''")


def _duckdb_quote_ident(name: str) -> str:
    """Return a DuckDB-safe quoted identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def l2_bundle_column_rename_map(columns: Sequence[str]) -> Dict[str, str]:
    """Return source->canonical lower-case column rename map for bundle outputs."""
    rename_map: Dict[str, str] = {}
    seen: Dict[str, str] = {}
    for raw_name in columns:
        src = str(raw_name).strip()
        if not src:
            raise ValueError("l2 bundle column names must be non-empty strings")
        canonical = src.lower()
        previous = seen.get(canonical)
        if previous is not None and previous != src:
            raise ValueError(
                "l2 bundle canonical column collision after lower-case normalization: "
                f"{previous!r} vs {src!r} -> {canonical!r}"
            )
        seen[canonical] = src
        rename_map[src] = canonical
    return rename_map


def _normalized_column_map(columns: Sequence[str]) -> Dict[str, str]:
    """Return canonical lower-case name -> original source name mapping."""
    rename_map = l2_bundle_column_rename_map(columns)
    return {canonical: source for source, canonical in rename_map.items()}


def _canonical_select_list(columns: Sequence[str], source_alias: str) -> str:
    """Build explicit DuckDB select list with lower-case aliases."""
    rename_map = l2_bundle_column_rename_map(columns)
    return ", ".join(
        f"{source_alias}.{_duckdb_quote_ident(src)} AS {_duckdb_quote_ident(dst)}"
        for src, dst in rename_map.items()
    )


def _duckdb_read_parquet_expr(paths: Sequence[Path]) -> str:
    """Build DuckDB ``read_parquet`` expression for one or many Parquet files."""
    cleaned = [_duckdb_escape_path(Path(p).resolve()) for p in paths]
    if not cleaned:
        raise ValueError("DuckDB read_parquet expression requires at least one path")
    if len(cleaned) == 1:
        return f"read_parquet('{cleaned[0]}')"
    inner = ", ".join(f"'{path_sql}'" for path_sql in cleaned)
    return f"read_parquet([{inner}])"


def canonical_l2_bundle_read_parquet_expr(paths: Sequence[Path]) -> str:
    """Return a canonical lower-case DuckDB subquery over one or many Parquet files."""
    import duckdb

    read_expr = _duckdb_read_parquet_expr(paths)
    con = duckdb.connect(":memory:")
    try:
        desc = con.execute(f"SELECT * FROM {read_expr} LIMIT 0").description
        source_columns = [str(col[0]) for col in (desc or [])]
    finally:
        con.close()
    select_list = _canonical_select_list(source_columns, "src")
    return f"(SELECT {select_list} FROM {read_expr} AS src)"


def copy_parquet_with_canonical_l2_columns(src_parquet: Path, dest_parquet: Path) -> None:
    """Rewrite *src_parquet* to *dest_parquet* with lower-case bundle column names."""
    import duckdb

    src_sql = _duckdb_escape_path(Path(src_parquet).resolve())
    dest_sql = _duckdb_escape_path(Path(dest_parquet).resolve())
    con = duckdb.connect(":memory:")
    try:
        cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src_sql}')").fetchall()
        source_columns = [str(row[0]) for row in cols]
        select_list = _canonical_select_list(source_columns, "src")
        con.execute(
            f"""
            COPY (
              SELECT {select_list}
              FROM read_parquet('{src_sql}') AS src
            ) TO '{dest_sql}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()


def _day_column_sql(columns_by_normalized_name: Mapping[str, str], source_alias: str) -> str:
    """Return SQL expression for calendar day using canonicalized source lookup."""
    if "gaming_day" in columns_by_normalized_name:
        actual = columns_by_normalized_name["gaming_day"]
        return f"try_cast({source_alias}.{_duckdb_quote_ident(actual)} AS DATE)"
    if "payout_complete_dtm" in columns_by_normalized_name:
        actual = columns_by_normalized_name["payout_complete_dtm"]
        return (
            "CAST(date_trunc('day', "
            f"try_cast({source_alias}.{_duckdb_quote_ident(actual)} AS TIMESTAMP)"
            ") AS DATE)"
        )
    raise ValueError(
        "l2_day_shard: parquet must contain gaming_day or payout_complete_dtm for day sharding; "
        f"columns={sorted(columns_by_normalized_name)[:40]}"
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
        source_columns = [str(row[0]) for row in cols]
        columns_by_normalized_name = _normalized_column_map(source_columns)
        col_sql = _day_column_sql(columns_by_normalized_name, "src")
        select_list = _canonical_select_list(source_columns, "src")
        days = con.execute(
            f"SELECT DISTINCT {col_sql} AS d FROM read_parquet('{sp}') AS src "
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
                f"COPY (SELECT {select_list} FROM read_parquet('{sp}') AS src "
                f"WHERE {col_sql} = DATE '{day_s}') "
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
