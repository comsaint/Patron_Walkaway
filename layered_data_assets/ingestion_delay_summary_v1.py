"""L1 manifest ``ingestion_delay_summary`` preview (SSOT §4.4; LDA-E1-06)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

DEFAULT_LATE_THRESHOLD_SEC = 86_400.0


def manifest_ingestion_delay_placeholder() -> dict[str, Any]:
    """Null-filled summary when no preview can be computed (schema-stable)."""
    return {
        "ingest_delay_p50_sec": None,
        "ingest_delay_p95_sec": None,
        "ingest_delay_p99_sec": None,
        "ingest_delay_max_sec": None,
        "late_row_count": None,
        "late_row_ratio": None,
        "affected_run_count": None,
        "affected_trip_count": None,
    }


def _parquet_column_names(con: Any, parquet_path: Path) -> set[str]:
    """Return column names for a Parquet file via DuckDB ``DESCRIBE``."""
    rows = con.execute(
        "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))",
        [str(parquet_path.resolve())],
    ).fetchall()
    return {str(r[0]) for r in rows}


def _assert_simple_sql_identifiers(event_time_col: str, observed_at_col: str) -> None:
    """Raise if column names are not safe to embed as bare SQL identifiers."""
    ident = r"[A-Za-z_][A-Za-z0-9_]*"
    if not re.fullmatch(ident, event_time_col) or not re.fullmatch(ident, observed_at_col):
        raise ValueError(
            "event_time_col and observed_at_col must be simple identifiers, got "
            f"{event_time_col!r}, {observed_at_col!r}"
        )


def _ingestion_delay_aggregate_sql(
    parquet_path_sql_escaped: str,
    event_time_col: str,
    observed_at_col: str,
    late_threshold_sec: float,
) -> str:
    """Build DuckDB SQL for delay quantiles over ``observed_at - event_time`` (seconds)."""
    ev = event_time_col
    ob = observed_at_col
    p = parquet_path_sql_escaped
    thr = float(late_threshold_sec)
    return f"""
WITH d AS (
  SELECT
    EXTRACT(EPOCH FROM TRY_CAST({ob} AS TIMESTAMP))
    - EXTRACT(EPOCH FROM TRY_CAST({ev} AS TIMESTAMP)) AS delay_sec
  FROM read_parquet('{p}')
),
tot AS (SELECT COUNT(*)::BIGINT AS n_all FROM read_parquet('{p}')),
agg AS (
  SELECT
    quantile_cont(delay_sec, 0.5) AS p50,
    quantile_cont(delay_sec, 0.95) AS p95,
    quantile_cont(delay_sec, 0.99) AS p99,
    MAX(delay_sec) AS dmax,
    COUNT(*)::BIGINT AS n_valid,
    COUNT(*) FILTER (WHERE delay_sec > {thr})::BIGINT AS late_n
  FROM d
  WHERE delay_sec IS NOT NULL
)
SELECT a.p50, a.p95, a.p99, a.dmax, a.n_valid, a.late_n, t.n_all
FROM agg a CROSS JOIN tot t
"""


def _ingestion_delay_summary_from_aggregate_row(row: tuple[Any, ...]) -> dict[str, Any] | None:
    """Map aggregate SQL row to manifest fields, or ``None`` if no valid delay rows."""
    p50, p95, p99, dmax, n_valid, late_n, n_all = row
    n_all_i = int(n_all) if n_all is not None else 0
    if not n_valid or int(n_valid) == 0:
        return None
    late_n_i = int(late_n) if late_n is not None else 0
    ratio = float(late_n_i) / float(n_all_i) if n_all_i > 0 else None
    return {
        "ingest_delay_p50_sec": float(p50) if p50 is not None else None,
        "ingest_delay_p95_sec": float(p95) if p95 is not None else None,
        "ingest_delay_p99_sec": float(p99) if p99 is not None else None,
        "ingest_delay_max_sec": float(dmax) if dmax is not None else None,
        "late_row_count": late_n_i,
        "late_row_ratio": ratio,
        "affected_run_count": None,
        "affected_trip_count": None,
    }


def compute_ingestion_delay_summary_preview(
    con: Any,
    parquet_path: Path,
    *,
    event_time_col: str = "payout_complete_dtm",
    observed_at_col: str = "__etl_insert_Dtm",
    late_threshold_sec: float = DEFAULT_LATE_THRESHOLD_SEC,
) -> dict[str, Any]:
    """Compute ``ingest_delay_*`` / ``late_*`` from ``observed_at - event_time`` (seconds).

    Uses ``t_bet`` defaults per ``time_semantics_registry`` / SSOT §4.4. If required columns
    are missing or no valid pairs exist, returns :func:`manifest_ingestion_delay_placeholder`.
    """
    if late_threshold_sec < 0:
        raise ValueError(f"late_threshold_sec must be >= 0, got {late_threshold_sec!r}")
    cols = _parquet_column_names(con, parquet_path)
    if event_time_col not in cols or observed_at_col not in cols:
        return manifest_ingestion_delay_placeholder()
    _assert_simple_sql_identifiers(event_time_col, observed_at_col)
    p_esc = str(parquet_path.resolve()).replace("'", "''")
    sql = _ingestion_delay_aggregate_sql(p_esc, event_time_col, observed_at_col, late_threshold_sec)
    row = con.execute(sql).fetchone()
    if row is None:
        return manifest_ingestion_delay_placeholder()
    out = _ingestion_delay_summary_from_aggregate_row(row)
    return manifest_ingestion_delay_placeholder() if out is None else out
