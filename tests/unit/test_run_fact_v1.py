"""Unit tests for ``run_fact_v1`` (DuckDB)."""

import json
from pathlib import Path

import pytest

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]

from layered_data_assets.run_fact_v1 import (
    build_run_fact_manifest,
    materialize_run_fact_v1,
)
from layered_data_assets.run_id_v1 import derive_run_id


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_materialize_run_fact_gap_and_run_id_parity(tmp_path: Path) -> None:
    """Two runs when gap >= 30 min; SQL ``run_id`` matches Python :func:`derive_run_id`."""
    inp = tmp_path / "in.parquet"
    out = tmp_path / "run_fact.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (SELECT * FROM (VALUES
              (1::BIGINT, 100::BIGINT, DATE '2026-01-15', TIMESTAMP '2026-01-15 10:00:00',
               TIMESTAMP '2026-01-15 11:00:00'),
              (2::BIGINT, 100::BIGINT, DATE '2026-01-15', TIMESTAMP '2026-01-15 10:15:00',
               TIMESTAMP '2026-01-15 11:05:00'),
              (3::BIGINT, 100::BIGINT, DATE '2026-01-15', TIMESTAMP '2026-01-15 11:00:00',
               TIMESTAMP '2026-01-15 12:00:00')
            ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        stats = materialize_run_fact_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out,
            run_end_gaming_day="2026-01-15",
            run_break_min=30,
        )
    finally:
        con.close()
    assert stats["row_count"] == 2
    con2 = duckdb.connect(database=":memory:")
    try:
        rows = con2.execute(
            "SELECT run_id, player_id, first_bet_id, run_start_ts FROM read_parquet(?)",
            [str(out)],
        ).fetchall()
        for run_id, player_id, first_bet_id, run_start_ts in rows:
            py = derive_run_id(
                player_id=player_id,
                run_start_ts=run_start_ts,
                first_bet_id=first_bet_id,
                run_definition_version="run_boundary_v1",
                source_namespace="layered_data_assets_l1",
            )
            assert run_id == py
    finally:
        con2.close()


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_build_run_fact_manifest_validates_schema(tmp_path: Path) -> None:
    from jsonschema import Draft7Validator

    repo = Path(__file__).resolve().parents[2]
    schema_path = repo / "schema" / "manifest_layered_data_assets.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    out = tmp_path / "run_fact.parquet"
    out.write_bytes(b"duck")  # invalid parquet; manifest only needs path relative to anchor
    stats = {"row_count": 0, "time_range_min": "2026-01-01T00:00:00Z", "time_range_max": "2026-01-01T01:00:00Z"}
    m = build_run_fact_manifest(
        source_snapshot_id="snap_abcdefgh",
        run_end_gaming_day="2026-01-01",
        l0_fingerprint_path=None,
        l1_preprocess_gaming_day="2026-01-01",
        output_parquet=out,
        manifest_uri_anchor=tmp_path,
        stats=stats,
    )
    Draft7Validator(schema).validate(m)
    assert m["artifact_kind"] == "run_fact"
    assert m["partition_keys"]["run_end_gaming_day"] == "2026-01-01"
    assert m["output_relative_uri"] == "run_fact.parquet"
