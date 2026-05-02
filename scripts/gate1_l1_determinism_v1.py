#!/usr/bin/env python3
"""Gate 1 L1 determinism check (LDA-E1-08): same inputs under multiple DuckDB resource profiles.

Exits 0 when row counts and row-level fingerprints match across profiles; prints JSON report to stdout.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from layered_data_assets.l1_determinism_gate_v1 import (  # noqa: E402
    GATE1_DEFAULT_DUCKDB_PROFILES,
    gate1_l1_report_across_duckdb_profiles,
    gate1_report_to_json,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI for Gate 1 L1 determinism."""
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--artifact",
        required=True,
        choices=["run_fact", "run_bet_map", "run_day_bridge"],
        help="Which L1 Parquet artifact to materialize per profile",
    )
    p.add_argument(
        "--input",
        dest="inputs",
        action="append",
        type=Path,
        required=True,
        help="Cleaned bet Parquet path (repeatable)",
    )
    p.add_argument("--output-dir", type=Path, required=True, help="Directory for per-profile Parquet outputs")
    p.add_argument(
        "--run-end-gaming-day",
        default=None,
        help="Required for run_fact and run_bet_map (YYYY-MM-DD)",
    )
    p.add_argument(
        "--bet-gaming-day",
        default=None,
        help="Required for run_day_bridge (YYYY-MM-DD)",
    )
    p.add_argument("--run-break-min", type=float, default=30.0, help="Run boundary gap in minutes")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run Gate 1 report and exit non-zero on mismatch."""
    try:
        import duckdb
    except ImportError:
        print("duckdb is required (see requirements.txt).", file=sys.stderr)
        return 2

    args = _parse_args(argv)
    if args.artifact in ("run_fact", "run_bet_map") and not args.run_end_gaming_day:
        print("--run-end-gaming-day is required for this artifact.", file=sys.stderr)
        return 2
    if args.artifact == "run_day_bridge" and not args.bet_gaming_day:
        print("--bet-gaming-day is required for run_day_bridge.", file=sys.stderr)
        return 2

    rep = gate1_l1_report_across_duckdb_profiles(
        duckdb_module=duckdb,
        artifact=args.artifact,
        input_paths=[p.resolve() for p in args.inputs],
        output_dir=args.output_dir.resolve(),
        profiles=GATE1_DEFAULT_DUCKDB_PROFILES,
        run_end_gaming_day=args.run_end_gaming_day,
        bet_gaming_day=args.bet_gaming_day,
        run_break_min=args.run_break_min,
    )
    sys.stdout.write(gate1_report_to_json(rep))
    ok = bool(rep["all_row_counts_match"] and rep["all_row_fingerprints_match"] and rep["all_row_fingerprint_row_counts_match_stats"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
