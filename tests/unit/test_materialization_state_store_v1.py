"""Unit tests for materialization_state_store_v1 (LDA-E1-09)."""

from pathlib import Path

import pytest

from layered_data_assets.materialization_state_store_v1 import (
    ARTIFACT_PREPROCESS_BET,
    ARTIFACT_RUN_DAY_BRIDGE,
    compute_input_hash,
    compute_output_row_fingerprint_pair,
    ensure_materialization_state_schema,
    fetch_state_row,
    format_row_fingerprint_tag,
    hash_preprocess_inputs,
    mark_step_running,
    mark_step_succeeded,
    patch_run_day_bridge_recompute_metrics,
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


def test_hash_preprocess_inputs_changes_when_eligible_file_changes(tmp_path: Path) -> None:
    """Eligible parquet content hash must participate in preprocess input_hash."""
    inp = tmp_path / "a.parquet"
    inp.write_bytes(b"x")
    eligible = tmp_path / "eligible.parquet"
    eligible.write_bytes(b"p1")
    h1 = hash_preprocess_inputs(
        source_snapshot_id="snap_x",
        gaming_day="2026-01-01",
        preprocess_input_paths=[inp],
        fingerprint_path=None,
        eligible_player_ids_parquet=eligible,
    )
    eligible.write_bytes(b"p12")
    h2 = hash_preprocess_inputs(
        source_snapshot_id="snap_x",
        gaming_day="2026-01-01",
        preprocess_input_paths=[inp],
        fingerprint_path=None,
        eligible_player_ids_parquet=eligible,
    )
    assert h1 != h2


def test_hash_preprocess_inputs_same_size_different_bytes(tmp_path: Path) -> None:
    """Content-based hash must distinguish equal-length payloads (stat size alone would not)."""
    inp = tmp_path / "a.parquet"
    inp.write_bytes(b"12")
    h1 = hash_preprocess_inputs(
        source_snapshot_id="snap_x",
        gaming_day="2026-01-01",
        preprocess_input_paths=[inp],
        fingerprint_path=None,
    )
    inp.write_bytes(b"34")
    h2 = hash_preprocess_inputs(
        source_snapshot_id="snap_x",
        gaming_day="2026-01-01",
        preprocess_input_paths=[inp],
        fingerprint_path=None,
    )
    assert h1 != h2


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_compute_output_row_fingerprint_pair_preprocess_stable(tmp_path: Path) -> None:
    pq = tmp_path / "cleaned.parquet"
    con = duckdb.connect()
    try:
        con.execute(
            """
            COPY (
              SELECT
                CAST(1 AS BIGINT) AS bet_id,
                CAST('p1' AS VARCHAR) AS player_id,
                CAST('2026-01-01' AS VARCHAR) AS gaming_day,
                CAST(NULL AS TIMESTAMP) AS payout_complete_dtm,
                CAST(TIMESTAMP '2020-01-01' AS TIMESTAMP) AS __etl_insert_Dtm,
                CAST(0 AS INTEGER) AS is_deleted,
                CAST(0 AS INTEGER) AS is_canceled,
                CAST(0 AS INTEGER) AS is_manual
            ) TO ? (FORMAT PARQUET)
            """,
            [str(pq.resolve())],
        )
        n1, h1 = compute_output_row_fingerprint_pair(
            con,
            parquet_path=pq,
            fingerprint_artifact_kind=ARTIFACT_PREPROCESS_BET,
        )
        n2, h2 = compute_output_row_fingerprint_pair(
            con,
            parquet_path=pq,
            fingerprint_artifact_kind=ARTIFACT_PREPROCESS_BET,
        )
        assert n1 == n2 == 1
        assert h1 == h2
        t1 = format_row_fingerprint_tag(artifact_kind=ARTIFACT_PREPROCESS_BET, fp_value=str(h1))
        t2 = format_row_fingerprint_tag(artifact_kind=ARTIFACT_PREPROCESS_BET, fp_value=str(h2))
        assert t1 == t2
    finally:
        con.close()


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_patch_run_day_bridge_recompute_metrics(tmp_path: Path) -> None:
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
            artifact_kind=ARTIFACT_RUN_DAY_BRIDGE,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
            input_hash=h,
        )
        mark_step_succeeded(
            con,
            artifact_kind=ARTIFACT_RUN_DAY_BRIDGE,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
            input_hash=h,
            attempt=att,
            output_uri="/tmp/x.parquet",
            row_count=1,
            row_hash="v1|run_day_bridge|abc",
        )
        patch_run_day_bridge_recompute_metrics(
            con,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
            recompute_rounds=2,
            recompute_stop_reason="fixed_point",
        )
        row = fetch_state_row(
            con,
            artifact_kind=ARTIFACT_RUN_DAY_BRIDGE,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
        )
        assert row is not None
        assert int(row["recompute_rounds"]) == 2
        assert str(row["recompute_stop_reason"]) == "fixed_point"
    finally:
        con.close()


def test_impacted_set_parse_and_filter() -> None:
    from pipelines.layered_data_assets.cli.lda_l1_gate1_day_range_v1 import (
        RECOMPUTE_STOP_FALLBACK_FULL,
        _filter_days_with_impacted_set,
        _parse_impacted_set_payload,
    )

    ids, cds = _parse_impacted_set_payload(
        {"impacted_entities": [{"canonical_id": "c1"}, "c2"], "candidate_days": ["2026-01-02", "2026-01-01"]}
    )
    assert ids == ["c1", "c2"]
    assert cds == ["2026-01-02", "2026-01-01"]
    days, n_e, n_d, fb = _filter_days_with_impacted_set(
        ["2026-01-01", "2026-01-02", "2026-01-03"],
        impacted_canonical_ids=ids,
        candidate_days=cds,
        max_impacted_entities=10,
        emit_stderr=None,
    )
    assert fb is None
    assert days == ["2026-01-01", "2026-01-02"]
    assert n_e == 2
    assert n_d == 2
    _d2, _n2, _n2d, fb2 = _filter_days_with_impacted_set(
        ["2026-01-01"],
        impacted_canonical_ids=["a", "b"],
        candidate_days=[],
        max_impacted_entities=1,
        emit_stderr=None,
    )
    assert fb2 == RECOMPUTE_STOP_FALLBACK_FULL
    assert _d2 == ["2026-01-01"]


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_collect_run_day_bridge_signatures(tmp_path: Path) -> None:
    from pipelines.layered_data_assets.cli.lda_l1_gate1_day_range_v1 import (
        _collect_run_day_bridge_signatures,
    )

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
            artifact_kind=ARTIFACT_RUN_DAY_BRIDGE,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
            input_hash=h,
        )
        mark_step_succeeded(
            con,
            artifact_kind=ARTIFACT_RUN_DAY_BRIDGE,
            gaming_day="2026-01-01",
            source_snapshot_id="snap_x",
            input_hash=h,
            attempt=att,
            output_uri="/tmp/x.parquet",
            row_count=1,
            row_hash="v1|run_day_bridge|deadbeef",
        )
        sig = _collect_run_day_bridge_signatures(con, ["2026-01-01"])
        assert sig == (("2026-01-01", "snap_x", "v1|run_day_bridge|deadbeef"),)
    finally:
        con.close()
