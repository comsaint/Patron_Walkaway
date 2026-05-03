"""LDA-E1-11 §5.3 row 12: preprocess with ingestion registry + full L1 + Gate1 matches no-registry baseline."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from tests.integration.test_lda_e1_10_resume_g7_v1 import (
    _D1,
    _D2,
    _cleanup_snap,
    _collect_l1_fingerprints,
    _run_orchestrator,
    _write_two_day_t_bet_fixture,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_ROOT = _REPO_ROOT / "data"
_REGISTRY = _REPO_ROOT / "schema" / "preprocess_bet_ingestion_fix_registry.yaml"

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_registry_preprocess_fingerprints_match_baseline_and_manifest_has_fix_ids(tmp_path: Path) -> None:
    """Fixture rows keep synthetic observed == raw; L1 fingerprints match baseline; manifest records FIX-004."""
    snap_no = f"snap_e111base_{uuid.uuid4().hex[:16]}"
    snap_reg = f"snap_e111reg_{uuid.uuid4().hex[:16]}"
    fixture = tmp_path / "two_day_t_bet.parquet"
    _write_two_day_t_bet_fixture(fixture)
    gate_no = tmp_path / "gate_no"
    gate_reg = tmp_path / "gate_reg"

    _cleanup_snap(_DATA_ROOT, snap_no)
    _cleanup_snap(_DATA_ROOT, snap_reg)
    try:
        _run_orchestrator(
            ["--gate1-output-parent", str(gate_no)],
            fixture_parquet=fixture,
            snap_id=snap_no,
            cwd=_REPO_ROOT,
        )
        fp_no = _collect_l1_fingerprints(data_root=_DATA_ROOT, snap_id=snap_no, days=(_D1, _D2))
        assert len(fp_no) == 8

        _run_orchestrator(
            [
                "--ingestion-fix-registry-yaml",
                str(_REGISTRY.resolve()),
                "--ingestion-fix-registry-version-expected",
                "v0.4_draft",
                "--gate1-output-parent",
                str(gate_reg),
            ],
            fixture_parquet=fixture,
            snap_id=snap_reg,
            cwd=_REPO_ROOT,
        )
        fp_reg = _collect_l1_fingerprints(data_root=_DATA_ROOT, snap_id=snap_reg, days=(_D1, _D2))
        assert fp_no == fp_reg, f"fingerprint mismatch:\nno-registry={fp_no}\nwith-registry={fp_reg}"

        from layered_data_assets.l1_paths import l1_bet_partition_dir

        man_path = l1_bet_partition_dir(_DATA_ROOT, snap_reg, _D1) / "manifest.json"
        man = json.loads(man_path.read_text(encoding="utf-8"))
        assert man.get("ingestion_fix_rule_id") == "BET-INGEST-FIX-004"
        assert man.get("ingestion_fix_rule_version") == "v1"
        assert "BET-INGEST-FIX-004:v1" in (man.get("applied_fix_rules") or [])
    finally:
        _cleanup_snap(_DATA_ROOT, snap_no)
        _cleanup_snap(_DATA_ROOT, snap_reg)
        for d in (gate_no, gate_reg):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
