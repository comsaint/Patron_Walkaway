"""High-tier training entry (skeleton).

Analogous role to ``trainer.training.trainer.run_pipeline`` / ``main``: orchestrate
load → fit → artifact write for the reduced high-tier objective. Implementation
is intentionally empty beyond logging and call order.
"""

from __future__ import annotations

import argparse
import importlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq

from trainer_hightier.config import (
    BetPreprocessConfig,
    CanonicalMappingConfig,
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    SessionPreprocessConfig,
)
from trainer_hightier.utils.canonical_mapping import (
    build_canonical_mapping_from_cleaned_session_parquet,
    default_canonical_mapping_parquet_path,
)
from trainer_hightier.utils.patron_session_metrics import compile_canonical_patron_session_metrics

# Module names cannot start with a digit in ``import …`` syntax; load by full name.
_ingest = importlib.import_module("trainer_hightier.01_data_ingest")
_hpre = importlib.import_module("trainer_hightier.02_preprocess")


logger = logging.getLogger("trainer_hightier")


@dataclass
class HighTierTrainArgs:
    """CLI / programmatic arguments for :func:`run_training`."""

    output_dir: Path
    data_dir: Path = field(default_factory=_ingest.default_data_dir)
    random_seed: int = 42
    objective: HighTierObjectiveConfig = field(default_factory=HighTierObjectiveConfig)
    # Skip cleaned-session cache and always recompute (full-table preprocess; heavy on large L0).
    no_cache: bool = False
    # DuckDB PRAGMA defaults for all high-tier DuckDB connections (not ``trainer.core``).
    duckdb_runtime: DuckDbRuntimeConfig = field(default_factory=DuckDbRuntimeConfig)
    session_preprocess: SessionPreprocessConfig = field(default_factory=SessionPreprocessConfig)
    bet_preprocess: BetPreprocessConfig = field(default_factory=BetPreprocessConfig)
    skip_bet_preprocess: bool = False
    canonical_mapping: CanonicalMappingConfig = field(default_factory=CanonicalMappingConfig)


def prepare_training_frame(args: HighTierTrainArgs) -> None:
    """Ingest offline Parquet (presence + schema QC); full-session preprocess to cleaned table."""
    pq_paths = _ingest.resolve_local_parquet_paths(args.data_dir)
    sess_report = _ingest.validate_session_ingress_or_raise(pq_paths)
    logger.info(
        "[Step 1] t_session Parquet OK: %s rows (metadata); t_bet deferred until after session clean / downstream",
        sess_report.session.num_rows,
    )
    cleaned_path = _hpre.default_cleaned_session_parquet_path()
    use_cache = not args.no_cache
    if use_cache and _hpre.session_clean_cache_is_hit(pq_paths.session_parquet, cleaned_path):
        logger.info(
            "[Step 2] session clean cache hit; skip preprocess (use --no-cache to force): %s",
            cleaned_path.resolve(),
        )
    else:
        out_parquet = _hpre.preprocess_sessions_from_parquet_streaming(
            pq_paths.session_parquet,
            cleaned_path,
            cfg=args.session_preprocess,
            duckdb_runtime=args.duckdb_runtime,
        )
        _hpre.write_session_clean_cache_manifest(pq_paths.session_parquet, out_parquet)
        n_clean = int(pq.ParquetFile(out_parquet).metadata.num_rows) if pq.ParquetFile(out_parquet).metadata else 0
        logger.info(
            "[Step 2] session preprocess OK (full table, no time window): cleaned rows=%d; written %s",
            n_clean,
            out_parquet,
        )

    if not cleaned_path.is_file():
        raise FileNotFoundError(f"Cleaned session Parquet missing after preprocess: {cleaned_path}")

    cleaned_bet_path = _hpre.default_cleaned_bet_parquet_path()
    if not args.skip_bet_preprocess and pq_paths.bet_parquet.is_file():
        _ingest.validate_bet_ingress_or_raise(pq_paths)
        reg_yaml = (
            Path(args.bet_preprocess.preprocess_registry_yaml)
            if args.bet_preprocess.preprocess_registry_yaml is not None
            else _hpre.default_preprocess_registry_yaml_path()
        )
        bet_cache_ok = (
            use_cache
            and _hpre.bet_clean_cache_is_hit(
                pq_paths.bet_parquet,
                cleaned_bet_path,
                preprocess_registry_yaml=reg_yaml,
            )
        )
        if bet_cache_ok:
            logger.info(
                "[Step 2b] bet clean cache hit; skip (use --no-cache to force): %s",
                cleaned_bet_path.resolve(),
            )
        else:
            out_b = _hpre.preprocess_bets_from_parquet_streaming(
                pq_paths.bet_parquet,
                cleaned_bet_path,
                cfg=args.bet_preprocess,
                duckdb_runtime=args.duckdb_runtime,
            )
            _hpre.write_bet_clean_cache_manifest(
                pq_paths.bet_parquet,
                out_b,
                preprocess_registry_yaml=reg_yaml,
            )
            n_b = int(pq.ParquetFile(out_b).metadata.num_rows) if pq.ParquetFile(out_b).metadata else 0
            logger.info(
                "[Step 2b] bet preprocess OK: cleaned rows=%d; written %s",
                n_b,
                out_b,
            )
    elif pq_paths.bet_parquet.is_file() and args.skip_bet_preprocess:
        logger.info("[Step 2b] bet preprocess skipped (--no-bet-preprocess); %s", pq_paths.bet_parquet)
    else:
        logger.info(
            "[Step 2b] no %s; skip bet preprocess",
            pq_paths.bet_parquet.name,
        )

    if args.canonical_mapping.enabled:
        build_canonical_mapping_from_cleaned_session_parquet(
            cleaned_path,
            cfg=args.canonical_mapping,
            duckdb_runtime=args.duckdb_runtime,
        )
        if args.canonical_mapping.compile_patron_session_metrics:
            compile_canonical_patron_session_metrics(
                cleaned_path,
                default_canonical_mapping_parquet_path(),
                duckdb_runtime=args.duckdb_runtime,
            )


def fit_model(args: HighTierTrainArgs) -> None:
    """Train a scorer on the high-tier slice (TODO)."""
    logger.info("[Step 5] fit_model: not implemented (seed=%s)", args.random_seed)


def write_artifacts(args: HighTierTrainArgs) -> None:
    """Persist model bundle + minimal metrics sidecars (TODO)."""
    logger.info("[Step 6] write_artifacts: not implemented")


def run_training(args: HighTierTrainArgs) -> None:
    """Run the high-tier training pipeline in order."""
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepare_training_frame(args)
    fit_model(args)
    write_artifacts(args)
    logger.info("[Step 7] run_training: skeleton finished")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="High-tier patron training (skeleton).")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".data") / "trainer_hightier" / "run",
        help="Directory for artifacts and intermediates.",
    )
    p.add_argument("--random-seed", type=int, default=42)
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Directory containing gmwds_t_session.parquet (required). gmwds_t_bet.parquet is validated later when the pipeline ingests bets (default: <repo>/data).",
    )
    p.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore session-clean cache and always recompute cleaned t_session (heavy on large L0).",
    )
    p.add_argument(
        "--canonical-cutoff",
        type=str,
        default=None,
        help=(
            "ISO-8601 cutoff for canonical mapping (training-window end). "
            "Default: infer MAX(session_end_dtm) then MAX(lud_dtm) from cleaned Parquet."
        ),
    )
    p.add_argument(
        "--no-canonical-mapping",
        action="store_true",
        help="Skip player_id -> canonical_id artifact build.",
    )
    p.add_argument(
        "--no-patron-session-metrics",
        action="store_true",
        help="Skip canonical_patron_session_metrics.parquet (ADT report) after canonical mapping.",
    )
    p.add_argument(
        "--no-bet-preprocess",
        action="store_true",
        help="Skip t_bet cleaning even when gmwds_t_bet.parquet exists under --data-dir.",
    )
    p.add_argument(
        "--canonical-legacy-coalesce-cutoff",
        action="store_true",
        help="Use COALESCE(session_end_dtm, lud_dtm) <= cutoff (legacy parity); default is session_end_dtm only.",
    )
    return p


def main() -> None:
    """Parse CLI and invoke :func:`run_training`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = _build_argparser()
    ns = parser.parse_args()
    data_dir = _ingest.default_data_dir() if ns.data_dir is None else Path(ns.data_dir)
    cutoff_parsed: datetime | None = None
    if ns.canonical_cutoff:
        cutoff_parsed = datetime.fromisoformat(ns.canonical_cutoff.replace("Z", "+00:00"))
    canonical_mapping = CanonicalMappingConfig(
        enabled=not bool(ns.no_canonical_mapping),
        cutoff_dtm=cutoff_parsed,
        legacy_coalesce_cutoff=bool(ns.canonical_legacy_coalesce_cutoff),
        compile_patron_session_metrics=not bool(ns.no_patron_session_metrics),
    )
    args = HighTierTrainArgs(
        output_dir=ns.output_dir,
        data_dir=data_dir,
        random_seed=int(ns.random_seed),
        no_cache=bool(ns.no_cache),
        skip_bet_preprocess=bool(ns.no_bet_preprocess),
        canonical_mapping=canonical_mapping,
    )
    run_training(args)


if __name__ == "__main__":
    main()
