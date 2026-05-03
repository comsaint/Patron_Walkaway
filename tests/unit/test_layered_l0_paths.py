"""Tests for layered_data_assets.l0_paths (LDA-E1-01)."""

from pathlib import Path

import pytest

from layered_data_assets.l0_paths import (
    discover_l0_snapshot_ids_for_partition,
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


def test_l0_partition_dir_rejects_partition_value_with_equals() -> None:
    with pytest.raises(ValueError, match="partition_value"):
        l0_partition_dir(Path("data"), "snap_abcdefgh", "t_bet", "gaming_day", "2026=04=01")


def test_discover_l0_snapshot_ids_for_partition_finds_snap(tmp_path: Path) -> None:
    part = (
        tmp_path
        / "l0_layered"
        / "snap_zzzzzzzz"
        / "t_bet"
        / "gaming_day=2026-02-01"
    )
    part.mkdir(parents=True)
    (part / "part-000.parquet").write_bytes(b"")
    ids = discover_l0_snapshot_ids_for_partition(
        tmp_path,
        table="t_bet",
        partition_key="gaming_day",
        partition_value="2026-02-01",
    )
    assert ids == ["snap_zzzzzzzz"]


def test_discover_l0_snapshot_ids_for_partition_empty(tmp_path: Path) -> None:
    assert (
        discover_l0_snapshot_ids_for_partition(
            tmp_path,
            table="t_bet",
            partition_key="gaming_day",
            partition_value="2099-01-01",
        )
        == []
    )
