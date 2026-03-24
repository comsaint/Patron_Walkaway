"""Trainer CLI argparse only (stdlib + core.config).

`python -m trainer.trainer --help` uses this module **before** importing
`trainer.training.trainer` so cold subprocesses stay within tight timeouts.
"""

from __future__ import annotations

import argparse

from trainer.core.config import TRAINER_DAYS


def build_trainer_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patron Walkaway - Phase 1 Trainer")
    parser.add_argument("--start", default=None, help="Training window start (YYYY-MM-DD or ISO)")
    parser.add_argument("--end", default=None, help="Training window end")
    parser.add_argument(
        "--days",
        type=int,
        default=TRAINER_DAYS,
        help="Last N days ending 30m ago (used when --start/--end are not given)",
    )
    parser.add_argument(
        "--use-local-parquet",
        action="store_true",
        help="Read from data/ Parquet instead of ClickHouse",
    )
    parser.add_argument(
        "--rebuild-canonical-mapping",
        action="store_true",
        help=(
            "Force rebuild canonical mapping (do not load from data/canonical_mapping.parquet); "
            "write after build."
        ),
    )
    parser.add_argument(
        "--force-recompute",
        action="store_true",
        help="Ignore cached chunk Parquet files and recompute",
    )
    parser.add_argument(
        "--skip-optuna",
        action="store_true",
        help="Skip Optuna search and use default LightGBM hyperparameters",
    )
    parser.add_argument(
        "--recent-chunks",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Debug/test mode: use only the last N monthly chunks from the training "
            "window. Limits data loaded from both ClickHouse and local Parquet. "
            "Recommended N>=3 to keep train/valid/test all non-empty. "
            "E.g. --recent-chunks 3 uses roughly the last 3 months of data."
        ),
    )
    parser.add_argument(
        "--no-preload",
        action="store_true",
        help=(
            "Disable full-table session Parquet preload during profile backfill. "
            "Instead, each snapshot day reads only the relevant time window via "
            "PyArrow pushdown filters. Recommended for machines with <=8 GB RAM "
            "where the full session Parquet (~74M rows) would cause OOM. "
            "Trade-off: backfill is slower but memory-safe. "
            "By default (flag absent) the entire session table is loaded once."
        ),
    )
    parser.add_argument(
        "--sample-rated",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Deterministically sample N rated canonical_ids (sorted lexicographically, "
            "head N). Default: no sampling (all rated canonical_ids are used). "
            "Example: --sample-rated 1000 to train on a 1k patron subset."
        ),
    )
    parser.add_argument(
        "--lgbm-device",
        type=str,
        default=None,
        metavar="cpu|gpu",
        help=(
            "LightGBM training device for Step 9 (OpenCL GPU on Windows via device_type=gpu). "
            "Overrides LIGHTGBM_DEVICE_TYPE env / config for this run only. "
            "Default: use LIGHTGBM_DEVICE_TYPE (trainer.core.config, default cpu)."
        ),
    )
    return parser
