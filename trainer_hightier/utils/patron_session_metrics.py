"""Per-canonical patron aggregates from cleaned ``t_session`` (theo + gaming days + ADT)."""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_artifacts_dir
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas, execute_sql_with_progress

logger = logging.getLogger("trainer_hightier")

_REQUIRED_CLEAN_COLS: frozenset[str] = frozenset({"player_id", "theo_win", "gaming_day"})
_MAPPING_COLS: frozenset[str] = frozenset({"player_id", "canonical_id"})


def default_patron_session_metrics_parquet_path() -> Path:
    """ADT report Parquet next to canonical mapping artifacts."""
    return default_canonical_mapping_artifacts_dir() / "canonical_patron_session_metrics.parquet"


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _validate_inputs(cleaned: Path, mapping: Path) -> None:
    if not cleaned.is_file():
        raise FileNotFoundError(cleaned)
    if not mapping.is_file():
        raise FileNotFoundError(mapping)
    cnames = frozenset(pq.read_schema(cleaned).names)
    mnames = frozenset(pq.read_schema(mapping).names)
    miss_c = sorted(_REQUIRED_CLEAN_COLS - cnames)
    miss_m = sorted(_MAPPING_COLS - mnames)
    if miss_c:
        raise ValueError(f"Cleaned session Parquet missing columns for patron metrics: {miss_c}")
    if miss_m:
        raise ValueError(f"Canonical mapping Parquet missing columns: {miss_m}")


def _adt_copy_sql(*, cleaned_posix: str, map_posix: str) -> str:
    """Aggregate joined sessions → one row per ``canonical_id``, sorted by ADT descending."""
    return f"""
WITH map AS (SELECT * FROM read_parquet('{map_posix}')),
sess AS (SELECT * FROM read_parquet('{cleaned_posix}')),
joined AS (
  SELECT
    CAST(map.canonical_id AS VARCHAR) AS canonical_id,
    COALESCE(TRY_CAST(sess.theo_win AS DOUBLE), 0.0) AS theo_win,
    sess.gaming_day AS gaming_day
  FROM sess
  INNER JOIN map
    ON TRY_CAST(sess.player_id AS BIGINT) = TRY_CAST(map.player_id AS BIGINT)
),
agg AS (
  SELECT
    canonical_id,
    CAST(SUM(theo_win) AS DOUBLE) AS total_theo_win,
    CAST(COUNT(DISTINCT gaming_day) AS BIGINT) AS gaming_days,
    CASE
      WHEN COUNT(DISTINCT gaming_day) > 0 THEN
        CAST(SUM(theo_win) AS DOUBLE) / CAST(COUNT(DISTINCT gaming_day) AS DOUBLE)
      ELSE NULL
    END AS adt
  FROM joined
  GROUP BY canonical_id
)
SELECT canonical_id, total_theo_win, gaming_days, adt
FROM agg
ORDER BY adt DESC NULLS LAST, canonical_id ASC
"""


def compile_canonical_patron_session_metrics(
    cleaned_session_parquet: Path,
    canonical_mapping_parquet: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    output_parquet: Path | None = None,
    duckdb_join_timeout_s: float = 3600.0,
) -> Path:
    """Write Parquet with ``canonical_id``, ``total_theo_win``, ``gaming_days``, ``adt`` (ADT descending).

    Sessions whose ``player_id`` is absent from the mapping are dropped (inner join).
    Patrons with zero distinct ``gaming_day`` values get ``adt`` NULL and sort last.
    """
    src_c = Path(cleaned_session_parquet).resolve()
    src_m = Path(canonical_mapping_parquet).resolve()
    _validate_inputs(src_c, src_m)

    out = Path(output_parquet) if output_parquet is not None else default_patron_session_metrics_parquet_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file():
        out.unlink()

    c_px = _path_posix(src_c).replace("'", "''")
    m_px = _path_posix(src_m).replace("'", "''")
    out_px = _path_posix(out).replace("'", "''")

    inner = _adt_copy_sql(cleaned_posix=c_px, map_posix=m_px)
    sql = f"COPY ({inner}) TO '{out_px}' (FORMAT PARQUET, COMPRESSION SNAPPY)"

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        execute_sql_with_progress(
            con,
            sql,
            desc="[Step 4] DuckDB patron ADT report",
            join_timeout_s=float(duckdb_join_timeout_s),
        )
    finally:
        con.close()

    meta = pq.ParquetFile(out).metadata
    nrows = int(meta.num_rows) if meta is not None else -1
    logger.info(
        "[Step 4] patron session metrics (canonical ADT): rows=%d written %s",
        nrows,
        out.resolve(),
    )
    return out.resolve()
