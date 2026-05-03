"""Unit tests for atomic_parquet_manifest_v1."""

from __future__ import annotations

from pathlib import Path

import pytest

from layered_data_assets.atomic_parquet_manifest_v1 import (
    commit_parquet_and_manifest,
    staged_manifest_path,
    staged_parquet_path,
)

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_commit_parquet_and_manifest_replaces_atomically(tmp_path: Path) -> None:
    final_p = tmp_path / "out" / "run_fact.parquet"
    final_m = tmp_path / "out" / "manifest.json"
    staged_p = staged_parquet_path(final_p)
    staged_m = staged_manifest_path(final_m)
    staged_p.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"COPY (SELECT 1::INTEGER AS x) TO '{staged_p.as_posix()}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    commit_parquet_and_manifest(
        staged_parquet=staged_p,
        final_parquet=final_p,
        manifest_text='{"ok": true}\n',
        final_manifest=final_m,
    )
    assert final_p.is_file()
    assert final_m.read_text(encoding="utf-8") == '{"ok": true}\n'
    assert not staged_p.exists()
    assert not staged_m.exists()
