"""Gate 1 L1 determinism (LDA-E1-08): multi-profile DuckDB vs row fingerprint."""

from pathlib import Path

import pytest

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]

from layered_data_assets.l1_determinism_gate_v1 import gate1_l1_report_across_duckdb_profiles

_PROFILES_SHORT: list[tuple[int | None, int]] = [(None, 2), (256, 2), (128, 2)]


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_gate1_run_fact_stable_across_duckdb_profiles(tmp_path: Path) -> None:
    inp = tmp_path / "in.parquet"
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
    finally:
        con.close()
    rep = gate1_l1_report_across_duckdb_profiles(
        duckdb_module=duckdb,
        artifact="run_fact",
        input_paths=[inp],
        output_dir=tmp_path / "gate_run_fact",
        profiles=_PROFILES_SHORT,
        run_end_gaming_day="2026-01-15",
        run_break_min=30,
    )
    assert rep["all_row_counts_match"] is True
    assert rep["all_row_fingerprints_match"] is True
    assert rep["all_row_fingerprint_row_counts_match_stats"] is True


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_gate1_run_bet_map_stable_across_duckdb_profiles(tmp_path: Path) -> None:
    inp = tmp_path / "in.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (SELECT * FROM (VALUES
              (1::BIGINT, 100::BIGINT, DATE '2026-01-15', TIMESTAMP '2026-01-15 10:00:00',
               TIMESTAMP '2026-01-15 11:00:00'),
              (2::BIGINT, 100::BIGINT, DATE '2026-01-15', TIMESTAMP '2026-01-15 10:15:00',
               TIMESTAMP '2026-01-15 11:05:00')
            ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    rep = gate1_l1_report_across_duckdb_profiles(
        duckdb_module=duckdb,
        artifact="run_bet_map",
        input_paths=[inp],
        output_dir=tmp_path / "gate_map",
        profiles=_PROFILES_SHORT,
        run_end_gaming_day="2026-01-15",
        run_break_min=30,
    )
    assert rep["all_row_counts_match"] is True
    assert rep["all_row_fingerprints_match"] is True


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_gate1_run_day_bridge_stable_across_duckdb_profiles(tmp_path: Path) -> None:
    inp = tmp_path / "in.parquet"
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
    finally:
        con.close()
    rep = gate1_l1_report_across_duckdb_profiles(
        duckdb_module=duckdb,
        artifact="run_day_bridge",
        input_paths=[inp],
        output_dir=tmp_path / "gate_bridge",
        profiles=_PROFILES_SHORT,
        bet_gaming_day="2026-01-14",
        run_break_min=30,
    )
    assert rep["all_row_counts_match"] is True
    assert rep["all_row_fingerprints_match"] is True
