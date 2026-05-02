"""Tests for layered_data_assets.l0_paths (LDA-E1-01)."""

from pathlib import Path

import pytest

from layered_data_assets.l0_paths import (
    l0_layered_root,
    l0_partition_dir,
    l0_snapshot_root,
    validate_source_snapshot_id,
)


def test_validate_source_snapshot_id_accepts_minimal_body() -> None:
    validate_source_snapshot_id("snap_abcdefgh")


def test_validate_source_snapshot_id_rejects_bad_prefix() -> None:
    with pytest.raises(ValueError, match="snap_"):
        validate_source_snapshot_id("bad_abcdefgh")


def test_validate_source_snapshot_id_rejects_short_body() -> None:
    with pytest.raises(ValueError, match="body"):
        validate_source_snapshot_id("snap_abcde")


def test_validate_source_snapshot_id_rejects_path_traversal() -> None:
    with pytest.raises(ValueError, match="path"):
        validate_source_snapshot_id("snap_abcdefgh..")


def test_l0_snapshot_root_composes_paths() -> None:
    root = Path("/tmp/data")
    p = l0_snapshot_root(root, "snap_abcdefgh")
    assert p == root / "l0_layered" / "snap_abcdefgh"


def test_l0_layered_root() -> None:
    assert l0_layered_root(Path("x")) == Path("x") / "l0_layered"


def test_l0_partition_dir_hive_style() -> None:
    p = l0_partition_dir(Path("data"), "snap_abcdefgh", "t_bet", "gaming_day", "2026-04-01")
    assert p == Path("data") / "l0_layered" / "snap_abcdefgh" / "t_bet" / "gaming_day=2026-04-01"


def test_l0_partition_dir_rejects_partition_key_with_dotdot_substring() -> None:
    with pytest.raises(ValueError, match="invalid partition_key"):
        l0_partition_dir(Path("data"), "snap_abcdefgh", "t_bet", "gaming_.._day", "2026-04-01")
