"""Unit tests for materialization_state_store_v1 (LDA-E1-09)."""

from pathlib import Path

import pytest

from layered_data_assets.materialization_state_store_v1 import (
    ARTIFACT_PREPROCESS_BET,
    compute_input_hash,
    ensure_materialization_state_schema,
    fetch_state_row,
    hash_preprocess_inputs,
    mark_step_running,
    mark_step_succeeded,
    should_skip_step,
)

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_ensure_schema_and_resume_skip(tmp_path: Path) -> None:
    db = tmp_path / "state.duckdb"
    con = duckdb.connect(str(db))
    try:
        ensure_materialization_state_schema(con)
        inp = tmp_path / "a.parquet"
        inp.write_bytes(b"x")
        h = hash_preprocess_inputs(
            source_snapshot_id="snap_x",
            gaming_day="2026-01-01",
            preprocess_input_paths=[inp],
            fingerprint_path=None,
        )
        att = mark_step_running(
            con,
            artifact_kind=ARTIFACT_PREPROCESS_BET,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
            input_hash=h,
        )
        assert att == 1
        mark_step_succeeded(
            con,
            artifact_kind=ARTIFACT_PREPROCESS_BET,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
            input_hash=h,
            attempt=att,
            output_uri="/tmp/cleaned.parquet",
            row_count=42,
        )
        row = fetch_state_row(
            con,
            artifact_kind=ARTIFACT_PREPROCESS_BET,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
        )
        assert row is not None
        assert row["status"] == "succeeded"
        assert should_skip_step(resume=True, force=False, row=row, input_hash=h) is True
        assert should_skip_step(resume=False, force=False, row=row, input_hash=h) is False
        assert should_skip_step(resume=True, force=True, row=row, input_hash=h) is False
    finally:
        con.close()


def test_compute_input_hash_stable_ordering() -> None:
    a = compute_input_hash({"z": 1, "a": 2})
    b = compute_input_hash({"a": 2, "z": 1})
    assert a == b


def test_hash_preprocess_inputs_changes_when_registry_expected_version_changes(tmp_path: Path) -> None:
    """Optional registry + expected version participate in preprocess input_hash."""
    inp = tmp_path / "a.parquet"
    inp.write_bytes(b"x")
    reg = tmp_path / "registry.yaml"
    reg.write_text("registry_version: test\n", encoding="utf-8")
    h1 = hash_preprocess_inputs(
        source_snapshot_id="snap_x",
        gaming_day="2026-01-01",
        preprocess_input_paths=[inp],
        fingerprint_path=None,
        ingestion_fix_registry_path=reg,
        ingestion_fix_registry_version_expected="v1",
    )
    h2 = hash_preprocess_inputs(
        source_snapshot_id="snap_x",
        gaming_day="2026-01-01",
        preprocess_input_paths=[inp],
        fingerprint_path=None,
        ingestion_fix_registry_path=reg,
        ingestion_fix_registry_version_expected="v2",
    )
    assert h1 != h2
