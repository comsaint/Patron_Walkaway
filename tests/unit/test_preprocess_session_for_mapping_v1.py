"""Tests for session ingestion fix registry and ``session_for_mapping`` materialization."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pipelines.layered_data_assets.core.preprocess_session_ingestion_fix_registry_v1 import (
    load_preprocess_session_ingestion_fix_registry,
    resolve_session_ingest_fix001_cap_binding,
)

_REPO = Path(__file__).resolve().parents[2]
_SESSION_REG = _REPO / "schema" / "preprocess_ingestion_fix_registry.yaml"


def test_load_session_registry_and_resolve_cap() -> None:
    doc = load_preprocess_session_ingestion_fix_registry(_SESSION_REG)
    cap, fix_id, fix_ver, applied = resolve_session_ingest_fix001_cap_binding(doc)
    assert cap == 636
    assert fix_id == "SESSION-INGEST-FIX-001"
    assert fix_ver == "v1"
    assert applied == ["SESSION-INGEST-FIX-001:v1"]


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
