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
            "Deprecated: overrides LightGBM only for this run (logs a warning). "
            "Prefer TRAINER_DEVICE_MODE=auto|cpu|gpu (trainer.core.config) to unify Step 9 LightGBM "
            "and A3 CatBoost/XGBoost bakeoff scheduling. OpenCL GPU on Windows uses device_type=gpu."
        ),
    )
    parser.add_argument(
        "--ranking-recipe",
        type=str,
        default=None,
        choices=(
            "baseline",
            "r2_top_band_light",
            "r2_hnm_light",
            "r2_combined_light",
        ),
        help=(
            "Precision uplift A2/R2: optional rated-only sample_weight recipe before Optuna "
            "(top-band / pseudo-HNM / shallow-HNM refine). When omitted, use env "
            "PRECISION_UPLIFT_RANKING_RECIPE if set, else r2_top_band_light (DEC-044). "
            "Pass baseline to disable A2-style reweighting. Ignored for Plan B+ LibSVM "
            "final fit (on-disk weights); CSV export path applies recipe when set."
        ),
    )
    parser.add_argument(
        "--no-gbm-bakeoff",
        action="store_true",
        help=(
            "Disable default A3/R3 three-model comparison. By default the trainer compares "
            "LightGBM / CatBoost / XGBoost on the same rated split matrices and selects the "
            "winner by the field-test validation objective; this flag keeps only the primary "
            "LightGBM training path. Requires optional deps catboost and xgboost when enabled."
        ),
    )
    parser.add_argument(
        "--gbm-bakeoff-catboost",
        dest="gbm_bakeoff_catboost",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "A3: include CatBoost in the GBM bakeoff for this process. When omitted, "
            "respect GBM_BAKEOFF_ENABLE_CATBOOST if set; otherwise use training-domain "
            "config default (currently off)."
        ),
    )
    parser.add_argument(
        "--gbm-bakeoff-xgboost",
        dest="gbm_bakeoff_xgboost",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "A3: include XGBoost in the GBM bakeoff for this process. When omitted, only the "
            "environment variable GBM_BAKEOFF_ENABLE_XGBOOST applies (unset defaults to off)."
        ),
    )
    parser.add_argument(
        "--disable-oof-stacking",
        action="store_true",
        help=(
            "Disable OOF stacked logistic candidate inside A3 bakeoff. "
            "By default A3 includes stacked_logistic_oof as an additional challenger "
            "when PIT-safe monthly folds can be built."
        ),
    )
    return parser
