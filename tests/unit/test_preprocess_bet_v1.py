"""Unit tests for preprocess_bet_v1 (DuckDB)."""

import json
from pathlib import Path

import pytest

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]

from layered_data_assets.preprocess_bet_v1 import (
    build_preprocess_manifest,
    manifest_output_relative_uri,
    run_preprocess_bet_v1,
)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_preprocess_bet_v1_filters_and_dedup(tmp_path: Path) -> None:
    inp = tmp_path / "in.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '2026-01-15',
                 TIMESTAMP '2026-01-15 10:00:00', TIMESTAMP '2026-01-15 11:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER),
                (1::BIGINT, 100::BIGINT, DATE '2026-01-15',
                 TIMESTAMP '2026-01-15 10:30:00', TIMESTAMP '2026-01-15 12:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER),
                (2::BIGINT, -1::BIGINT, DATE '2026-01-15',
                 TIMESTAMP '2026-01-15 09:00:00', TIMESTAMP '2026-01-15 09:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER),
                (3::BIGINT, 200::BIGINT, DATE '2026-01-16',
                 TIMESTAMP '2026-01-16 09:00:00', TIMESTAMP '2026-01-16 09:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        out = tmp_path / "cleaned.parquet"
        stats = run_preprocess_bet_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out,
            gaming_day="2026-01-15",
            dummy_player_ids_parquet=None,
            eligible_player_ids_parquet=None,
        )
    finally:
        con.close()
    assert stats["row_count"] == 1
    gaps_join = " ".join(stats["preprocessing_gaps"])
    assert "BET-DQ-02" in gaps_join and "BET-DQ-03" in gaps_join
    con2 = duckdb.connect(database=":memory:")
    try:
        pid = con2.execute("SELECT player_id FROM read_parquet(?)", [str(inp)]).fetchall()
        assert len(pid) == 4
        one = con2.execute("SELECT bet_id, player_id FROM read_parquet(?)", [str(out)]).fetchone()
        assert one == (1, 100)
    finally:
        con2.close()


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_preprocess_bet_v1_empty_output_no_crash(tmp_path: Path) -> None:
    """All rows filtered → 0 rows; time-range query must not raise on fetchone."""
    inp = tmp_path / "in.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '2026-01-16',
                 TIMESTAMP '2026-01-16 10:00:00',
                 TIMESTAMP '2026-01-16 11:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        out = tmp_path / "empty_cleaned.parquet"
        stats = run_preprocess_bet_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out,
            gaming_day="2026-01-15",
            dummy_player_ids_parquet=None,
            eligible_player_ids_parquet=None,
        )
    finally:
        con.close()
    assert stats["row_count"] == 0


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_preprocess_bet_v1_eligible_filter(tmp_path: Path) -> None:
    inp = tmp_path / "in.parquet"
    elig = tmp_path / "elig.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (SELECT 100::BIGINT AS player_id, DATE '2026-01-15' AS gaming_day,
                  TIMESTAMP '2026-01-15 10:00:00' AS payout_complete_dtm,
                  TIMESTAMP '2026-01-15 11:00:00' AS __etl_insert_Dtm,
                  1::BIGINT AS bet_id, 0::INTEGER AS is_deleted, 0::INTEGER AS is_canceled, 0::INTEGER AS is_manual
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        con.execute(
            f"COPY (SELECT 100::BIGINT AS player_id) TO '{elig.as_posix()}' (FORMAT PARQUET)"
        )
        out = tmp_path / "out.parquet"
        stats = run_preprocess_bet_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out,
            gaming_day="2026-01-15",
            dummy_player_ids_parquet=None,
            eligible_player_ids_parquet=elig,
        )
    finally:
        con.close()
    assert stats["row_count"] == 1
    gaps_join = " ".join(stats["preprocessing_gaps"])
    assert "BET-DQ-02" in gaps_join
    assert "BET-DQ-03" not in gaps_join


def test_manifest_output_relative_uri_under_anchor(tmp_path: Path) -> None:
    """Paths in the manifest must be relative to the chosen anchor (e.g. repo root)."""
    data = tmp_path / "data"
    out = data / "l1_layered" / "snap_x" / "t_bet" / "gaming_day=2026-01-01" / "cleaned.parquet"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"")
    assert manifest_output_relative_uri(out, tmp_path) == (
        "data/l1_layered/snap_x/t_bet/gaming_day=2026-01-01/cleaned.parquet"
    )


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_build_preprocess_manifest_validates_schema(tmp_path: Path) -> None:
    from jsonschema import Draft7Validator

    repo = Path(__file__).resolve().parents[2]
    schema_path = repo / "schema" / "manifest_layered_data_assets.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    stats = {
        "row_count": 0,
        "time_range_min": "2026-01-01T00:00:00Z",
        "time_range_max": "2026-01-01T01:00:00Z",
        "preprocess_subrules_applied": ["BET-PK-01"],
        "preprocessing_gaps": [],
    }
    m = build_preprocess_manifest(
        source_snapshot_id="snap_abcdefgh",
        gaming_day="2026-01-01",
        l0_fingerprint_path=None,
        output_parquet=tmp_path / "cleaned.parquet",
        manifest_uri_anchor=tmp_path,
        stats=stats,
    )
    Draft7Validator(schema).validate(m)
    assert m["output_relative_uri"] == "cleaned.parquet"
    assert not Path(m["output_relative_uri"]).is_absolute()
