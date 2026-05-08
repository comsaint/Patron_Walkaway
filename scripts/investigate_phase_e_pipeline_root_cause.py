#!/usr/bin/env python3
"""Investigate Phase E pipeline crash with hard evidence collection.

This script runs the real trainer pipeline as a subprocess, enables Phase E
memory diagnostics in-process, captures stdout/stderr, samples RSS over time,
and queries Windows event logs for the run time window.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import psutil


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = REPO_ROOT / "data" / "phase_e_investigation"


@dataclass
class RunResult:
    """Evidence bundle for one pipeline run."""

    run_idx: int
    started_utc: str
    ended_utc: str
    elapsed_s: float
    exit_code: Optional[int]
    max_rss_mb: float
    saw_predict_begin: bool
    saw_batch_begin: bool
    saw_before_predict_proba: bool
    saw_after_predict_proba: bool
    saw_batch_end: bool
    stdout_tail: str
    windows_events: list[dict[str, Any]]


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-runs", type=int, default=3)
    parser.add_argument("--trainer-arg", action="append", default=["--use-local-parquet"])
    parser.add_argument(
        "--stop-on-first-failure",
        action="store_true",
        help="Stop immediately when a non-zero exit or silent boundary appears.",
    )
    return parser.parse_args(argv)


def _trainer_bootstrap_code() -> str:
    """Return Python code string to run pipeline with diag toggle."""
    return (
        "import sys\n"
        "try:\n"
        "    if hasattr(sys.stdout, 'reconfigure'):\n"
        "        sys.stdout.reconfigure(errors='replace')\n"
        "    if hasattr(sys.stderr, 'reconfigure'):\n"
        "        sys.stderr.reconfigure(errors='replace')\n"
        "except Exception:\n"
        "    pass\n"
        "from trainer.training.trainer_argparse import build_trainer_argparser\n"
        "import trainer.core.config as _cfg\n"
        "_cfg.A3_PHASE_E_DIAG_MEMORY_SNAPSHOT = True\n"
        "args = build_trainer_argparser().parse_args(sys.argv[1:])\n"
        "from trainer.training import trainer as _t\n"
        "_t.run_pipeline(args)\n"
    )


def _sample_rss_tree(pid: int) -> float:
    """Return total RSS (MB) for process tree rooted at *pid*."""
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return 0.0
    total = 0
    procs = [root]
    try:
        procs.extend(root.children(recursive=True))
    except psutil.Error:
        pass
    for proc in procs:
        try:
            total += int(proc.memory_info().rss)
        except psutil.Error:
            continue
    return float(total) / (1024.0**2)


def _poll_rss(pid: int, out: list[float], stop_evt: threading.Event) -> None:
    """Poll process-tree RSS every second until stopped."""
    while not stop_evt.is_set():
        out.append(_sample_rss_tree(pid))
        time.sleep(1.0)


def _windows_events(start_utc: datetime, end_utc: datetime) -> list[dict[str, Any]]:
    """Query relevant Windows events in [start-2m, end+2m]."""
    start_local = (start_utc - timedelta(minutes=2)).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    end_local = (end_utc + timedelta(minutes=2)).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "$s=[datetime]'"
            + start_local
            + "'; $e=[datetime]'"
            + end_local
            + "'; "
            "$ev=Get-WinEvent -FilterHashtable @{LogName='Application';StartTime=$s;EndTime=$e} -ErrorAction SilentlyContinue; "
            "$ev += Get-WinEvent -FilterHashtable @{LogName='System';StartTime=$s;EndTime=$e} -ErrorAction SilentlyContinue; "
            "$ev | Where-Object { "
            "$_.ProviderName -in @('Application Error','Windows Error Reporting','Microsoft-Windows-Resource-Exhaustion-Detector') "
            "-or $_.Message -match 'python|xgboost|out of memory|resource exhaustion|0xC0000409|0xC0000005' "
            "} | Select-Object TimeCreated,LogName,ProviderName,Id,LevelDisplayName,Message "
            "| ConvertTo-Json -Depth 4"
        ),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        return [{"error": f"powershell_invoke_failed:{exc}"}]
    blob = (proc.stdout or "").strip()
    if not blob:
        return []
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        return [{"error": "event_json_decode_failed", "raw": blob[-2000:]}]
    if isinstance(parsed, list):
        return [dict(x) for x in parsed]
    if isinstance(parsed, dict):
        return [parsed]
    return [{"error": "unexpected_event_shape", "repr": repr(parsed)}]


def _run_once(run_idx: int, trainer_args: list[str]) -> RunResult:
    """Run one pipeline attempt and return full evidence."""
    started = datetime.now(timezone.utc)
    cmd = [sys.executable, "-c", _trainer_bootstrap_code(), *trainer_args]
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
        bufsize=0,
    )
    assert proc.stdout is not None
    rss_samples: list[float] = []
    stop_evt = threading.Event()
    poller = threading.Thread(target=_poll_rss, args=(proc.pid, rss_samples, stop_evt), daemon=True)
    poller.start()
    t0 = time.perf_counter()
    chunks: list[bytes] = []
    try:
        while True:
            chunk = proc.stdout.readline()
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        stop_evt.set()
        poller.join(timeout=3.0)
    proc.wait()
    ended = datetime.now(timezone.utc)
    blob = b"".join(chunks).decode("utf-8", errors="replace")
    return RunResult(
        run_idx=run_idx,
        started_utc=started.isoformat(),
        ended_utc=ended.isoformat(),
        elapsed_s=round(time.perf_counter() - t0, 3),
        exit_code=int(proc.returncode) if proc.returncode is not None else None,
        max_rss_mb=round(max(rss_samples) if rss_samples else 0.0, 2),
        saw_predict_begin=("A3 PhaseE predict_begin" in blob),
        saw_batch_begin=("A3 PhaseE batch_begin" in blob),
        saw_before_predict_proba=("A3 PhaseE_diag tag=before_predict_proba" in blob),
        saw_after_predict_proba=("A3 PhaseE_diag tag=after_predict_proba" in blob),
        saw_batch_end=("A3 PhaseE batch_end" in blob),
        stdout_tail=blob[-8000:] if len(blob) > 8000 else blob,
        windows_events=_windows_events(started, ended),
    )


def _is_silent_boundary_fail(res: RunResult) -> bool:
    """Return True when logs stop at predict boundary without batch_end."""
    return bool(res.saw_batch_begin and not res.saw_batch_end)


def _report_payload(results: list[RunResult]) -> dict[str, Any]:
    """Build final JSON payload with root-cause assessment."""
    nonzero = [r.run_idx for r in results if (r.exit_code is not None and r.exit_code != 0)]
    silent = [r.run_idx for r in results if _is_silent_boundary_fail(r)]
    any_event = [r for r in results if r.windows_events]
    if silent or nonzero:
        root_cause = "pipeline_failure_reproduced"
    else:
        root_cause = "not_reproduced_on_this_host"
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "root_cause_status": root_cause,
        "nonzero_runs": nonzero,
        "silent_boundary_runs": silent,
        "runs_with_windows_events": [r.run_idx for r in any_event],
        "runs": [asdict(r) for r in results],
    }


def main(argv: Optional[list[str]] = None) -> int:
    """Execute repeated pipeline runs and persist investigation report."""
    args = _parse_args(argv)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[RunResult] = []
    trainer_args = [str(x) for x in args.trainer_arg]
    for idx in range(1, max(1, int(args.max_runs)) + 1):
        result = _run_once(idx, trainer_args)
        results.append(result)
        fail_like = bool(result.exit_code not in (0, None)) or _is_silent_boundary_fail(result)
        print(
            f"run={idx} exit={result.exit_code} max_rss_mb={result.max_rss_mb} "
            f"batch_begin={result.saw_batch_begin} batch_end={result.saw_batch_end} "
            f"after_predict={result.saw_after_predict_proba}"
        )
        if args.stop_on_first_failure and fail_like:
            break
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = REPORT_DIR / f"pipeline_root_cause_{stamp}.json"
    out_path.write_text(json.dumps(_report_payload(results), indent=2), encoding="utf-8")
    print(str(out_path.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
