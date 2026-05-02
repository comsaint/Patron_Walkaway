"""Unit tests for layered_data_assets.l0_fingerprint."""

from pathlib import Path

import pytest

from layered_data_assets.l0_fingerprint import (
    build_fingerprint_document,
    derive_source_snapshot_id,
    fingerprint_canonical_json,
    normalized_sorted_sources,
    relative_path_for_fingerprint,
    sha256_file,
)


def test_sha256_file_empty(tmp_path: Path) -> None:
    p = tmp_path / "a.bin"
    p.write_bytes(b"")
    assert sha256_file(p) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_fingerprint_order_independent(tmp_path: Path) -> None:
    anchor = tmp_path
    p1 = tmp_path / "z.txt"
    p2 = tmp_path / "a.txt"
    p1.write_bytes(b"1")
    p2.write_bytes(b"2")
    d1 = build_fingerprint_document(
        source_paths=[p1, p2],
        anchor=anchor,
        table="t_bet",
        partition_key="gaming_day",
        partition_value="2026-04-01",
    )
    d2 = build_fingerprint_document(
        source_paths=[p2, p1],
        anchor=anchor,
        table="t_bet",
        partition_key="gaming_day",
        partition_value="2026-04-01",
    )
    assert fingerprint_canonical_json(d1) == fingerprint_canonical_json(d2)


def test_derive_source_snapshot_id_stable() -> None:
    c = '{"a":1}'
    assert derive_source_snapshot_id(c) == derive_source_snapshot_id(c)
    assert derive_source_snapshot_id(c).startswith("snap_")
    assert len(derive_source_snapshot_id(c)) == len("snap_") + 32


def test_normalized_sorted_sources_dedupes(tmp_path: Path) -> None:
    p = tmp_path / "x.txt"
    p.write_bytes(b"q")
    out = normalized_sorted_sources([p, p, Path(str(p))])
    assert len(out) == 1


def test_relative_path_for_fingerprint_rejects_outside_anchor(tmp_path: Path) -> None:
    anchor = tmp_path / "repo"
    anchor.mkdir()
    inside = anchor / "data" / "a.bin"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"1")
    assert relative_path_for_fingerprint(inside, anchor) == "data/a.bin"

    outside = tmp_path / "other" / "b.bin"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"2")
    with pytest.raises(ValueError, match="under anchor"):
        relative_path_for_fingerprint(outside, anchor)
