"""LDA-E1-11 §5.3 row 12: orchestrator always uses ingestion registry; manifest records FIX-004."""

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

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_orchestrator_always_applies_registry_manifest_has_fix_ids(tmp_path: Path) -> None:
    """Default run uses canonical registry; L1 stack completes; preprocess manifest records FIX-004."""
    snap_id = f"snap_e111reg_{uuid.uuid4().hex[:16]}"
    fixture = tmp_path / "two_day_t_bet.parquet"
    _write_two_day_t_bet_fixture(fixture)
    gate_out = tmp_path / "gate_out"

    _cleanup_snap(_DATA_ROOT, snap_id)
    try:
        _run_orchestrator(
            [
                "--ingestion-fix-registry-version-expected",
                "v0.4_draft",
                "--gate1-output-parent",
                str(gate_out),
            ],
            fixture_parquet=fixture,
            snap_id=snap_id,
            cwd=_REPO_ROOT,
        )
        fp = _collect_l1_fingerprints(data_root=_DATA_ROOT, snap_id=snap_id, days=(_D1, _D2))
        assert len(fp) == 8, f"expected 8 fingerprints, got {sorted(fp.keys())}"

        from layered_data_assets.l1_paths import l1_bet_partition_dir

        man_path = l1_bet_partition_dir(_DATA_ROOT, snap_id, _D1) / "manifest.json"
        man = json.loads(man_path.read_text(encoding="utf-8"))
        assert man.get("ingestion_fix_rule_id") == "BET-INGEST-FIX-004"
        assert man.get("ingestion_fix_rule_version") == "v1"
        assert "BET-INGEST-FIX-004:v1" in (man.get("applied_fix_rules") or [])
    finally:
        _cleanup_snap(_DATA_ROOT, snap_id)
        if gate_out.is_dir():
            shutil.rmtree(gate_out, ignore_errors=True)
