"""Synthetic tests for YAML bulk_historical_ingest_episodes (BET-BULK-INGEST-*)."""

from pathlib import Path

import pytest

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]

from layered_data_assets.preprocess_bet_v1 import run_preprocess_bet_v1

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_INGEST_REGISTRY = _REPO_ROOT / "schema" / "preprocess_l0_data_contract_registry.yaml"

# Mirrors schema/preprocess_l0_data_contract_registry.yaml tables.t_bet bulk_historical_ingest_episodes.
_MATCH_ETL_DAY_2025_05_27 = (
    "CAST(date_trunc('day', TRY_CAST(__etl_insert_Dtm AS TIMESTAMP)) AS DATE) = DATE '2025-05-27'"
)
_MATCH_ETL_DAY_2025_07_21 = (
    "CAST(date_trunc('day', TRY_CAST(__etl_insert_Dtm AS TIMESTAMP)) AS DATE) = DATE '2025-07-21'"
)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_mock_row_matches_bet_bulk_ingest_2025_05_27_match_rule_sql(tmp_path: Path) -> None:
    """Synthetic row with ETL calendar day 2025-05-27 satisfies episode match_rule_sql."""
    inp = tmp_path / "in.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '2025-04-03',
                 TIMESTAMP '2025-04-03 10:00:00', TIMESTAMP '2025-05-27 18:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        n = con.execute(
            f"SELECT COUNT(*) FROM read_parquet(?) WHERE {_MATCH_ETL_DAY_2025_05_27}",
            [str(inp)],
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_mock_row_matches_bet_bulk_ingest_2025_07_21_match_rule_sql(tmp_path: Path) -> None:
    """Synthetic row with ETL calendar day 2025-07-21 satisfies episode match_rule_sql."""
    inp = tmp_path / "in.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '2025-06-01',
                 TIMESTAMP '2025-06-01 12:00:00', TIMESTAMP '2025-07-21 09:30:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        n = con.execute(
            f"SELECT COUNT(*) FROM read_parquet(?) WHERE {_MATCH_ETL_DAY_2025_07_21}",
            [str(inp)],
        ).fetchone()[0]
    finally:
        con.close()
    assert n == 1


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_bulk_etl_day_2025_05_27_preserves_gaming_day_and_synthetic_equals_cap_bound(
    tmp_path: Path,
) -> None:
    """Business gaming_day unchanged; FIX-004 synthetic = LEAST(etl, payout+122s)."""
    assert _DEFAULT_INGEST_REGISTRY.is_file()
    inp = tmp_path / "in.parquet"
    out = tmp_path / "cleaned.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '2025-04-03',
                 TIMESTAMP '2025-04-03 10:00:00', TIMESTAMP '2025-05-27 18:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        run_preprocess_bet_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out,
            gaming_day="2025-04-03",
            dummy_player_ids_parquet=None,
            eligible_player_ids_parquet=None,
            ingestion_fix_registry_path=_DEFAULT_INGEST_REGISTRY,
        )
        ok, gd = con.execute(
            f"""
            SELECT
              CAST(__etl_insert_Dtm_synthetic AS TIMESTAMP)
                = LEAST(
                  TRY_CAST(__etl_insert_Dtm AS TIMESTAMP),
                  TRY_CAST(payout_complete_dtm AS TIMESTAMP) + INTERVAL '122 seconds'
                ),
              CAST(gaming_day AS DATE)
            FROM read_parquet(?)
            """,
            [str(out)],
        ).fetchone()
    finally:
        con.close()
    assert ok is True
    assert str(gd) == "2025-04-03"


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_bulk_etl_day_2025_07_21_preserves_gaming_day_and_synthetic_equals_cap_bound(
    tmp_path: Path,
) -> None:
    """Mirror of 2025-05-27 episode for ETL calendar day 2025-07-21."""
    assert _DEFAULT_INGEST_REGISTRY.is_file()
    inp = tmp_path / "in.parquet"
    out = tmp_path / "cleaned.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (2::BIGINT, 101::BIGINT, DATE '2025-06-10',
                 TIMESTAMP '2025-06-10 08:00:00', TIMESTAMP '2025-07-21 14:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        run_preprocess_bet_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out,
            gaming_day="2025-06-10",
            dummy_player_ids_parquet=None,
            eligible_player_ids_parquet=None,
            ingestion_fix_registry_path=_DEFAULT_INGEST_REGISTRY,
        )
        ok, gd = con.execute(
            f"""
            SELECT
              CAST(__etl_insert_Dtm_synthetic AS TIMESTAMP)
                = LEAST(
                  TRY_CAST(__etl_insert_Dtm AS TIMESTAMP),
                  TRY_CAST(payout_complete_dtm AS TIMESTAMP) + INTERVAL '122 seconds'
                ),
              CAST(gaming_day AS DATE)
            FROM read_parquet(?)
            """,
            [str(out)],
        ).fetchone()
    finally:
        con.close()
    assert ok is True
    assert str(gd) == "2025-06-10"


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
@pytest.mark.parametrize(
    "etl_a,etl_b",
    [
        ("2025-05-27 10:00:00", "2025-05-27 11:00:00"),
        ("2025-07-21 10:00:00", "2025-07-21 11:00:00"),
    ],
)
def test_bulk_etl_calendar_day_duplicate_bet_id_cap_reverses_raw_etl_winner(
    tmp_path: Path,
    etl_a: str,
    etl_b: str,
) -> None:
    """Both rows on bulk ETL calendar day; synthetic ordering picks player 100 over 200."""
    assert _DEFAULT_INGEST_REGISTRY.is_file()
    inp = tmp_path / "in.parquet"
    out_no = tmp_path / "cleaned_no_cap.parquet"
    out_cap = tmp_path / "cleaned_cap.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (9001::BIGINT, 100::BIGINT, DATE '2025-02-01',
                 TIMESTAMP '2025-02-01 10:00:00', TIMESTAMP '{etl_a}',
                 0::INTEGER, 0::INTEGER, 0::INTEGER),
                (9001::BIGINT, 200::BIGINT, DATE '2025-02-01',
                 TIMESTAMP '2025-02-01 09:59:00', TIMESTAMP '{etl_b}',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{inp.as_posix()}' (FORMAT PARQUET)
            """
        )
        run_preprocess_bet_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out_no,
            gaming_day="2025-02-01",
            dummy_player_ids_parquet=None,
            eligible_player_ids_parquet=None,
        )
        run_preprocess_bet_v1(
            con=con,
            input_paths=[inp],
            output_parquet=out_cap,
            gaming_day="2025-02-01",
            dummy_player_ids_parquet=None,
            eligible_player_ids_parquet=None,
            ingestion_fix_registry_path=_DEFAULT_INGEST_REGISTRY,
        )
        pid_no = con.execute("SELECT player_id FROM read_parquet(?)", [str(out_no)]).fetchone()[0]
        pid_cap = con.execute("SELECT player_id FROM read_parquet(?)", [str(out_cap)]).fetchone()[0]
    finally:
        con.close()
    assert pid_no == 200
    assert pid_cap == 100
