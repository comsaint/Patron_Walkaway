"""Unit tests for ``run_bet_map_v1`` (DuckDB)."""

import json
from pathlib import Path

import pytest

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]

from layered_data_assets.run_bet_map_v1 import build_run_bet_map_manifest, materialize_run_bet_map_v1
from layered_data_assets.run_fact_v1 import materialize_run_fact_v1


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_run_bet_map_matches_run_fact_bet_counts(tmp_path: Path) -> None:
    """Map row counts per run_id match ``run_fact.bet_count``; bet sets match first/last."""
    inp = tmp_path / "in.parquet"
    rf = tmp_path / "run_fact.parquet"
    mp = tmp_path / "run_bet_map.parquet"
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
        materialize_run_fact_v1(
            con=con,
            input_paths=[inp],
            output_parquet=rf,
            run_end_gaming_day="2026-01-15",
            run_break_min=30,
        )
        materialize_run_bet_map_v1(
            con=con,
            input_paths=[inp],
            output_parquet=mp,
            run_end_gaming_day="2026-01-15",
            run_break_min=30,
        )
    finally:
        con.close()

    con2 = duckdb.connect(database=":memory:")
    try:
        j = con2.execute(
            """
            SELECT
              f.run_id,
              f.bet_count,
              m.cnt,
              f.first_bet_id,
              f.last_bet_id,
              m.bids
            FROM read_parquet(?) f
            INNER JOIN (
              SELECT
                run_id,
                COUNT(*)::BIGINT AS cnt,
                LIST(bet_id ORDER BY payout_complete_dtm ASC, bet_id ASC) AS bids
              FROM read_parquet(?)
              GROUP BY run_id
            ) m USING (run_id)
            ORDER BY f.run_id
            """,
            [str(rf), str(mp)],
        ).fetchall()
        assert len(j) == 2
        for _rid, bet_count, cnt, first_bet_id, last_bet_id, bids in j:
            assert bet_count == cnt
            assert bids[0] == first_bet_id
            assert bids[-1] == last_bet_id
        total = con2.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(mp)]).fetchone()[0]
        assert total == 3
    finally:
        con2.close()


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_build_run_bet_map_manifest_validates_schema(tmp_path: Path) -> None:
    from jsonschema import Draft7Validator

    repo = Path(__file__).resolve().parents[2]
    schema_path = repo / "schema" / "manifest_layered_data_assets.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    out = tmp_path / "run_bet_map.parquet"
    out.write_bytes(b"x")
    stats = {"row_count": 0, "time_range_min": "2026-01-01T00:00:00Z", "time_range_max": "2026-01-01T01:00:00Z"}
    m = build_run_bet_map_manifest(
        source_snapshot_id="snap_abcdefgh",
        run_end_gaming_day="2026-01-01",
        l0_fingerprint_path=None,
        l1_preprocess_gaming_day="2026-01-01",
        output_parquet=out,
        manifest_uri_anchor=tmp_path,
        stats=stats,
    )
    Draft7Validator(schema).validate(m)
    assert m["artifact_kind"] == "run_bet_map"
    assert len(m["source_partitions"]) == len(m["source_hashes"]) == 2
