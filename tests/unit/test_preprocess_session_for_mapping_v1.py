"""Tests for session ingestion fix registry and ``session_for_mapping`` materialization."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pipelines.layered_data_assets.core.preprocess_session_ingestion_fix_registry_v1 import (
    load_preprocess_session_ingestion_fix_registry,
    resolve_session_for_mapping_materialization_contract,
    resolve_session_ingest_fix001_cap_binding,
)

_REPO = Path(__file__).resolve().parents[2]
_SESSION_REG = _REPO / "schema" / "preprocess_l0_data_contract_registry.yaml"


def test_load_session_registry_and_resolve_cap() -> None:
    doc = load_preprocess_session_ingestion_fix_registry(_SESSION_REG)
    cap, fix_id, fix_ver, applied = resolve_session_ingest_fix001_cap_binding(doc)
    assert cap == 636
    assert fix_id == "SESSION-INGEST-FIX-001"
    assert fix_ver == "v1"
    assert applied == ["SESSION-INGEST-FIX-001:v1"]


def test_resolve_session_materialization_contract() -> None:
    doc = load_preprocess_session_ingestion_fix_registry(_SESSION_REG)
    c = resolve_session_for_mapping_materialization_contract(doc)
    assert c.clean_logic_version == "v3-registry-driven-session-clean"
    assert "session_id" in c.required_l0_columns
    assert len(c.episode_calendar_tags) == 3
    assert c.correction_pairing_enabled is True
    assert "is_manual_i64" in c.correction_winner_order_sql


def test_prepare_session_passthrough_when_ingest_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from parallel_lda_mvp import session_for_mapping as sfm

    p = tmp_path / "l0.parquet"
    pd.DataFrame(
        {
            "session_id": [1],
            "session_end_dtm": pd.Timestamp("2025-06-01 12:00:00+00:00"),
            "__etl_insert_Dtm": pd.Timestamp("2025-06-01 20:00:00+00:00"),
        }
    ).to_parquet(p, index=False)
    monkeypatch.setenv("PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE", "1")
    monkeypatch.delenv("PARALLEL_LDA_MVP_SESSION_INGEST_REGISTRY", raising=False)
    assert sfm.prepare_session_parquet_for_canonical_mapping(p).resolve() == p.resolve()


def test_prepare_session_materializes_synthetic_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("duckdb")
    import duckdb

    from parallel_lda_mvp import session_for_mapping as sfm

    staging = tmp_path / "staging"

    def _staging() -> Path:
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    monkeypatch.setattr(sfm, "_session_mapping_staging_dir", _staging)
    monkeypatch.setenv("PARALLEL_LDA_MVP_SESSION_INGEST_REGISTRY", str(_SESSION_REG.resolve()))
    monkeypatch.delenv("PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE", raising=False)

    p = tmp_path / "l0_session.parquet"
    pd.DataFrame(
        {
            "session_id": [1],
            "session_end_dtm": pd.Timestamp("2025-06-01 12:00:00+00:00"),
            "session_start_dtm": pd.Timestamp("2025-06-01 11:00:00+00:00"),
            "player_id": [1],
            "is_manual": [0],
            "lud_dtm": pd.Timestamp("2025-06-01 20:00:00+00:00"),
            "__etl_insert_Dtm": pd.Timestamp("2025-06-01 20:00:00+00:00"),
        }
    ).to_parquet(p, index=False)

    out = sfm.prepare_session_parquet_for_canonical_mapping(p)
    assert out.resolve() != p.resolve()
    con = duckdb.connect()
    try:
        delta = con.execute(
            """
            SELECT
              EXTRACT(EPOCH FROM TRY_CAST(__etl_insert_Dtm_synthetic AS TIMESTAMP))
              - EXTRACT(EPOCH FROM TRY_CAST(session_end_dtm AS TIMESTAMP)) AS sec_after_end
            FROM read_parquet(?)
            """,
            [str(out.resolve())],
        ).fetchone()
    finally:
        con.close()
    assert delta is not None
    assert float(delta[0]) == pytest.approx(636.0, abs=0.01)


def test_prepare_session_preserves_early_observed_before_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("duckdb")
    import duckdb

    from parallel_lda_mvp import session_for_mapping as sfm

    staging = tmp_path / "staging"

    def _staging() -> Path:
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    monkeypatch.setattr(sfm, "_session_mapping_staging_dir", _staging)
    monkeypatch.setenv("PARALLEL_LDA_MVP_SESSION_INGEST_REGISTRY", str(_SESSION_REG.resolve()))
    monkeypatch.delenv("PARALLEL_LDA_MVP_SESSION_INGEST_DISABLE", raising=False)

    p = tmp_path / "l0_session_early.parquet"
    pd.DataFrame(
        {
            "session_id": [1],
            "session_end_dtm": pd.Timestamp("2025-06-01 12:00:00+00:00"),
            "session_start_dtm": pd.Timestamp("2025-06-01 11:00:00+00:00"),
            "player_id": [1],
            "is_manual": [0],
            "lud_dtm": pd.Timestamp("2025-06-01 12:05:00+00:00"),
            "__etl_insert_Dtm": pd.Timestamp("2025-06-01 11:55:00+00:00"),
        }
    ).to_parquet(p, index=False)

    out = sfm.prepare_session_parquet_for_canonical_mapping(p)
    con = duckdb.connect()
    try:
        row = con.execute(
            """
            SELECT
              EXTRACT(EPOCH FROM TRY_CAST(__etl_insert_Dtm_synthetic AS TIMESTAMP))
              - EXTRACT(EPOCH FROM TRY_CAST(__etl_insert_Dtm AS TIMESTAMP)) AS synthetic_minus_raw_sec
            FROM read_parquet(?)
            """,
            [str(out.resolve())],
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    assert float(row[0]) == pytest.approx(0.0, abs=0.01)
