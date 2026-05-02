"""Unit tests for ``run_day_bridge_v1`` (DuckDB)."""

import json
from pathlib import Path

import pytest

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]

from layered_data_assets.run_day_bridge_v1 import build_run_day_bridge_manifest, materialize_run_day_bridge_v1


def test_bet_gaming_day_must_be_yyyy_mm_dd() -> None:
    """``bet_gaming_day`` uses the same ISO date rule as ``run_end_gaming_day`` / preprocess."""
    with pytest.raises(ValueError, match="bet_gaming_day must be YYYY-MM-DD"):
        build_run_day_bridge_manifest(
            source_snapshot_id="snap_abcdefgh",
            bet_gaming_day="smoke-2026-05-02",
            l0_fingerprint_path=None,
            l1_preprocess_gaming_day="2026-01-01",
            output_parquet=Path("x.parquet"),
            manifest_uri_anchor=Path("."),
            stats={"row_count": 0},
        )


def test_l1_preprocess_gaming_day_must_be_yyyy_mm_dd() -> None:
    with pytest.raises(ValueError, match="l1_preprocess_gaming_day must be YYYY-MM-DD"):
        build_run_day_bridge_manifest(
            source_snapshot_id="snap_abcdefgh",
            bet_gaming_day="2026-01-01",
            l0_fingerprint_path=None,
            l1_preprocess_gaming_day="not-a-date",
            output_parquet=Path("x.parquet"),
            manifest_uri_anchor=Path("."),
            stats={"row_count": 0},
        )


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_materialize_rejects_non_iso_bet_gaming_day(tmp_path: Path) -> None:
    """Invalid ``bet_gaming_day`` fails before DuckDB reads ``input_paths``."""
    con = duckdb.connect(database=":memory:")
    try:
        out = tmp_path / "out.parquet"
        with pytest.raises(ValueError, match="bet_gaming_day must be YYYY-MM-DD"):
            materialize_run_day_bridge_v1(
                con=con,
                input_paths=[tmp_path / "not_read_yet.parquet"],
                output_parquet=out,
                bet_gaming_day="2026/01/01",
                run_break_min=30,
            )
    finally:
        con.close()


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_run_day_bridge_cross_day_run_appears_under_each_bet_gaming_day(tmp_path: Path) -> None:
    """One run spanning two calendar days is listed in both ``bet_gaming_day`` partitions."""
    inp = tmp_path / "in.parquet"
    b14 = tmp_path / "bridge14.parquet"
    b15 = tmp_path / "bridge15.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (SELECT * FROM (VALUES
              (1::BIGINT, 1::BIGINT, DATE '2026-01-14', TIMESTAMP '2026-01-14 23:50:00',
               TIMESTAMP '2026-01-14 23:50:00'),
              (2::BIGINT, 1::BIGINT, DATE '2026-01-15', TIMESTAMP '2026-01-15 00:10:00',
               TIMESTAMP '2026-01-15 00:10:00')
            ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        materialize_run_day_bridge_v1(
            con=con,
            input_paths=[inp],
            output_parquet=b14,
            bet_gaming_day="2026-01-14",
            run_break_min=30,
        )
        materialize_run_day_bridge_v1(
            con=con,
            input_paths=[inp],
            output_parquet=b15,
            bet_gaming_day="2026-01-15",
            run_break_min=30,
        )
    finally:
        con.close()

    con2 = duckdb.connect(database=":memory:")
    try:
        r14 = con2.execute("SELECT run_id FROM read_parquet(?) ORDER BY run_id", [str(b14)]).fetchall()
        r15 = con2.execute("SELECT run_id FROM read_parquet(?) ORDER BY run_id", [str(b15)]).fetchall()
        assert len(r14) == 1 and len(r15) == 1
        assert r14[0][0] == r15[0][0]
    finally:
        con2.close()


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_run_day_bridge_empty_partition_when_no_bets_on_day(tmp_path: Path) -> None:
    """``bet_gaming_day`` with no bets in inputs yields zero rows (still valid Parquet)."""
    inp = tmp_path / "in.parquet"
    out = tmp_path / "bridge_empty.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (SELECT * FROM (VALUES
              (1::BIGINT, 1::BIGINT, DATE '2026-01-15', TIMESTAMP '2026-01-15 10:00:00',
               TIMESTAMP '2026-01-15 10:00:00')
            ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        stats = materialize_run_day_bridge_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out,
            bet_gaming_day="2026-01-14",
            run_break_min=30,
        )
    finally:
        con.close()
    assert stats["row_count"] == 0


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_build_run_day_bridge_manifest_validates_schema(tmp_path: Path) -> None:
    from jsonschema import Draft7Validator

    repo = Path(__file__).resolve().parents[2]
    schema_path = repo / "schema" / "manifest_layered_data_assets.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    out = tmp_path / "run_day_bridge.parquet"
    out.write_bytes(b"x")
    stats = {"row_count": 0, "time_range_min": "2026-01-01T00:00:00Z", "time_range_max": "2026-01-01T01:00:00Z"}
    m = build_run_day_bridge_manifest(
        source_snapshot_id="snap_abcdefgh",
        bet_gaming_day="2026-01-01",
        l0_fingerprint_path=None,
        l1_preprocess_gaming_day="2026-01-01",
        output_parquet=out,
        manifest_uri_anchor=tmp_path,
        stats=stats,
    )
    Draft7Validator(schema).validate(m)
    assert m["artifact_kind"] == "run_day_bridge"
    assert m["partition_keys"]["bet_gaming_day"] == "2026-01-01"
    assert len(m["source_partitions"]) == len(m["source_hashes"]) == 1
