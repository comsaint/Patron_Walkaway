"""End-to-end train + backtest using INVESTIGATION_PLAN_TEST_VS_PRODUCTION P1.2 windows.

Defaults (HK-aligned date strings, parsed by trainer/backtester as in CLI):
  Training:   2024-01-01 .. 2025-12-31
  Backtest:   2026-01-01 .. 2026-03-31

Runs two subprocesses (same entrypoints as manual runs)::

  python -m trainer.trainer   --start … --end … [flags]
  python -m trainer.backtester --start … --end … [flags]

**Laptop / skip training — use a pretrained bundle**

Train elsewhere, copy ``out/models/<version>/`` (with ``model.pkl``) here, then::

  python -m trainer.scripts.run_train_backtest_investigation_windows \\
      --backtest-only --use-local-parquet \\
      --model-dir out/models/<version>

Omit ``--model-dir`` / ``--model-version`` to use ``_latest_model_manifest.json``
(or legacy flat ``model.pkl`` under ``MODEL_DIR``), same as ``trainer.backtester``.

From repo root::

  python -m trainer.scripts.run_train_backtest_investigation_windows --help
  python -m trainer.scripts.run_train_backtest_investigation_windows --dry-run
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

_log = logging.getLogger(__name__)

# INVESTIGATION_PLAN_TEST_VS_PRODUCTION.md § P1.2
_DEFAULT_TRAIN_START = "2024-01-01"
_DEFAULT_TRAIN_END = "2025-12-31"
_DEFAULT_BACKTEST_START = "2026-01-01"
_DEFAULT_BACKTEST_END = "2026-03-31"


def _repo_root() -> Path:
    """Return repository root (parent of ``trainer/``)."""
    return Path(__file__).resolve().parents[2]


def _build_train_cmd(
    *,
    train_start: str,
    train_end: str,
    use_local_parquet: bool,
    skip_optuna: bool,
    recent_chunks: Optional[int],
    sample_rated: Optional[int],
    no_preload: bool,
) -> List[str]:
    """Assemble full ``trainer.trainer`` argv including ``sys.executable``."""
    cmd: List[str] = [
        sys.executable,
        "-m",
        "trainer.trainer",
        "--start",
        train_start,
        "--end",
        train_end,
    ]
    if use_local_parquet:
        cmd.append("--use-local-parquet")
    if skip_optuna:
        cmd.append("--skip-optuna")
    if recent_chunks is not None:
        cmd.extend(["--recent-chunks", str(recent_chunks)])
    if sample_rated is not None:
        cmd.extend(["--sample-rated", str(sample_rated)])
    if no_preload:
        cmd.append("--no-preload")
    return cmd


def _build_backtest_cmd(
    *,
    backtest_start: str,
    backtest_end: str,
    use_local_parquet: bool,
    skip_optuna: bool,
    model_version: Optional[str],
    model_dir: Optional[Path],
) -> List[str]:
    """Assemble full ``trainer.backtester`` argv including ``sys.executable``."""
    cmd: List[str] = [
        sys.executable,
        "-m",
        "trainer.backtester",
        "--start",
        backtest_start,
        "--end",
        backtest_end,
    ]
    if use_local_parquet:
        cmd.append("--use-local-parquet")
    if skip_optuna:
        cmd.append("--skip-optuna")
    if model_dir is not None:
        cmd.extend(["--model-dir", str(model_dir)])
    elif model_version:
        cmd.extend(["--model-version", model_version])
    return cmd


def _run_checked(cmd: Sequence[str], *, cwd: Path) -> int:
    """Run *cmd* in *cwd*; stream stdout/stderr; return process return code."""
    _log.info("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(cwd))
    return int(proc.returncode)


def run_pipeline(
    *,
    train_start: str,
    train_end: str,
    backtest_start: str,
    backtest_end: str,
    use_local_parquet: bool,
    skip_train_optuna: bool,
    skip_backtest_optuna: bool,
    recent_chunks: Optional[int],
    sample_rated: Optional[int],
    no_preload: bool,
    model_version: Optional[str],
    model_dir: Optional[Path],
    train_only: bool,
    backtest_only: bool,
    dry_run: bool,
) -> int:
    """Execute train and/or backtest; return 0 on success, non-zero on failure."""
    root = _repo_root()
    if backtest_only:
        if model_dir is not None:
            _log.info("Backtest-only: using model bundle directory %s", model_dir.resolve())
        elif model_version:
            _log.info("Backtest-only: using --model-version %s (under MODEL_DIR)", model_version)
        else:
            _log.info(
                "Backtest-only: no --model-dir/--model-version; backtester resolves latest manifest or legacy bundle",
            )
    if dry_run:
        if not backtest_only:
            print("[dry-run] train:", " ".join(_build_train_cmd(
                train_start=train_start,
                train_end=train_end,
                use_local_parquet=use_local_parquet,
                skip_optuna=skip_train_optuna,
                recent_chunks=recent_chunks,
                sample_rated=sample_rated,
                no_preload=no_preload,
            )))
        if not train_only:
            print("[dry-run] backtest:", " ".join(_build_backtest_cmd(
                backtest_start=backtest_start,
                backtest_end=backtest_end,
                use_local_parquet=use_local_parquet,
                skip_optuna=skip_backtest_optuna,
                model_version=model_version,
                model_dir=model_dir,
            )))
        return 0

    if not backtest_only:
        tr = _build_train_cmd(
            train_start=train_start,
            train_end=train_end,
            use_local_parquet=use_local_parquet,
            skip_optuna=skip_train_optuna,
            recent_chunks=recent_chunks,
            sample_rated=sample_rated,
            no_preload=no_preload,
        )
        rc = _run_checked(tr, cwd=root)
        if rc != 0:
            _log.error("Training exited with code %s", rc)
            return rc

    if not train_only:
        bt = _build_backtest_cmd(
            backtest_start=backtest_start,
            backtest_end=backtest_end,
            use_local_parquet=use_local_parquet,
            skip_optuna=skip_backtest_optuna,
            model_version=model_version,
            model_dir=model_dir,
        )
        rc = _run_checked(bt, cwd=root)
        if rc != 0:
            _log.error("Backtest exited with code %s", rc)
            return rc

    return 0


def build_argparser() -> argparse.ArgumentParser:
    """CLI for the investigation-window e2e runner."""
    p = argparse.ArgumentParser(
        description=(
            "Train + backtest with INVESTIGATION_PLAN P1.2 default windows "
            "(override with --train-start / --backtest-end, etc.). "
            "Use --backtest-only or --eval-only to skip training and run backtest on an existing bundle."
        ),
    )
    p.add_argument(
        "--train-start",
        default=_DEFAULT_TRAIN_START,
        help=f"Training window start (default: {_DEFAULT_TRAIN_START})",
    )
    p.add_argument(
        "--train-end",
        default=_DEFAULT_TRAIN_END,
        help=f"Training window end (default: {_DEFAULT_TRAIN_END})",
    )
    p.add_argument(
        "--backtest-start",
        default=_DEFAULT_BACKTEST_START,
        help=f"Backtest window start (default: {_DEFAULT_BACKTEST_START})",
    )
    p.add_argument(
        "--backtest-end",
        default=_DEFAULT_BACKTEST_END,
        help=f"Backtest window end (default: {_DEFAULT_BACKTEST_END})",
    )
    p.add_argument(
        "--use-local-parquet",
        action="store_true",
        help="Pass --use-local-parquet to both trainer and backtester.",
    )
    p.add_argument(
        "--skip-train-optuna",
        action="store_true",
        help="Pass --skip-optuna to trainer only.",
    )
    p.add_argument(
        "--skip-backtest-optuna",
        action="store_true",
        help="Pass --skip-optuna to backtester only.",
    )
    p.add_argument(
        "--skip-optuna",
        action="store_true",
        help="Pass --skip-optuna to both trainer and backtester.",
    )
    p.add_argument(
        "--recent-chunks",
        type=int,
        default=None,
        metavar="N",
        help="Forward --recent-chunks N to trainer (debug / low-RAM).",
    )
    p.add_argument(
        "--sample-rated",
        type=int,
        default=None,
        metavar="N",
        help="Forward --sample-rated N to trainer.",
    )
    p.add_argument(
        "--no-preload",
        action="store_true",
        help="Forward --no-preload to trainer (lower RAM, slower profile path).",
    )
    p.add_argument(
        "--model-version",
        default=None,
        help="Backtest: --model-version VER (default: latest manifest / legacy root).",
    )
    p.add_argument(
        "--model-dir",
        type=Path,
        default=None,
        help="Backtest: explicit bundle dir (overrides --model-version).",
    )
    p.add_argument(
        "--train-only",
        action="store_true",
        help="Only run trainer.",
    )
    p.add_argument(
        "--backtest-only",
        "--eval-only",
        action="store_true",
        dest="backtest_only",
        help=(
            "Only run backtester (skip training). Use --model-dir or --model-version to pick a bundle; "
            "otherwise backtester uses latest manifest / legacy MODEL_DIR."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print trainer/backtester commands without executing.",
    )
    return p


def main() -> int:
    """Parse CLI and run train/backtest subprocess chain."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_argparser().parse_args()
    if args.train_only and args.backtest_only:
        print("Cannot combine --train-only and --backtest-only.", file=sys.stderr)
        return 2
    skip_tr = bool(args.skip_optuna or args.skip_train_optuna)
    skip_bt = bool(args.skip_optuna or args.skip_backtest_optuna)
    return run_pipeline(
        train_start=args.train_start,
        train_end=args.train_end,
        backtest_start=args.backtest_start,
        backtest_end=args.backtest_end,
        use_local_parquet=bool(args.use_local_parquet),
        skip_train_optuna=skip_tr,
        skip_backtest_optuna=skip_bt,
        recent_chunks=args.recent_chunks,
        sample_rated=args.sample_rated,
        no_preload=bool(args.no_preload),
        model_version=(args.model_version or "").strip() or None,
        model_dir=args.model_dir,
        train_only=bool(args.train_only),
        backtest_only=bool(args.backtest_only),
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    raise SystemExit(main())
