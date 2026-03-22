"""Argparse-only builder for player_profile ETL CLI (stdlib).

Used by `trainer.etl_player_profile` stub so `python -m trainer.etl_player_profile --help` does not import pandas / ClickHouse stack.
"""

from __future__ import annotations

import argparse
from datetime import date


def build_etl_player_profile_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Build player_profile snapshot for one date or a date range."
    )
    p.add_argument(
        "--snapshot-date",
        type=date.fromisoformat,
        default=None,
        help="Single snapshot date (YYYY-MM-DD). Defaults to today (HK time).",
    )
    p.add_argument(
        "--start-date",
        type=date.fromisoformat,
        default=None,
        help="Start date for backfill (YYYY-MM-DD). Requires --end-date.",
    )
    p.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help="End date for backfill, inclusive (YYYY-MM-DD).",
    )
    p.add_argument(
        "--local-parquet",
        action="store_true",
        help="Read sessions from local Parquet and write output to local Parquet (dev mode).",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable progress bar (e.g. for CI / non-TTY). PLAN § progress-bars-long-steps.",
    )
    p.add_argument(
        "--month-end",
        action="store_true",
        help=(
            "Build only month-end snapshots (last calendar day of each month in range). "
            "Same schedule as trainer.ensure_player_profile_ready. If range has no month-end "
            "(intra-month), builds one anchor snapshot at latest month-end on or before end-date."
        ),
    )
    p.add_argument(
        "--snapshot-interval-days",
        type=int,
        default=1,
        metavar="N",
        help=(
            "When not using --month-end: compute a snapshot every N days (default 1 = daily). "
            "Trainer always uses month-end; this is for non-trainer backfills."
        ),
    )
    return p
