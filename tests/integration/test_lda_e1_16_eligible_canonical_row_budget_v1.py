"""LDA-E1-16: cutoff session row-budget before canonical trainer path + JSONL precount event."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCH = _REPO_ROOT / "scripts" / "lda_l1_gate1_day_range_v1.py"
_D_BET = "2099-08-01"
_CUTOFF = "2099-12-31T23:59:59"

from pipelines.layered_data_assets.cli import lda_l1_gate1_day_range_v1 as lda_mod

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]


def _fixture_dir() -> Path:
    d = _REPO_ROOT / ".tmp" / f"lda_e116_{uuid.uuid4().hex[:12]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_min_bet_parquet(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '{_D_BET}',
                 TIMESTAMP '{_D_BET} 10:00:00', TIMESTAMP '{_D_BET} 11:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{path.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()


def _write_t_session_n_rows(path: Path, n: int) -> None:
    """Minimal ``t_session`` rows (canonical trainer schema subset)."""
    if n < 1:
        raise ValueError("n must be >= 1")
    path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    for i in range(n):
        sid = 9001 + i
        parts.append(
            f"({sid}::BIGINT, 100::BIGINT, 'CP100'::VARCHAR, "
            f"TIMESTAMP '{_D_BET} 09:00:00', TIMESTAMP '{_D_BET} 09:30:00', TIMESTAMP '{_D_BET} 10:00:00', "
            f"0::INTEGER, 0::INTEGER, 0::INTEGER, 2::INTEGER, 50.0::DOUBLE)"
        )
    values_sql = ",\n".join(parts)
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                {values_sql}
              ) AS t(
                session_id, player_id, casino_player_id,
                lud_dtm, session_start_dtm, session_end_dtm,
                is_manual, is_deleted, is_canceled, num_games_with_wager, turnover
              )
            ) TO '{path.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()


def _run_orch(argv_extra: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(_ORCH), "--no-progress", *argv_extra]
    return subprocess.run(cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_canonical_trainer_precount_exit_2_when_session_rows_exceed_budget() -> None:
    """Missing ``--canonical-mapping-parquet`` + trainer build must honor ``--eligible-build-max-session-rows``."""
    fix = _fixture_dir()
    bet_pq = fix / "t_bet.parquet"
    sess_pq = fix / "t_session.parquet"
    missing_cm = fix / "canonical_to_build.parquet"
    _write_min_bet_parquet(bet_pq)
    _write_t_session_n_rows(sess_pq, 3)
    try:
        proc = _run_orch(
            [
                "--date-from",
                _D_BET,
                "--date-to",
                _D_BET,
                "--bet-parquet",
                str(bet_pq.resolve()),
                "--source-snapshot-id",
                "snap_e116_budget",
                "--raw-t-session-parquet",
                str(sess_pq.resolve()),
                "--cutoff-dtm",
                _CUTOFF,
                "--canonical-mapping-parquet",
                str(missing_cm.resolve()),
                "--eligible-build-max-session-rows",
                "2",
            ]
        )
        assert proc.returncode == 2, (proc.stderr or "")[-6000:]
        err = proc.stderr or ""
        assert "--eligible-build-max-session-rows" in err
    finally:
        if fix.is_dir():
            shutil.rmtree(fix, ignore_errors=True)


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_build_canonical_mapping_via_trainer_writes_precount_run_log(tmp_path: Path) -> None:
    """Trainer canonical build appends ``canonical_mapping_precount`` before DuckDB links materialize."""
    sess_pq = tmp_path / "t_session.parquet"
    out_cm = tmp_path / "canonical_mapping.parquet"
    side = tmp_path / "canonical_mapping.cutoff.json"
    log = tmp_path / "eligible_build.jsonl"
    _write_t_session_n_rows(sess_pq, 2)
    cutoff = datetime.fromisoformat(_CUTOFF)
    lda_mod._build_canonical_mapping_parquet_via_trainer(
        raw_t_session_parquet=sess_pq,
        cutoff_dtm=cutoff,
        canonical_mapping_parquet=out_cm,
        sidecar_json=side,
        data_root=tmp_path,
        max_session_rows=10_000_000,
        duckdb_threads=1,
        run_log_path=log,
    )
    assert out_cm.is_file()
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]
    kinds = {e.get("event") for e in events}
    assert "canonical_mapping_precount" in kinds
    assert "canonical_mapping_build_done" in kinds
