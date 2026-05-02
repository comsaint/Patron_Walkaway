"""CLI smoke tests for scripts/l0_ingest.py (small temp files only)."""

import json
import subprocess
import sys
from pathlib import Path


def _run_ingest(tmp_path: Path, extra: list[str]) -> subprocess.CompletedProcess[str]:
    repo = Path(__file__).resolve().parents[2]
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    script = repo / "scripts" / "l0_ingest.py"
    cmd = [sys.executable, str(script), "--data-root", str(data_root), "--anchor-path", str(tmp_path)] + extra
    return subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True, check=False)


def test_l0_ingest_materialize_and_idempotent(tmp_path: Path) -> None:
    src = tmp_path / "in.parquet"
    src.write_bytes(b"fakeparquet")
    proc = _run_ingest(
        tmp_path,
        [
            "--table",
            "t_bet",
            "--partition-key",
            "gaming_day",
            "--partition-value",
            "2026-04-01",
            "--source",
            str(src),
        ],
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip().splitlines()
    assert any(line.startswith("OK snapshot_id=") for line in out)
    snap_line = next(line for line in out if line.startswith("OK snapshot_id="))
    snap_id = snap_line.split("=", 1)[1]
    fp = tmp_path / "data" / "l0_layered" / snap_id / "snapshot_fingerprint.json"
    assert fp.is_file()
    part = tmp_path / "data" / "l0_layered" / snap_id / "t_bet" / "gaming_day=2026-04-01" / "part-000.parquet"
    assert part.read_bytes() == b"fakeparquet"

    proc2 = _run_ingest(
        tmp_path,
        [
            "--table",
            "t_bet",
            "--partition-key",
            "gaming_day",
            "--partition-value",
            "2026-04-01",
            "--source",
            str(src),
        ],
    )
    assert proc2.returncode == 0, proc2.stderr


def test_l0_ingest_rejects_snapshot_id_mismatch(tmp_path: Path) -> None:
    src = tmp_path / "in.parquet"
    src.write_bytes(b"x")
    proc = _run_ingest(
        tmp_path,
        [
            "--table",
            "t_bet",
            "--partition-key",
            "gaming_day",
            "--partition-value",
            "2026-04-01",
            "--source",
            str(src),
            "--snapshot-id",
            "snap_zzzzzzzz",
        ],
    )
    assert proc.returncode == 2


def test_l0_ingest_conflict_on_fingerprint_change(tmp_path: Path) -> None:
    src1 = tmp_path / "a.parquet"
    src1.write_bytes(b"one")
    proc1 = _run_ingest(
        tmp_path,
        [
            "--table",
            "t_bet",
            "--partition-key",
            "gaming_day",
            "--partition-value",
            "2026-04-01",
            "--source",
            str(src1),
            "--snapshot-id",
            "snap_conflict12",
            "--allow-snapshot-id-mismatch",
        ],
    )
    assert proc1.returncode == 0, proc1.stderr

    src2 = tmp_path / "b.parquet"
    src2.write_bytes(b"two")
    proc2 = _run_ingest(
        tmp_path,
        [
            "--table",
            "t_bet",
            "--partition-key",
            "gaming_day",
            "--partition-value",
            "2026-04-01",
            "--source",
            str(src2),
            "--snapshot-id",
            "snap_conflict12",
            "--allow-snapshot-id-mismatch",
        ],
    )
    assert proc2.returncode == 3
