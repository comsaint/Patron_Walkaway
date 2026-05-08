#!/usr/bin/env python3
"""Run Phase E repro variants under subprocess + RSS polling; write JSON evidence.

Each experiment spawns ``repro_phase_e_xgboost_predict.py`` as a child, polls the
child RSS until exit, and records exit code, max RSS, and whether log shows
``after_predict_proba`` (first batch completed).

Output: ``data/phase_e_investigation/report_<utc_stamp>.json`` (and prints path).

Usage (from repo root)::

    python scripts/investigate_phase_e_crash.py
    python scripts/investigate_phase_e_crash.py --include-stress
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO = Path(__file__).resolve().parents[1]


@dataclass
class ExperimentResult:
    """One subprocess experiment outcome."""

    name: str
    argv_suffix: list[str]
    exit_code: Optional[int]
    duration_s: float
    max_rss_mb: float
    saw_after_predict_proba: bool
    saw_batch_end: bool
    stderr_tail: str


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--include-stress",
        action="store_true",
        help="add slower mimic-500 + large train experiment (minutes, multi-GB RSS)",
    )
    return p.parse_args()


def _poll_rss(pid: int, out: list[float], stop: threading.Event) -> None:
    """Background thread: append child RSS in MiB until *stop* is set."""
    try:
        import psutil
    except ImportError:
        return
    proc = psutil.Process(pid)
    while not stop.is_set():
        try:
            out.append(float(proc.memory_info().rss) / (1024.0**2))
        except Exception:
            break
        time.sleep(0.4)


def _run_one(name: str, argv_suffix: list[str]) -> ExperimentResult:
    """Spawn repro script with *argv_suffix*; return structured result."""
    script = _REPO / "scripts" / "repro_phase_e_xgboost_predict.py"
    cmd = [sys.executable, str(script), *argv_suffix]
    rss_samples: list[float] = []
    stop = threading.Event()
    proc = subprocess.Popen(
        cmd,
        cwd=str(_REPO),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    poller = threading.Thread(target=_poll_rss, args=(proc.pid, rss_samples, stop), daemon=True)
    poller.start()
    t0 = time.perf_counter()
    out_lines: list[str] = []
    try:
        for line in proc.stdout:
            out_lines.append(line)
    finally:
        stop.set()
        poller.join(timeout=2.0)
    proc.wait()
    elapsed = time.perf_counter() - t0
    blob = "".join(out_lines)
    max_rss = max(rss_samples) if rss_samples else 0.0
    return ExperimentResult(
        name=name,
        argv_suffix=argv_suffix,
        exit_code=int(proc.returncode) if proc.returncode is not None else None,
        duration_s=round(elapsed, 3),
        max_rss_mb=round(max_rss, 2),
        saw_after_predict_proba=("after_predict_proba" in blob),
        saw_batch_end=("A3 PhaseE batch_end" in blob),
        stderr_tail=blob[-4000:] if len(blob) > 4000 else blob,
    )


def _conclusion(rows: list[ExperimentResult]) -> dict[str, Any]:
    """Summarise matrix into a short evidence-backed conclusion."""
    any_kill = any((r.exit_code is not None and r.exit_code < 0) for r in rows)
    any_fail = any((r.exit_code is not None and r.exit_code != 0) for r in rows)
    stuck = [r.name for r in rows if r.exit_code == 0 and not r.saw_after_predict_proba]
    bloat_fail = [r.name for r in rows if "bloat" in r.name and any_fail]
    all_ok = not any_fail and not stuck
    return {
        "any_nonzero_exit": bool(any_fail),
        "any_negative_exit_suggesting_signal": bool(any_kill),
        "completed_without_after_predict_proba": stuck,
        "bloat_named_runs_failed": bloat_fail,
        "reproduced_silent_failure_on_this_host": not all_ok,
        "failure_boundary_in_code_when_logs_stop_after_batch_begin": (
            "_phase_e_dense_positive_scores: after chunk.astype(float32), "
            "inside model.predict_proba(...) before batch_end log"
        ),
        "what_this_matrix_proves_if_all_pass": (
            "On this host, XGBoost sklearn Phase E completes for prodish val row count, "
            "mimic-style params, n_jobs in {1,-1}, and optional VMS bloat; it does NOT "
            "reproduce a silent kill, so the production failure is environment- or "
            "pipeline-state-specific (not a deterministic bug in this path alone)."
        ),
        "interpretation_hint": (
            "If bloat runs fail with exit -9/-6 or no log after before_predict_proba, "
            "prefer OS OOM / memory pressure. If all runs pass here but production dies, "
            "root cause is likely pipeline-only RSS (data + prior steps), not predict_proba alone."
        ),
    }


def main() -> int:
    """Run experiment matrix and write JSON report."""
    args = _parse_args()
    experiments: list[tuple[str, list[str]]] = [
        (
            "e1_smoke_mimic_diag",
            ["--preset", "smoke", "--mimic-pipeline-fit", "--n-estimators", "20", "--diag-memory-snapshot"],
        ),
        (
            "e2_prodish_mimic40_diag",
            [
                "--preset",
                "prodish",
                "--mimic-pipeline-fit",
                "--n-estimators",
                "40",
                "--train-rows",
                "150000",
                "--diag-memory-snapshot",
            ],
        ),
        (
            "e3_prodish_mimic40_njobs1",
            [
                "--preset",
                "prodish",
                "--mimic-pipeline-fit",
                "--n-estimators",
                "40",
                "--train-rows",
                "150000",
                "--xgb-n-jobs",
                "1",
                "--diag-memory-snapshot",
            ],
        ),
        (
            "e4_prodish_mimic40_bloat1p5gib",
            [
                "--preset",
                "prodish",
                "--mimic-pipeline-fit",
                "--n-estimators",
                "40",
                "--train-rows",
                "150000",
                "--bloat-gib",
                "1.5",
                "--diag-memory-snapshot",
            ],
        ),
    ]
    if args.include_stress:
        experiments.append(
            (
                "e5_stress_mimic500_train800k",
                [
                    "--preset",
                    "prodish",
                    "--mimic-pipeline-fit",
                    "--train-rows",
                    "800000",
                    "--diag-memory-snapshot",
                ],
            )
        )
    results = [_run_one(n, suf) for n, suf in experiments]
    out_dir = _REPO / "data" / "phase_e_investigation"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"report_{stamp}.json"
    payload = {
        "utc": stamp,
        "python": sys.version,
        "experiments": [asdict(r) for r in results],
        "conclusion": _conclusion(results),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(str(path.resolve()))
    for r in results:
        print(
            f"{r.name}: exit={r.exit_code} max_rss_mb={r.max_rss_mb} "
            f"after_proba={r.saw_after_predict_proba} batch_end={r.saw_batch_end} "
            f"t={r.duration_s}s"
        )
    return 0 if all((r.exit_code == 0) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
