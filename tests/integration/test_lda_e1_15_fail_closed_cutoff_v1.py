"""LDA-E1-15: BET-DQ-03 fail-closed argv validation and cutoff contract (exit 2, no silent pass)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCH = _REPO_ROOT / "scripts" / "lda_l1_gate1_day_range_v1.py"
_D = "2099-07-01"

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]


def _run_orch(argv_extra: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(_ORCH), "--no-progress", *argv_extra]
    return subprocess.run(cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=60)


def _write_min_bet_parquet(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '{_D}',
                 TIMESTAMP '{_D} 10:00:00', TIMESTAMP '{_D} 11:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{path.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()


def _fixture_dir() -> Path:
    d = _REPO_ROOT / ".tmp" / f"lda_e115_{uuid.uuid4().hex[:12]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_raw_mode_exit_2_without_rated_allowlist_source() -> None:
    """Raw ``t_bet`` without session / canonical / explicit eligible must exit 2 (fail-closed)."""
    fake_bet = _REPO_ROOT / ".tmp" / f"lda_e115_norate_{uuid.uuid4().hex[:8]}.parquet"
    fake_bet.parent.mkdir(parents=True, exist_ok=True)
    fake_bet.write_bytes(b"")
    try:
        proc = _run_orch(
            [
                "--date-from",
                _D,
                "--date-to",
                _D,
                "--raw-t-bet-parquet",
                str(fake_bet.resolve()),
            ]
        )
        assert proc.returncode == 2, (proc.stderr or "")[-4000:]
        err = proc.stderr or ""
        assert "raw mode requires BET-DQ-03 allowlist" in err
    finally:
        fake_bet.unlink(missing_ok=True)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_raw_mode_exit_2_session_without_cutoff() -> None:
    """``--raw-t-session-parquet`` in raw mode requires ``--cutoff-dtm`` when no other rated source."""
    fix = _fixture_dir()
    bet_pq = fix / "t_bet.parquet"
    sess_touch = fix / "t_session.parquet"
    _write_min_bet_parquet(bet_pq)
    sess_touch.write_bytes(b"")
    try:
        proc = _run_orch(
            [
                "--date-from",
                _D,
                "--date-to",
                _D,
                "--raw-t-bet-parquet",
                str(bet_pq.resolve()),
                "--raw-t-session-parquet",
                str(sess_touch.resolve()),
            ]
        )
        assert proc.returncode == 2, (proc.stderr or "")[-4000:]
        assert "cutoff-dtm is required with --raw-t-session-parquet" in (proc.stderr or "")
    finally:
        if fix.is_dir():
            shutil.rmtree(fix, ignore_errors=True)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_raw_mode_exit_2_invalid_cutoff_iso() -> None:
    """Invalid ``--cutoff-dtm`` must exit 2 before pipeline."""
    fix = _fixture_dir()
    bet_pq = fix / "t_bet.parquet"
    sess_pq = fix / "t_session.parquet"
    _write_min_bet_parquet(bet_pq)
    sess_pq.write_bytes(b"")
    try:
        proc = _run_orch(
            [
                "--date-from",
                _D,
                "--date-to",
                _D,
                "--raw-t-bet-parquet",
                str(bet_pq.resolve()),
                "--raw-t-session-parquet",
                str(sess_pq.resolve()),
                "--cutoff-dtm",
                "not-an-iso-datetime",
            ]
        )
        assert proc.returncode == 2, (proc.stderr or "")[-4000:]
        err = proc.stderr or ""
        assert "Invalid --cutoff-dtm" in err
    finally:
        if fix.is_dir():
            shutil.rmtree(fix, ignore_errors=True)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_bet_parquet_with_session_requires_cutoff_or_allowlist_exit_2() -> None:
    """``--bet-parquet`` + ``--raw-t-session-parquet`` without cutoff/eligible/canonical must exit 2."""
    fix = _fixture_dir()
    bet_pq = fix / "t_bet.parquet"
    sess_pq = fix / "t_session.parquet"
    _write_min_bet_parquet(bet_pq)
    sess_pq.write_bytes(b"")
    try:
        proc = _run_orch(
            [
                "--date-from",
                _D,
                "--date-to",
                _D,
                "--bet-parquet",
                str(bet_pq.resolve()),
                "--source-snapshot-id",
                "snap_e115_gate",
                "--raw-t-session-parquet",
                str(sess_pq.resolve()),
            ]
        )
        assert proc.returncode == 2, (proc.stderr or "")[-4000:]
        err = proc.stderr or ""
        assert "With --bet-parquet and --raw-t-session-parquet" in err
        assert "cutoff-dtm" in err.lower()
    finally:
        if fix.is_dir():
            shutil.rmtree(fix, ignore_errors=True)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_canonical_mapping_missing_without_session_or_cutoff_exit_2() -> None:
    """Explicit ``--canonical-mapping-parquet`` missing on disk needs session+cutoff to build."""
    fix = _fixture_dir()
    bet_pq = fix / "t_bet.parquet"
    missing_cm = fix / "no_such_canonical_mapping.parquet"
    _write_min_bet_parquet(bet_pq)
    try:
        proc = _run_orch(
            [
                "--date-from",
                _D,
                "--date-to",
                _D,
                "--raw-t-bet-parquet",
                str(bet_pq.resolve()),
                "--canonical-mapping-parquet",
                str(missing_cm.resolve()),
            ]
        )
        assert proc.returncode == 2, (proc.stderr or "")[-4000:]
        assert "--canonical-mapping-parquet not found" in (proc.stderr or "")
    finally:
        if fix.is_dir():
            shutil.rmtree(fix, ignore_errors=True)
