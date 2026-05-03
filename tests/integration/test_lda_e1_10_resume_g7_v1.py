"""LDA-E1-10 / G7: one-shot vs stop-after-date + --resume produce identical L1 fingerprints."""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_ROOT = _REPO_ROOT / "data"
_ORCH = _REPO_ROOT / "scripts" / "lda_l1_gate1_day_range_v1.py"

_D1 = "2099-06-01"
_D2 = "2099-06-02"

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]


def _write_two_day_t_bet_fixture(path: Path) -> None:
    """Write a small ``t_bet``-shaped Parquet (two ``gaming_day`` values)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '{_D1}',
                 TIMESTAMP '{_D1} 10:00:00', TIMESTAMP '{_D1} 11:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER),
                (2::BIGINT, 100::BIGINT, DATE '{_D1}',
                 TIMESTAMP '{_D1} 10:15:00', TIMESTAMP '{_D1} 11:15:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER),
                (3::BIGINT, 100::BIGINT, DATE '{_D2}',
                 TIMESTAMP '{_D2} 09:00:00', TIMESTAMP '{_D2} 10:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER),
                (4::BIGINT, 100::BIGINT, DATE '{_D2}',
                 TIMESTAMP '{_D2} 09:30:00', TIMESTAMP '{_D2} 10:30:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{path.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()


def _collect_l1_fingerprints(*, data_root: Path, snap_id: str, days: tuple[str, ...]) -> dict[tuple[str, str], tuple[int, str]]:
    """Return ``(artifact, day) -> (row_count, sha256_hex)`` for preprocess + three L1 parquets."""
    from layered_data_assets.l1_determinism_gate_v1 import (
        cleaned_bet_parquet_row_fingerprint,
        run_bet_map_parquet_row_fingerprint,
        run_day_bridge_parquet_row_fingerprint,
        run_fact_parquet_row_fingerprint,
    )
    from layered_data_assets.l1_paths import (
        l1_bet_cleaned_parquet_path,
        l1_run_bet_map_partition_dir,
        l1_run_day_bridge_partition_dir,
        l1_run_fact_partition_dir,
    )

    con = duckdb.connect(database=":memory:")
    try:
        out: dict[tuple[str, str], tuple[int, str]] = {}
        for d in days:
            cleaned = l1_bet_cleaned_parquet_path(data_root, snap_id, d)
            if cleaned.is_file():
                out[("preprocess_bet", d)] = cleaned_bet_parquet_row_fingerprint(con, cleaned)
            rf = l1_run_fact_partition_dir(data_root, snap_id, d) / "run_fact.parquet"
            if rf.is_file():
                out[("run_fact", d)] = run_fact_parquet_row_fingerprint(con, rf)
            bm = l1_run_bet_map_partition_dir(data_root, snap_id, d) / "run_bet_map.parquet"
            if bm.is_file():
                out[("run_bet_map", d)] = run_bet_map_parquet_row_fingerprint(con, bm)
            br = l1_run_day_bridge_partition_dir(data_root, snap_id, d) / "run_day_bridge.parquet"
            if br.is_file():
                out[("run_day_bridge", d)] = run_day_bridge_parquet_row_fingerprint(con, br)
        return out
    finally:
        con.close()


def _run_orchestrator(
    argv_extra: list[str],
    *,
    fixture_parquet: Path,
    snap_id: str,
    cwd: Path,
) -> None:
    """Invoke ``lda_l1_gate1_day_range_v1``; raise on non-zero exit."""
    cmd = [
        sys.executable,
        str(_ORCH),
        "--date-from",
        _D1,
        "--date-to",
        _D2,
        "--bet-parquet",
        str(fixture_parquet.resolve()),
        "--source-snapshot-id",
        snap_id,
        "--no-progress",
        *argv_extra,
    ]
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-8000:]
        raise AssertionError(
            f"orchestrator exit {proc.returncode}\nSTDERR tail:\n{tail}\nSTDOUT:\n{(proc.stdout or '')[-4000:]}"
        )


def _cleanup_snap(data_root: Path, snap_id: str) -> None:
    root = data_root / "l1_layered" / snap_id
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_g7_one_shot_matches_stop_then_resume(tmp_path: Path) -> None:
    """Path A: full range in one process. Path B: ``--stop-after-date`` day1 then ``--resume`` full range.

    Fingerprints (preprocess + three L1 parquets) for both days must match.
    """
    snap_id = f"snap_e110g7_{uuid.uuid4().hex[:16]}"
    fixture = tmp_path / "two_day_t_bet.parquet"
    _write_two_day_t_bet_fixture(fixture)

    state_a = tmp_path / "state_a.duckdb"
    state_b = tmp_path / "state_b.duckdb"
    gate_a = tmp_path / "gate_a"
    gate_b1 = tmp_path / "gate_b1"
    gate_b2 = tmp_path / "gate_b2"

    _cleanup_snap(_DATA_ROOT, snap_id)
    try:
        _run_orchestrator(
            [
                "--state-store",
                str(state_a),
                "--gate1-output-parent",
                str(gate_a),
            ],
            fixture_parquet=fixture,
            snap_id=snap_id,
            cwd=_REPO_ROOT,
        )
        fp_a = _collect_l1_fingerprints(data_root=_DATA_ROOT, snap_id=snap_id, days=(_D1, _D2))
        assert len(fp_a) == 8, f"expected 8 fingerprints (4 artifacts x 2 days), got {sorted(fp_a.keys())}"

        shutil.rmtree(_DATA_ROOT / "l1_layered" / snap_id, ignore_errors=True)
        if state_b.is_file():
            state_b.unlink()

        _run_orchestrator(
            [
                "--state-store",
                str(state_b),
                "--stop-after-date",
                _D1,
                "--gate1-output-parent",
                str(gate_b1),
            ],
            fixture_parquet=fixture,
            snap_id=snap_id,
            cwd=_REPO_ROOT,
        )
        _run_orchestrator(
            [
                "--state-store",
                str(state_b),
                "--resume",
                "--gate1-output-parent",
                str(gate_b2),
            ],
            fixture_parquet=fixture,
            snap_id=snap_id,
            cwd=_REPO_ROOT,
        )
        fp_b = _collect_l1_fingerprints(data_root=_DATA_ROOT, snap_id=snap_id, days=(_D1, _D2))
        assert fp_a == fp_b, f"fingerprint mismatch:\nA={fp_a}\nB={fp_b}"
    finally:
        _cleanup_snap(_DATA_ROOT, snap_id)
        for p in (state_a, state_b):
            if Path(p).is_file():
                Path(p).unlink(missing_ok=True)
        for d in (gate_a, gate_b1, gate_b2):
            if Path(d).is_dir():
                shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_cleaned_bet_fingerprint_roundtrip(tmp_path: Path) -> None:
    """Sanity: ``cleaned_bet_parquet_row_fingerprint`` runs on a tiny Parquet."""
    from layered_data_assets.l1_determinism_gate_v1 import cleaned_bet_parquet_row_fingerprint

    p = tmp_path / "one.parquet"
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '{_D1}',
                 TIMESTAMP '{_D1} 10:00:00', TIMESTAMP '{_D1} 11:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{p.as_posix()}' (FORMAT PARQUET)
            """
        )
        n, h = cleaned_bet_parquet_row_fingerprint(con, p)
        assert n == 1
        assert len(h) == 64
    finally:
        con.close()
