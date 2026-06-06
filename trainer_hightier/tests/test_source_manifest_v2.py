"""Tests for content-addressed source manifest v2 (Phase 1)."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.utils.partition_inventory import (
    PartitionParquetStat,
    scan_partition_snapshot_dir,
)
from trainer_hightier.utils.source_manifest_v2 import (
    CHANGE_ADDED,
    CHANGE_MODIFIED,
    CHANGE_REMOVED,
    CHANGE_UNCHANGED,
    build_source_file_record,
    build_source_manifest_v2,
    diff_source_manifests,
    load_source_manifest_v2,
    materialize_source_manifest_v2_phase1,
    sha256_file_bytes,
    validate_partition_yyyymm,
)


def _tiny_parquet_bytes(*, value: int = 1) -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.table({"col": [value]}), buf)
    return buf.getvalue()


def _write_legacy_snapshot(root: Path, *, months: tuple[str, ...] = ("202401",)) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for ym in months:
        (root / f"t_bet__part_{ym}.parquet").write_bytes(_tiny_parquet_bytes())
        (root / f"t_session__part_{ym}.parquet").write_bytes(_tiny_parquet_bytes())


def test_t1_identical_bytes_different_mtime_are_unchanged(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    _write_legacy_snapshot(snap)
    cache = tmp_path / "cache"
    first = materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="run1")
    assert first["source_manifest_v2_diff_summary"]["added"] == 2
    bet_path = snap / "t_bet__part_202401.parquet"
    os.utime(bet_path, (time.time() + 3600, time.time() + 3600))
    second = materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="run2")
    summary = second["source_manifest_v2_diff_summary"]
    assert summary["modified"] == 0
    assert summary["unchanged"] == 2
    assert summary["added"] == 0
    assert summary["removed"] == 0


def test_t2_single_file_modified_marks_one_partition(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    _write_legacy_snapshot(snap)
    cache = tmp_path / "cache"
    materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="baseline")
    (snap / "t_bet__part_202401.parquet").write_bytes(_tiny_parquet_bytes(value=99))
    got = materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="after_edit")
    summary = got["source_manifest_v2_diff_summary"]
    assert summary["modified"] == 1
    assert summary["unchanged"] == 1
    assert got["source_manifest_v2_changed_partitions"] == {"t_bet": ["202401"], "t_session": []}
    change_set = json.loads(Path(got["source_manifest_v2_change_set_path"]).read_text(encoding="utf-8"))
    kinds = {row["change_kind"] for row in change_set["changed_files"]}
    assert kinds == {CHANGE_MODIFIED}


def test_t3_added_partition_file(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    _write_legacy_snapshot(snap, months=("202401",))
    cache = tmp_path / "cache"
    materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="baseline")
    (snap / "t_bet__part_202402.parquet").write_bytes(_tiny_parquet_bytes())
    got = materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="after_add")
    summary = got["source_manifest_v2_diff_summary"]
    assert summary["added"] == 1
    assert summary["unchanged"] == 2
    assert got["source_manifest_v2_changed_partitions"]["t_bet"] == ["202402"]


def test_t4_removed_partition_file(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    _write_legacy_snapshot(snap, months=("202401", "202402"))
    cache = tmp_path / "cache"
    materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="baseline")
    (snap / "t_session__part_202402.parquet").unlink()
    got = materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="after_remove")
    summary = got["source_manifest_v2_diff_summary"]
    assert summary["removed"] == 1
    assert got["source_manifest_v2_changed_partitions"]["t_session"] == ["202402"]


def test_t5_unmappable_partition_yyyymm_fail_fast(tmp_path: Path) -> None:
    p = tmp_path / "bad.parquet"
    p.write_bytes(_tiny_parquet_bytes())
    stat = PartitionParquetStat(
        path=p,
        yyyymm="20X401",
        role="t_bet",
        mtime_ns=0,
        size_bytes=1,
        num_rows=1,
    )
    with pytest.raises(ValueError, match="partition_yyyymm must be six digits"):
        build_source_file_record(stat, snapshot_dir=tmp_path, file_sha256=sha256_file_bytes(p))
    with pytest.raises(ValueError, match="partition_yyyymm must be six digits"):
        validate_partition_yyyymm("bad", path=p)


def test_t6_missing_previous_manifest_treats_all_as_added(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    _write_legacy_snapshot(snap)
    cache = tmp_path / "cache"
    got = materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="first")
    summary = got["source_manifest_v2_diff_summary"]
    assert summary == {"added": 2, "removed": 0, "modified": 0, "unchanged": 0}
    assert got["source_manifest_v2_changed_partitions"]["t_bet"] == ["202401"]
    assert got["source_manifest_v2_changed_partitions"]["t_session"] == ["202401"]


def test_t6_corrupt_previous_manifest_treats_all_as_added(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    _write_legacy_snapshot(snap)
    cache = tmp_path / "cache"
    prev_dir = cache / "source_manifest_v2"
    prev_dir.mkdir(parents=True)
    (prev_dir / "previous.json").write_text("{not json", encoding="utf-8")
    got = materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="corrupt_prev")
    assert got["source_manifest_v2_diff_summary"]["added"] == 2
    assert load_source_manifest_v2(prev_dir / "previous.json") is not None


def test_diff_classifies_unchanged_modified_added_removed() -> None:
    prev = {
        "files": [
            {"table": "t_bet", "relative_path": "a.parquet", "file_sha256": "1", "partition_yyyymm": "202401"},
            {"table": "t_bet", "relative_path": "mod.parquet", "file_sha256": "old", "partition_yyyymm": "202402"},
            {"table": "t_bet", "relative_path": "gone.parquet", "file_sha256": "2", "partition_yyyymm": "202403"},
        ],
    }
    cur = {
        "files": [
            {"table": "t_bet", "relative_path": "a.parquet", "file_sha256": "1", "partition_yyyymm": "202401"},
            {"table": "t_bet", "relative_path": "mod.parquet", "file_sha256": "new", "partition_yyyymm": "202402"},
            {"table": "t_bet", "relative_path": "added.parquet", "file_sha256": "3", "partition_yyyymm": "202404"},
        ],
    }
    diff = diff_source_manifests(prev, cur)
    assert len(diff.unchanged) == 1
    assert len(diff.added) == 1
    assert len(diff.removed) == 1
    assert len(diff.modified) == 1


def test_build_manifest_records_sorted_and_atomic_paths_exist(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    _write_legacy_snapshot(snap)
    cache = tmp_path / "cache"
    got = materialize_source_manifest_v2_phase1(snapshot_dir=snap, cache_root=cache, run_id="paths")
    assert Path(got["source_manifest_v2_change_set_path"]).is_file()
    assert Path(got["source_manifest_v2_cache_report_path"]).is_file()
    assert Path(got["source_manifest_v2_current_path"]).is_file()
    bets, sess = scan_partition_snapshot_dir(snap)
    manifest, _, _ = build_source_manifest_v2(snapshot_dir=snap, snapshot_id="snap", bet_stats=bets, session_stats=sess)
    paths = [f["relative_path"] for f in manifest["files"]]
    assert paths == sorted(paths)
