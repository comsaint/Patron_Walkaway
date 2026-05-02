"""Tests for layered_data_assets.l1_paths."""

from pathlib import Path

import pytest

from layered_data_assets.l1_paths import (
    l1_bet_partition_dir,
    l1_layered_root,
    l1_run_fact_partition_dir,
    l1_snapshot_root,
)


def test_l1_layered_root() -> None:
    assert l1_layered_root(Path("d")) == Path("d") / "l1_layered"


def test_l1_snapshot_root() -> None:
    p = l1_snapshot_root(Path("/x"), "snap_abcdefgh")
    assert p == Path("/x") / "l1_layered" / "snap_abcdefgh"


def test_l1_bet_partition_dir() -> None:
    p = l1_bet_partition_dir(Path("data"), "snap_abcdefgh", "2026-04-01")
    assert p == Path("data") / "l1_layered" / "snap_abcdefgh" / "t_bet" / "gaming_day=2026-04-01"


def test_l1_bet_partition_dir_rejects_bad_gaming_day() -> None:
    with pytest.raises(ValueError, match="invalid gaming_day"):
        l1_bet_partition_dir(Path("data"), "snap_abcdefgh", "2026/04/01")


def test_l1_run_fact_partition_dir() -> None:
    p = l1_run_fact_partition_dir(Path("data"), "snap_abcdefgh", "2026-04-01")
    assert p == Path("data") / "l1_layered" / "snap_abcdefgh" / "run_fact" / "run_end_gaming_day=2026-04-01"


def test_l1_run_fact_partition_dir_rejects_bad_day() -> None:
    with pytest.raises(ValueError, match="invalid run_end_gaming_day"):
        l1_run_fact_partition_dir(Path("data"), "snap_abcdefgh", "2026/04/01")
