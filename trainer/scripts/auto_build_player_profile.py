"""One-command OOM-safe runner for player_profile_daily ETL.

This helper wraps `trainer/etl_player_profile.py` with:
1) automatic date-range detection from local session parquet metadata,
2) checkpoint-based resume,
3) adaptive chunk sizing on failure (reduce days per run).

Typical usage:
    python trainer/scripts/auto_build_player_profile.py --local-parquet
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import psutil
import pyarrow.parquet as pq


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SESSION_PARQUET = DATA_DIR / "gmwds_t_session.parquet"
ETL_SCRIPT = PROJECT_ROOT / "trainer" / "etl_player_profile.py"
DEFAULT_CHECKPOINT = DATA_DIR / "player_profile_etl_checkpoint.json"


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


def _parse_value_to_date(v: object) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


def detect_date_range_from_parquet(path: Path) -> tuple[date, date]:
    if not path.exists():
        raise FileNotFoundError(f"Session parquet not found: {path}")

    pf = pq.ParquetFile(path)
    col_names = pf.schema_arrow.names
    candidates = ["gaming_day", "session_end_dtm", "lud_dtm", "session_start_dtm"]

    for col in candidates:
        if col not in col_names:
            continue
        col_idx = col_names.index(col)
        mins: list[date] = []
        maxs: list[date] = []
        for i in range(pf.metadata.num_row_groups):
            stats = pf.metadata.row_group(i).column(col_idx).statistics
            if stats is None or not getattr(stats, "has_min_max", False):
                continue
            dmin = _parse_value_to_date(stats.min)
            dmax = _parse_value_to_date(stats.max)
            if dmin is not None:
                mins.append(dmin)
            if dmax is not None:
                maxs.append(dmax)
        if mins and maxs:
            return min(mins), max(maxs)

    raise RuntimeError(
        "Cannot infer date range from parquet metadata stats. "
        "Please provide --start-date and --end-date explicitly."
    )


def load_checkpoint(path: Path) -> Optional[date]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        val = payload.get("last_success_date")
        if not val:
            return None
        return date.fromisoformat(val)
    except Exception:
        return None


def save_checkpoint(path: Path, d: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_success_date": d.isoformat(),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def infer_initial_chunk_days(user_chunk_days: int) -> int:
    if user_chunk_days > 0:
        return user_chunk_days
    total_gb = psutil.virtual_memory().total / (1024**3)
    if total_gb >= 96:
        return 14
    if total_gb >= 64:
        return 7
    if total_gb >= 32:
        return 3
    return 1


def run_etl_chunk(start_d: date, end_d: date, local_parquet: bool) -> CmdResult:
    cmd = [
        sys.executable,
        str(ETL_SCRIPT),
        "--start-date",
        start_d.isoformat(),
        "--end-date",
        end_d.isoformat(),
    ]
    if local_parquet:
        cmd.append("--local-parquet")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return CmdResult(proc.returncode, proc.stdout, proc.stderr)


def _tail(text: str, n: int = 30) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def auto_run(
    start_date: date,
    end_date: date,
    checkpoint_file: Path,
    local_parquet: bool,
    chunk_days: int,
    resume: bool,
) -> None:
    if start_date > end_date:
        raise ValueError("start_date cannot be later than end_date")

    current = start_date
    if resume:
        ckpt = load_checkpoint(checkpoint_file)
        if ckpt is not None and ckpt >= start_date:
            current = min(ckpt + timedelta(days=1), end_date + timedelta(days=1))
            print(f"[resume] checkpoint found: {ckpt.isoformat()} -> continue from {current.isoformat()}")

    if current > end_date:
        print("Nothing to do. Range already completed.")
        return

    base_chunk = max(1, chunk_days)
    active_chunk = base_chunk

    while current <= end_date:
        chunk_end = min(current + timedelta(days=active_chunk - 1), end_date)
        print(f"[run] {current.isoformat()} -> {chunk_end.isoformat()} (chunk_days={active_chunk})")
        result = run_etl_chunk(current, chunk_end, local_parquet=local_parquet)

        if result.returncode == 0:
            save_checkpoint(checkpoint_file, chunk_end)
            print(f"[ok] completed through {chunk_end.isoformat()}")
            current = chunk_end + timedelta(days=1)
            # Recover toward baseline chunk size after successful retries.
            if active_chunk < base_chunk:
                active_chunk = min(base_chunk, active_chunk * 2)
            continue

        print("[warn] chunk failed")
        err_tail = _tail(result.stderr)
        out_tail = _tail(result.stdout)
        if err_tail:
            print("[stderr-tail]")
            print(err_tail)
        elif out_tail:
            print("[stdout-tail]")
            print(out_tail)

        if active_chunk > 1:
            active_chunk = max(1, active_chunk // 2)
            print(f"[retry] reducing chunk_days to {active_chunk} and retrying same start date")
            continue

        raise SystemExit(
            f"Failed even at 1-day chunk on {current.isoformat()}. "
            "Please inspect logs above; this is likely not just memory pressure."
        )

    print("[done] player_profile_daily backfill completed.")
    print(f"[checkpoint] {checkpoint_file}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Automatically run OOM-safe player_profile_daily backfill."
    )
    p.add_argument("--start-date", type=date.fromisoformat, default=None)
    p.add_argument("--end-date", type=date.fromisoformat, default=None)
    p.add_argument(
        "--local-parquet",
        action="store_true",
        help="Use local parquet input/output mode in etl_player_profile.py.",
    )
    p.add_argument(
        "--chunk-days",
        type=int,
        default=0,
        help="Initial chunk size (days). 0 = auto by machine RAM.",
    )
    p.add_argument(
        "--checkpoint-file",
        type=Path,
        default=DEFAULT_CHECKPOINT,
        help="Checkpoint file path for resume.",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore checkpoint and start from --start-date.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    start_d = args.start_date
    end_d = args.end_date
    if start_d is None or end_d is None:
        auto_start, auto_end = detect_date_range_from_parquet(SESSION_PARQUET)
        start_d = start_d or auto_start
        end_d = end_d or auto_end
        print(
            "[range] auto-detected from parquet metadata:",
            f"{start_d.isoformat()} -> {end_d.isoformat()}",
        )

    if not ETL_SCRIPT.exists():
        raise FileNotFoundError(f"Cannot find ETL script: {ETL_SCRIPT}")

    init_chunk = infer_initial_chunk_days(args.chunk_days)
    print(f"[config] initial chunk_days={init_chunk}, local_parquet={args.local_parquet}")

    auto_run(
        start_date=start_d,
        end_date=end_d,
        checkpoint_file=args.checkpoint_file,
        local_parquet=args.local_parquet,
        chunk_days=init_chunk,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()

