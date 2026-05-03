"""Unit tests for ``trip_fact_v1`` / ``trip_id_v1`` (Phase 2 MVP)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from layered_data_assets.trip_fact_v1 import (
    TRIP_DEFINITION_VERSION_DEFAULT,
    SOURCE_NAMESPACE_DEFAULT,
    build_trip_fact_and_run_map_frames,
    materialize_trip_partition_parquets,
    source_partitions_from_runs,
)
from layered_data_assets.trip_id_v1 import derive_trip_id


def _sample_runs(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_derive_trip_id_stable() -> None:
    tid = derive_trip_id(
        player_id=100,
        trip_start_gaming_day="2026-01-01",
        first_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        trip_definition_version=TRIP_DEFINITION_VERSION_DEFAULT,
        source_namespace=SOURCE_NAMESPACE_DEFAULT,
        source_snapshot_id="snap_unit_test_01",
    )
    tid2 = derive_trip_id(
        player_id=100,
        trip_start_gaming_day="2026-01-01",
        first_run_id="run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        trip_definition_version=TRIP_DEFINITION_VERSION_DEFAULT,
        source_namespace=SOURCE_NAMESPACE_DEFAULT,
        source_snapshot_id="snap_unit_test_01",
    )
    assert tid == tid2
    assert tid.startswith("trip_")


def test_three_empty_days_splits_trip() -> None:
    """Gap of 3 full empty gaming_days between runs starts a new trip (impl plan §4.3)."""
    runs = _sample_runs(
        [
            {
                "player_id": 1,
                "run_id": "run_a1",
                "first_bet_id": 1,
                "last_bet_id": 2,
                "run_start_ts": "2026-01-01 10:00:00",
                "run_end_ts": "2026-01-01 11:00:00",
                "run_start_gaming_day": "2026-01-01",
                "run_end_gaming_day": "2026-01-01",
                "bet_count": 2,
                "run_definition_version": "run_boundary_v1",
                "source_namespace": "ns",
            },
            {
                "player_id": 1,
                "run_id": "run_a2",
                "first_bet_id": 3,
                "last_bet_id": 4,
                "run_start_ts": "2026-01-05 10:00:00",
                "run_end_ts": "2026-01-05 11:00:00",
                "run_start_gaming_day": "2026-01-05",
                "run_end_gaming_day": "2026-01-05",
                "bet_count": 2,
                "run_definition_version": "run_boundary_v1",
                "source_namespace": "ns",
            },
        ]
    )
    trip_df, map_df = build_trip_fact_and_run_map_frames(
        runs,
        source_snapshot_id="snap_unit_test_02",
        trip_definition_version=TRIP_DEFINITION_VERSION_DEFAULT,
        source_namespace=SOURCE_NAMESPACE_DEFAULT,
        coverage_end=date(2026, 1, 8),
    )
    assert len(trip_df) == 2
    assert set(trip_df["trip_start_gaming_day"]) == {"2026-01-01", "2026-01-05"}
    by_start = trip_df.set_index("trip_start_gaming_day")
    assert bool(by_start.loc["2026-01-01", "is_trip_closed"]) is True
    assert by_start.loc["2026-01-01", "trip_end_gaming_day"] == "2026-01-04"
    assert bool(by_start.loc["2026-01-05", "is_trip_closed"]) is True
    assert by_start.loc["2026-01-05", "trip_end_gaming_day"] == "2026-01-08"
    assert len(map_df) == 2


def test_tail_open_trip_when_insufficient_empty_days() -> None:
    runs = _sample_runs(
        [
            {
                "player_id": 2,
                "run_id": "run_b1",
                "first_bet_id": 1,
                "last_bet_id": 1,
                "run_start_ts": "2026-01-01 08:00:00",
                "run_end_ts": "2026-01-01 09:00:00",
                "run_start_gaming_day": "2026-01-01",
                "run_end_gaming_day": "2026-01-01",
                "bet_count": 1,
                "run_definition_version": "run_boundary_v1",
                "source_namespace": "ns",
            },
        ]
    )
    trip_df, _ = build_trip_fact_and_run_map_frames(
        runs,
        source_snapshot_id="snap_unit_test_03",
        coverage_end=date(2026, 1, 3),
    )
    assert len(trip_df) == 1
    assert not bool(trip_df.iloc[0]["is_trip_closed"])
    assert trip_df.iloc[0]["trip_end_gaming_day"] is None or pd.isna(trip_df.iloc[0]["trip_end_gaming_day"])


def test_source_partitions_from_runs_sorted() -> None:
    runs = _sample_runs(
        [
            {
                "player_id": 1,
                "run_id": "r1",
                "first_bet_id": 1,
                "last_bet_id": 1,
                "run_start_ts": "2026-01-02 10:00:00",
                "run_end_ts": "2026-01-02 11:00:00",
                "run_start_gaming_day": "2026-01-02",
                "run_end_gaming_day": "2026-01-03",
                "bet_count": 1,
                "run_definition_version": "run_boundary_v1",
                "source_namespace": "ns",
            },
            {
                "player_id": 1,
                "run_id": "r2",
                "first_bet_id": 2,
                "last_bet_id": 2,
                "run_start_ts": "2026-01-01 10:00:00",
                "run_end_ts": "2026-01-01 11:00:00",
                "run_start_gaming_day": "2026-01-01",
                "run_end_gaming_day": "2026-01-01",
                "bet_count": 1,
                "run_definition_version": "run_boundary_v1",
                "source_namespace": "ns",
            },
        ]
    )
    sp = source_partitions_from_runs(runs)
    assert sp == [
        "l1/run_fact/run_end_gaming_day=2026-01-01",
        "l1/run_fact/run_end_gaming_day=2026-01-03",
    ]


def test_materialize_trip_partition_writes_parquet(tmp_path: Path) -> None:
    import duckdb

    runs = _sample_runs(
        [
            {
                "player_id": 9,
                "run_id": "run_c1",
                "first_bet_id": 1,
                "last_bet_id": 1,
                "run_start_ts": "2026-02-01 12:00:00",
                "run_end_ts": "2026-02-01 13:00:00",
                "run_start_gaming_day": "2026-02-01",
                "run_end_gaming_day": "2026-02-01",
                "bet_count": 1,
                "run_definition_version": "run_boundary_v1",
                "source_namespace": "ns",
            },
        ]
    )
    rf = tmp_path / "run_fact.parquet"
    con = duckdb.connect(database=":memory:")
    con.register("runs", runs)
    con.execute(f"COPY runs TO '{rf.as_posix()}' (FORMAT PARQUET)")
    con.close()
    con2 = duckdb.connect(database=":memory:")
    out_t = tmp_path / "trip_fact.parquet"
    out_m = tmp_path / "trip_run_map.parquet"
    stats = materialize_trip_partition_parquets(
        con=con2,
        run_fact_paths=[rf],
        trip_start_gaming_day="2026-02-01",
        trip_fact_out=out_t,
        trip_run_map_out=out_m,
        source_snapshot_id="snap_unit_test_04",
        coverage_end=date(2026, 2, 1),
    )
    assert stats["row_count_trip_fact"] == 1
    assert stats["row_count_trip_run_map"] == 1
    assert out_t.is_file() and out_m.is_file()
    con2.close()


def test_materialize_empty_partition_no_rows(tmp_path: Path) -> None:
    import duckdb

    runs = _sample_runs(
        [
            {
                "player_id": 9,
                "run_id": "run_d1",
                "first_bet_id": 1,
                "last_bet_id": 1,
                "run_start_ts": "2026-02-01 12:00:00",
                "run_end_ts": "2026-02-01 13:00:00",
                "run_start_gaming_day": "2026-02-01",
                "run_end_gaming_day": "2026-02-01",
                "bet_count": 1,
                "run_definition_version": "run_boundary_v1",
                "source_namespace": "ns",
            },
        ]
    )
    rf = tmp_path / "run_fact2.parquet"
    con = duckdb.connect(database=":memory:")
    con.register("runs", runs)
    con.execute(f"COPY runs TO '{rf.as_posix()}' (FORMAT PARQUET)")
    con.close()
    con2 = duckdb.connect(database=":memory:")
    out_t = tmp_path / "trip_fact_empty.parquet"
    out_m = tmp_path / "trip_run_map_empty.parquet"
    stats = materialize_trip_partition_parquets(
        con=con2,
        run_fact_paths=[rf],
        trip_start_gaming_day="2026-03-01",
        trip_fact_out=out_t,
        trip_run_map_out=out_m,
        source_snapshot_id="snap_unit_test_05",
        coverage_end=date(2026, 2, 1),
    )
    assert stats["row_count_trip_fact"] == 0
    assert stats["row_count_trip_run_map"] == 0
    con2.close()
