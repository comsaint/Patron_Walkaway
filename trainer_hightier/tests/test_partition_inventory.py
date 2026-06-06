"""Partition inventory manifest + deterministic recompute month selection."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.utils.partition_inventory import (
    LAYOUT_LEGACY_MONTHLY_PARQUET,
    LAYOUT_TABLE_PARTITION_SHARDS,
    compute_recompute_months,
    default_partition_inventory_path,
    default_partition_snapshot_dir,
    detect_partition_layout,
    expect_default_partition_snapshot_dir,
    expect_existing_partition_snapshot_dir,
    inventory_to_manifest_dict,
    months_added_or_changed,
    resolve_partition_inventory_previous_for_run,
    scan_partition_snapshot_dir,
    write_partition_inventory_manifest,
)


def _tiny_parquet_bytes() -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.table({"col": [1]}), buf)
    return buf.getvalue()


def test_scan_partition_snapshot_lists_sorted_months(tmp_path: Path) -> None:
    d = tmp_path / "snap"
    d.mkdir()
    (d / "t_bet__part_202402.parquet").write_bytes(_tiny_parquet_bytes())
    (d / "t_bet__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    (d / "t_session__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    bets, sess = scan_partition_snapshot_dir(d)
    assert [x.yyyymm for x in bets] == ["202401", "202402"]
    assert [x.yyyymm for x in sess] == ["202401"]


def test_scan_partition_snapshot_supports_nested_export_subdirs(tmp_path: Path) -> None:
    d = tmp_path / "snap"
    nested = d / "20260512"
    nested.mkdir(parents=True)
    (nested / "t_bet__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    (nested / "t_session__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    bets, sess = scan_partition_snapshot_dir(d)
    assert len(bets) == 1
    assert len(sess) == 1
    assert bets[0].yyyymm == "202401"
    assert sess[0].yyyymm == "202401"


def test_compute_recompute_first_run_all_months(tmp_path: Path) -> None:
    d = tmp_path / "snap"
    d.mkdir()
    (d / "t_bet__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    (d / "t_session__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    bets, sess = scan_partition_snapshot_dir(d)
    m = inventory_to_manifest_dict("snap", snapshot_dir=d, bet_stats=bets, session_stats=sess)
    got = compute_recompute_months(
        current_manifest=m,
        previous_manifest=None,
        correction_months=(),
        backfill_month_count=0,
    )
    assert got == ["202401"]


def test_months_added_when_new_partition_file(tmp_path: Path) -> None:
    d1 = tmp_path / "snap1"
    d1.mkdir()
    (d1 / "t_bet__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    (d1 / "t_session__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    b1, s1 = scan_partition_snapshot_dir(d1)
    m1 = inventory_to_manifest_dict("s", snapshot_dir=d1, bet_stats=b1, session_stats=s1)

    d2 = tmp_path / "snap2"
    d2.mkdir()
    (d2 / "t_bet__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    (d2 / "t_bet__part_202402.parquet").write_bytes(_tiny_parquet_bytes())
    (d2 / "t_session__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    (d2 / "t_session__part_202402.parquet").write_bytes(_tiny_parquet_bytes())
    b2, s2 = scan_partition_snapshot_dir(d2)
    m2 = inventory_to_manifest_dict("s", snapshot_dir=d2, bet_stats=b2, session_stats=s2)

    assert "202402" in months_added_or_changed(m2, m1)


def test_write_manifest_roundtrip_loads(tmp_path: Path) -> None:
    from trainer_hightier.utils.partition_inventory import load_partition_inventory_manifest

    d = tmp_path / "snap"
    d.mkdir()
    (d / "t_bet__part_202406.parquet").write_bytes(_tiny_parquet_bytes())
    (d / "t_session__part_202406.parquet").write_bytes(_tiny_parquet_bytes())
    bets, sess = scan_partition_snapshot_dir(d)
    m = inventory_to_manifest_dict("june", snapshot_dir=d, bet_stats=bets, session_stats=sess)
    outp = tmp_path / "inventory.json"
    write_partition_inventory_manifest(outp, m)
    got = load_partition_inventory_manifest(outp)
    assert got["fingerprint_sha256_hex"] == m["fingerprint_sha256_hex"]


def _write_table_dir_shard(table_root: Path, yyyymm: str, shard_name: str) -> None:
    part = table_root / f"partition_{yyyymm}"
    part.mkdir(parents=True, exist_ok=True)
    (part / shard_name).write_bytes(_tiny_parquet_bytes())


def test_scan_table_dir_partition_shards(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_table_dir_shard(data / "t_bet", "202406", "part_000001.parquet")
    _write_table_dir_shard(data / "t_bet", "202406", "part_000002.parquet")
    _write_table_dir_shard(data / "t_session", "202406", "part_000001.parquet")
    bets, sess = scan_partition_snapshot_dir(data)
    assert detect_partition_layout(data) == LAYOUT_TABLE_PARTITION_SHARDS
    assert [x.yyyymm for x in bets] == ["202406", "202406"]
    assert [x.yyyymm for x in sess] == ["202406"]


def test_scan_prefers_table_dir_when_legacy_also_present(tmp_path: Path) -> None:
    data = tmp_path / "data"
    legacy = data / "partitions"
    legacy.mkdir(parents=True)
    (legacy / "t_bet__part_202405.parquet").write_bytes(_tiny_parquet_bytes())
    (legacy / "t_session__part_202405.parquet").write_bytes(_tiny_parquet_bytes())
    _write_table_dir_shard(data / "t_bet", "202406", "part_000001.parquet")
    _write_table_dir_shard(data / "t_session", "202406", "part_000001.parquet")
    bets, sess = scan_partition_snapshot_dir(data)
    assert detect_partition_layout(data) == LAYOUT_TABLE_PARTITION_SHARDS
    assert [x.yyyymm for x in bets] == ["202406"]
    assert [x.yyyymm for x in sess] == ["202406"]


def test_default_partition_snapshot_dir_prefers_table_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    legacy = data / "partitions"
    legacy.mkdir(parents=True)
    (legacy / "t_bet__part_202406.parquet").write_bytes(_tiny_parquet_bytes())
    _write_table_dir_shard(data / "t_bet", "202406", "part_000001.parquet")
    _write_table_dir_shard(data / "t_session", "202406", "part_000001.parquet")
    assert default_partition_snapshot_dir(repo_root=tmp_path) == data.resolve()


def test_layout_kind_change_marks_all_months_changed(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "t_bet__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    (legacy_dir / "t_session__part_202401.parquet").write_bytes(_tiny_parquet_bytes())
    b1, s1 = scan_partition_snapshot_dir(legacy_dir)
    m_legacy = inventory_to_manifest_dict(
        "legacy",
        snapshot_dir=legacy_dir,
        bet_stats=b1,
        session_stats=s1,
        layout_kind=LAYOUT_LEGACY_MONTHLY_PARQUET,
    )

    data = tmp_path / "data"
    _write_table_dir_shard(data / "t_bet", "202401", "part_000001.parquet")
    _write_table_dir_shard(data / "t_session", "202401", "part_000001.parquet")
    b2, s2 = scan_partition_snapshot_dir(data)
    m_table = inventory_to_manifest_dict("data", snapshot_dir=data, bet_stats=b2, session_stats=s2)
    assert months_added_or_changed(m_table, m_legacy) == {"202401"}


def test_default_partition_snapshot_dir_only_when_dir_exists(tmp_path: Path) -> None:
    assert default_partition_snapshot_dir(repo_root=tmp_path) is None
    target = tmp_path / "data" / "partitions"
    target.mkdir(parents=True)
    (target / "t_bet__part_202406.parquet").write_bytes(_tiny_parquet_bytes())
    (target / "t_session__part_202406.parquet").write_bytes(_tiny_parquet_bytes())
    assert default_partition_snapshot_dir(repo_root=tmp_path) == target.resolve()


def test_resolve_partition_inventory_previous_auto_same_snapshot_id(tmp_path: Path) -> None:
    snap = tmp_path / "my_export"
    snap.mkdir()
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    prev_path = default_partition_inventory_path(manifests_dir=manifests, snapshot_id="my_export")
    prev_path.write_text('{"manifest_kind": "x"}', encoding="utf-8")
    got = resolve_partition_inventory_previous_for_run(
        manifests_dir=manifests,
        snapshot_dir=snap,
        explicit_previous=None,
    )
    assert got == prev_path.resolve()


def test_resolve_partition_inventory_previous_explicit_wins(tmp_path: Path) -> None:
    snap = tmp_path / "my_export"
    snap.mkdir()
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    auto = default_partition_inventory_path(manifests_dir=manifests, snapshot_id="my_export")
    auto.write_text('{"manifest_kind": "auto"}', encoding="utf-8")
    explicit = tmp_path / "other.json"
    explicit.write_text('{"manifest_kind": "explicit"}', encoding="utf-8")
    got = resolve_partition_inventory_previous_for_run(
        manifests_dir=manifests,
        snapshot_dir=snap,
        explicit_previous=explicit,
    )
    assert got == explicit.resolve()


def test_expect_default_partition_snapshot_dir_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Expected default partition snapshot layout"):
        expect_default_partition_snapshot_dir(repo_root=tmp_path)


def test_expect_default_partition_snapshot_dir_returns_resolved(tmp_path: Path) -> None:
    target = tmp_path / "data" / "partitions"
    target.mkdir(parents=True)
    (target / "t_bet__part_202406.parquet").write_bytes(_tiny_parquet_bytes())
    (target / "t_session__part_202406.parquet").write_bytes(_tiny_parquet_bytes())
    got = expect_default_partition_snapshot_dir(repo_root=tmp_path)
    assert got == target.resolve()


def test_expect_existing_partition_snapshot_dir_ok(tmp_path: Path) -> None:
    d = tmp_path / "snap"
    d.mkdir()
    got = expect_existing_partition_snapshot_dir(d)
    assert got == d.resolve()


def test_expect_existing_partition_snapshot_dir_raises_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_dir"
    with pytest.raises(FileNotFoundError, match="missing path"):
        expect_existing_partition_snapshot_dir(missing)


def test_expect_existing_partition_snapshot_dir_raises_when_not_dir(tmp_path: Path) -> None:
    f = tmp_path / "file_not_dir"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError, match="must be a directory"):
        expect_existing_partition_snapshot_dir(f)
