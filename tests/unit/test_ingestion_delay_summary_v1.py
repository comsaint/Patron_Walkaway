"""Unit tests for ``ingestion_delay_summary_v1``."""

from pathlib import Path

import pytest

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]

from layered_data_assets.ingestion_delay_summary_v1 import (
    compute_ingestion_delay_summary_preview,
    manifest_ingestion_delay_placeholder,
)


def test_placeholder_has_all_keys() -> None:
    p = manifest_ingestion_delay_placeholder()
    assert set(p.keys()) == {
        "ingest_delay_p50_sec",
        "ingest_delay_p95_sec",
        "ingest_delay_p99_sec",
        "ingest_delay_max_sec",
        "late_row_count",
        "late_row_ratio",
        "affected_run_count",
        "affected_trip_count",
    }


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_compute_preview_with_two_columns(tmp_path: Path) -> None:
    p = tmp_path / "b.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (SELECT
              TIMESTAMP '2026-01-15 10:00:00' AS payout_complete_dtm,
              TIMESTAMP '2026-01-15 10:30:00' AS __etl_insert_Dtm
            ) TO '{p.as_posix()}' (FORMAT PARQUET)
            """
        )
        s = compute_ingestion_delay_summary_preview(con, p, late_threshold_sec=3600)
    finally:
        con.close()
    assert s["ingest_delay_p50_sec"] == 1800.0
    assert s["late_row_count"] == 0
    assert s["late_row_ratio"] == 0.0


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_compute_preview_missing_columns_returns_placeholder(tmp_path: Path) -> None:
    p = tmp_path / "b.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"COPY (SELECT 1::BIGINT AS bet_id) TO '{p.as_posix()}' (FORMAT PARQUET)"
        )
        s = compute_ingestion_delay_summary_preview(con, p)
    finally:
        con.close()
    assert s == manifest_ingestion_delay_placeholder()
