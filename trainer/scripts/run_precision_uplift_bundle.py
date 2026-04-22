"""One-command bundle runner for Precision Uplift training workflows.

This wrapper keeps the main train command simple while making evidence/report
steps optional and reproducible.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Run trainer + optional W1 freeze evidence + optional W2 parity report "
            "in one command."
        )
    )
    p.add_argument(
        "--python-executable",
        default=sys.executable,
        help="Python executable used for subcommands (default: current interpreter).",
    )
    p.add_argument(
        "--trainer-module",
        default="trainer.training.trainer",
        help="Trainer module for -m execution.",
    )
    p.add_argument(
        "--precondition-json",
        default=None,
        help=(
            "When provided, injects FIELD_TEST_OBJECTIVE_PRECONDITION_JSON into "
            "the trainer process environment."
        ),
    )
    p.add_argument(
        "--skip-train",
        action="store_true",
        help="Skip trainer execution (useful for report-only reruns).",
    )
    p.add_argument(
        "--run-dir",
        action="append",
        default=[],
        help="Run directory for downstream evidence/report steps. Repeatable.",
    )
    p.add_argument(
        "--auto-discover-latest-run-dir-glob",
        default="out/models/*",
        help=(
            "When no --run-dir is provided, pick the newest run directory matching "
            "this glob (default: out/models/*)."
        ),
    )
    p.add_argument(
        "--emit-w1-freeze-evidence",
        action="store_true",
        help="Run trainer.scripts.build_w1_freeze_evidence after training.",
    )
    p.add_argument(
        "--w1-output-json",
        default="out/precision_uplift_field_test_objective/w1_freeze_evidence.json",
        help="Output JSON path for W1 freeze evidence.",
    )
    p.add_argument(
        "--w1-output-md",
        default="trainer/precision_improvement_plan/w1_freeze_evidence.md",
        help="Output markdown path for W1 freeze evidence.",
    )
    p.add_argument(
        "--emit-w2-parity",
        action="store_true",
        help="Run trainer.scripts.report_w2_objective_parity after training.",
    )
    p.add_argument(
        "--w2-output-csv",
        default="out/precision_uplift_field_test_objective/w2_objective_parity_report.csv",
        help="Output CSV path for W2 parity report.",
    )
    p.add_argument(
        "--w2-output-md",
        default="trainer/precision_improvement_plan/w2_objective_parity_report.md",
        help="Output markdown path for W2 parity report.",
    )
    p.add_argument(
        "--w2-run-dir",
        action="append",
        default=[],
        help="Additional run directories for W2 parity report. Repeatable.",
    )
    p.add_argument(
        "--w2-run-dir-glob",
        action="append",
        default=[],
        help="Additional run directory globs for W2 parity report. Repeatable.",
    )
    return p


def _resolve_latest_run_dir(glob_pattern: str) -> Optional[Path]:
    if not glob_pattern:
        return None
    candidates = [Path(raw) for raw in glob.glob(glob_pattern) if Path(raw).is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _run_cmd(cmd: list[str], env: Optional[dict[str, str]] = None) -> int:
    cp = subprocess.run(cmd, env=env, check=False)
    return int(cp.returncode)


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args, trainer_args = parser.parse_known_args(argv)
    if trainer_args and trainer_args[0] == "--":
        trainer_args = trainer_args[1:]

    run_dirs: list[str] = [str(v).strip() for v in args.run_dir if str(v).strip()]
    if not run_dirs:
        discovered = _resolve_latest_run_dir(str(args.auto_discover_latest_run_dir_glob or "").strip())
        if discovered is not None:
            run_dirs.append(str(discovered))

    if not args.skip_train:
        trainer_cmd = [str(args.python_executable), "-m", str(args.trainer_module)] + list(trainer_args)
        trainer_env = os.environ.copy()
        if args.precondition_json:
            trainer_env["FIELD_TEST_OBJECTIVE_PRECONDITION_JSON"] = str(args.precondition_json)
        rc = _run_cmd(trainer_cmd, env=trainer_env)
        if rc != 0:
            return rc

    if args.emit_w1_freeze_evidence:
        if not run_dirs:
            raise SystemExit(
                "No run directory for W1 evidence. Provide --run-dir or ensure "
                "--auto-discover-latest-run-dir-glob can find one."
            )
        w1_cmd = [
            str(args.python_executable),
            "-m",
            "trainer.scripts.build_w1_freeze_evidence",
        ]
        if args.precondition_json:
            w1_cmd += ["--precondition-json", str(args.precondition_json)]
        for d in run_dirs:
            w1_cmd += ["--run-dir", str(d)]
        w1_cmd += ["--output-json", str(args.w1_output_json), "--output-md", str(args.w1_output_md)]
        rc = _run_cmd(w1_cmd)
        if rc != 0:
            return rc

    if args.emit_w2_parity:
        w2_run_dirs = list(run_dirs)
        w2_run_dirs.extend(str(v).strip() for v in args.w2_run_dir if str(v).strip())
        w2_run_globs = [str(v).strip() for v in args.w2_run_dir_glob if str(v).strip()]
        if not w2_run_dirs and not w2_run_globs:
            raise SystemExit(
                "No run directories for W2 parity. Provide --run-dir/--w2-run-dir or "
                "--w2-run-dir-glob."
            )
        w2_cmd = [
            str(args.python_executable),
            "-m",
            "trainer.scripts.report_w2_objective_parity",
        ]
        for d in w2_run_dirs:
            w2_cmd += ["--run-dir", d]
        for g in w2_run_globs:
            w2_cmd += ["--run-dir-glob", g]
        w2_cmd += ["--output-csv", str(args.w2_output_csv), "--output-md", str(args.w2_output_md)]
        rc = _run_cmd(w2_cmd)
        if rc != 0:
            return rc

    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
