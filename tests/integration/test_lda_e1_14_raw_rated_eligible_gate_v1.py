"""LDA-E1-14: raw + session + cutoff auto-materialize BET-DQ-03 eligible and pass to preprocess."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Iterable

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_ROOT = _REPO_ROOT / "data"
_ORCH = _REPO_ROOT / "scripts" / "lda_l1_gate1_day_range_v1.py"

_D = "2099-06-01"
_CUTOFF = "2099-12-31T23:59:59"

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore[misc, assignment]


def _write_one_day_t_bet_fixture(path: Path) -> None:
    """Minimal ``t_bet`` Parquet for one ``gaming_day`` (L0 + preprocess)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (1::BIGINT, 100::BIGINT, DATE '{_D}',
                 TIMESTAMP '{_D} 10:00:00', TIMESTAMP '{_D} 11:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER),
                (2::BIGINT, 100::BIGINT, DATE '{_D}',
                 TIMESTAMP '{_D} 10:15:00', TIMESTAMP '{_D} 11:15:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER)
              ) AS t(bet_id, player_id, gaming_day, payout_complete_dtm, __etl_insert_Dtm,
                     is_deleted, is_canceled, is_manual)
            ) TO '{path.as_posix()}' (FORMAT PARQUET)
            """
        )
    finally:
        con.close()


def _write_t_session_rated_fixture(path: Path) -> None:
    """Two sessions for same ``player_id`` (avoid FND-12 single-session dummy) with canonical columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(
            f"""
            COPY (
              SELECT * FROM (VALUES
                (9001::BIGINT, 100::BIGINT, 'CP100'::VARCHAR,
                 TIMESTAMP '{_D} 09:00:00', TIMESTAMP '{_D} 09:30:00', TIMESTAMP '{_D} 10:00:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER, 2::INTEGER, 50.0::DOUBLE),
                (9002::BIGINT, 100::BIGINT, 'CP100'::VARCHAR,
                 TIMESTAMP '{_D} 10:30:00', TIMESTAMP '{_D} 11:00:00', TIMESTAMP '{_D} 11:30:00',
                 0::INTEGER, 0::INTEGER, 0::INTEGER, 1::INTEGER, 40.0::DOUBLE)
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


def _snapshot_ids_from_orch_output(blob: str) -> list[str]:
    """Parse ``OK snapshot_id=...`` lines from merged orchestrator output."""
    return list(dict.fromkeys(re.findall(r"OK snapshot_id=(\S+)", blob)))


def _backup_file(src: Path, backup_dir: Path) -> Path | None:
    if not src.is_file():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / src.name
    shutil.copy2(src, dst)
    return dst


def _cleanup_l0_l1_snaps(data_root: Path, snap_ids: Iterable[str]) -> None:
    for sid in snap_ids:
        for base in (data_root / "l0_layered", data_root / "l1_layered"):
            p = base / sid
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)


def _restore_or_remove_cutoff_json(
    *,
    target: Path,
    backup: Path | None,
) -> None:
    if backup is not None:
        shutil.copy2(backup, target)
        backup.unlink(missing_ok=True)
        return
    if target.is_file():
        target.unlink()


@pytest.mark.skipif(duckdb is None, reason="duckdb not installed")
def test_raw_mode_echoes_preprocess_with_eligible_player_ids_parquet(tmp_path: Path) -> None:
    """Trainer path builds mapping + eligible; preprocess argv includes ``--eligible-player-ids-parquet``."""
    # l0_ingest anchor is repo root; sources must live under _REPO_ROOT (not pytest's AppData tmp).
    fix_dir = _REPO_ROOT / ".tmp" / f"lda_e114_{uuid.uuid4().hex[:12]}"
    fix_dir.mkdir(parents=True, exist_ok=True)
    bet_pq = fix_dir / "raw_t_bet.parquet"
    sess_pq = fix_dir / "raw_t_session.parquet"
    cm_pq = fix_dir / "e114_only_canonical_mapping.parquet"
    gate_out = tmp_path / "gate_e114"
    backup_dir = fix_dir / "cutoff_json_backup"
    cutoff_sidecar = _DATA_ROOT / "canonical_mapping.cutoff.json"
    prev_cutoff_backup = _backup_file(cutoff_sidecar, backup_dir)

    _write_one_day_t_bet_fixture(bet_pq)
    _write_t_session_rated_fixture(sess_pq)
    if cm_pq.is_file():
        cm_pq.unlink()

    snap_ids: list[str] = []

    cmd = [
        sys.executable,
        str(_ORCH),
        "--date-from",
        _D,
        "--date-to",
        _D,
        "--raw-t-bet-parquet",
        str(bet_pq.resolve()),
        "--raw-t-session-parquet",
        str(sess_pq.resolve()),
        "--cutoff-dtm",
        _CUTOFF,
        "--canonical-mapping-parquet",
        str(cm_pq.resolve()),
        "--gate1-output-parent",
        str(gate_out.resolve()),
        "--echo-commands",
        "--no-progress",
    ]
    proc = subprocess.run(cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=600)
    merged = (proc.stderr or "") + "\n" + (proc.stdout or "")
    try:
        assert proc.returncode == 0, merged[-12000:]
        assert "[LDA] BET-DQ-03 eligible ids:" in (proc.stderr or ""), "banner must list eligible parquet path"
        assert "preprocess_bet_v1.py" in merged, "expected preprocess subprocess"
        found_eligible_argv = False
        for line in merged.splitlines():
            if "preprocess_bet_v1.py" in line and "--eligible-player-ids-parquet" in line:
                found_eligible_argv = True
                break
        assert found_eligible_argv, (
            "E1-14: preprocess must receive --eligible-player-ids-parquet when raw+t_session+cutoff "
            f"(merged tail):\n{merged[-8000:]}"
        )
        snap_ids = _snapshot_ids_from_orch_output(merged)
        assert snap_ids, "expected at least one OK snapshot_id= from L0 ingest"
    finally:
        _cleanup_l0_l1_snaps(_DATA_ROOT, snap_ids)
        if gate_out.is_dir():
            shutil.rmtree(gate_out, ignore_errors=True)
        _restore_or_remove_cutoff_json(target=cutoff_sidecar, backup=prev_cutoff_backup)
        if fix_dir.is_dir():
            shutil.rmtree(fix_dir, ignore_errors=True)
