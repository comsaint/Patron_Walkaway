"""Tests for ``layered_data_assets.lda_day_range_v1``."""

from pathlib import Path

import duckdb
import pytest

from layered_data_assets.lda_day_range_v1 import (
    distinct_gaming_days_from_l0_t_bet_layout,
    distinct_gaming_days_from_t_bet_parquet,
    inclusive_iso_date_strings,
)


def test_inclusive_iso_date_strings_single_day() -> None:
    assert inclusive_iso_date_strings("2026-01-05", "2026-01-05") == ["2026-01-05"]


def test_inclusive_iso_date_strings_three_days() -> None:
    assert inclusive_iso_date_strings("2026-01-01", "2026-01-03") == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    ]


def test_inclusive_iso_date_strings_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="on or after"):
        inclusive_iso_date_strings("2026-01-03", "2026-01-01")


def test_inclusive_iso_date_strings_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="date_from"):
        inclusive_iso_date_strings("01-01-2026", "2026-01-02")


def test_inclusive_iso_date_strings_rejects_impossible_date_from() -> None:
    with pytest.raises(ValueError, match=r"date_from invalid calendar date"):
        inclusive_iso_date_strings("2026-02-30", "2026-03-01")


def test_inclusive_iso_date_strings_rejects_impossible_date_to() -> None:
    with pytest.raises(ValueError, match=r"date_to invalid calendar date"):
        inclusive_iso_date_strings("2026-01-01", "2026-02-30")


def test_distinct_gaming_days_from_t_bet_parquet_sparse_days(tmp_path: Path) -> None:
    """Non-contiguous gaming_day values appear in order without filler days."""
    p = tmp_path / "tb.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (SELECT * FROM (VALUES
                (DATE '2026-01-01'),
                (DATE '2026-01-03')
            ) AS t(gaming_day)) TO '{p.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()
    assert distinct_gaming_days_from_t_bet_parquet(p) == ["2026-01-01", "2026-01-03"]


def test_distinct_gaming_days_from_t_bet_parquet_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.parquet"
    with pytest.raises(FileNotFoundError):
        distinct_gaming_days_from_t_bet_parquet(missing)


def test_distinct_gaming_days_from_l0_t_bet_layout(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    snap = data_root / "l0_layered" / "snap_test" / "t_bet" / "gaming_day=2026-02-02"
    snap.mkdir(parents=True)
    (snap / "part-000.parquet").write_bytes(b"x")
    other = data_root / "l0_layered" / "snap_test" / "t_bet" / "gaming_day=2026-02-01"
    other.mkdir(parents=True)
    (other / "part-000.parquet").write_bytes(b"y")
    assert distinct_gaming_days_from_l0_t_bet_layout(data_root) == ["2026-02-01", "2026-02-02"]


def test_distinct_gaming_days_from_l0_t_bet_layout_empty(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "l0_layered").mkdir(parents=True)
    with pytest.raises(ValueError, match="No L0"):
        distinct_gaming_days_from_l0_t_bet_layout(root)
