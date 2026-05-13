"""High-tier training entry (skeleton).

Analogous role to ``trainer.training.trainer.run_pipeline`` / ``main``: orchestrate
load → fit → artifact write for the reduced high-tier objective. Implementation
is intentionally empty beyond logging and call order.
"""

from __future__ import annotations

import argparse
import importlib
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

import pyarrow.parquet as pq

from trainer_hightier.config import (
    BetPreprocessConfig,
    CanonicalMappingConfig,
    DEFAULT_RUN_PROFILE_NAME,
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    SessionPreprocessConfig,
    configs_from_run_profile,
    get_run_profile,
)
from trainer_hightier.utils.canonical_mapping import (
    build_canonical_mapping_from_cleaned_session_parquet,
    default_canonical_mapping_parquet_path,
)
from trainer_hightier.utils.patron_session_metrics import (
    compile_canonical_patron_profile_csv,
    compile_canonical_patron_session_metrics,
    default_adt_allowed_players_parquet_path,
    default_patron_profile_csv_path,
    materialize_adt_allowed_players_parquet,
)

# Module names cannot start with a digit in ``import …`` syntax; load by full name.
_ingest = importlib.import_module("trainer_hightier.01_data_ingest")
_hpre = importlib.import_module("trainer_hightier.02_preprocess")


logger = logging.getLogger("trainer_hightier")


@dataclass
class HighTierTrainArgs:
    """Programmatic run configuration for :func:`run_training` (defaults + optional overrides)."""

    output_dir: Path
    data_dir: Path = field(default_factory=_ingest.default_data_dir)
    random_seed: int = 42
    objective: HighTierObjectiveConfig = field(default_factory=HighTierObjectiveConfig)
    # When True: skip all preprocess disk-cache short-circuits (session-clean + bet-clean manifests).
    ignore_caches: bool = False
    # DuckDB PRAGMA defaults for all high-tier DuckDB connections (not ``trainer.core``).
    duckdb_runtime: DuckDbRuntimeConfig = field(default_factory=DuckDbRuntimeConfig)
    session_preprocess: SessionPreprocessConfig = field(default_factory=SessionPreprocessConfig)
    bet_preprocess: BetPreprocessConfig = field(default_factory=BetPreprocessConfig)
    skip_bet_preprocess: bool = False
    canonical_mapping: CanonicalMappingConfig = field(default_factory=CanonicalMappingConfig)
    # When True with ``objective.theo_train_quantile`` in (0,1), bet preprocess keeps only ADT-top patrons.
    filter_bets_by_adt_quantile: bool = True


def prepare_training_frame(args: HighTierTrainArgs) -> None:
    """Ingest offline Parquet (presence + schema QC); full-session preprocess to cleaned table."""
    pq_paths = _ingest.resolve_local_parquet_paths(args.data_dir)
    sess_report = _ingest.validate_session_ingress_or_raise(pq_paths)
    logger.info(
        "[Step 1] t_session Parquet OK: %s rows (metadata); t_bet deferred until after session clean / downstream",
        sess_report.session.num_rows,
    )
    cleaned_path = _hpre.default_cleaned_session_parquet_path()
    use_preprocess_caches = not args.ignore_caches
    if use_preprocess_caches and _hpre.session_clean_cache_is_hit(
        pq_paths.session_parquet,
        cleaned_path,
        dedup_hash_buckets=args.session_preprocess.dedup_hash_buckets,
    ):
        logger.info(
            "[Step 2] session clean cache hit; skip preprocess (use --ignore-caches to force): %s",
            cleaned_path.resolve(),
        )
    else:
        out_parquet = _hpre.preprocess_sessions_from_parquet_streaming(
            pq_paths.session_parquet,
            cleaned_path,
            cfg=args.session_preprocess,
            duckdb_runtime=args.duckdb_runtime,
        )
        _hpre.write_session_clean_cache_manifest(
            pq_paths.session_parquet,
            out_parquet,
            dedup_hash_buckets=args.session_preprocess.dedup_hash_buckets,
        )
        n_clean = int(pq.ParquetFile(out_parquet).metadata.num_rows) if pq.ParquetFile(out_parquet).metadata else 0
        logger.info(
            "[Step 2] session preprocess OK (full table, no time window): cleaned rows=%d; written %s",
            n_clean,
            out_parquet,
        )

    if not cleaned_path.is_file():
        raise FileNotFoundError(f"Cleaned session Parquet missing after preprocess: {cleaned_path}")

    cleaned_bet_path = _hpre.default_cleaned_bet_parquet_path()
    mapping_parquet_path = default_canonical_mapping_parquet_path()
    profile_csv_path = default_patron_profile_csv_path()
    q_thr = float(args.objective.theo_train_quantile)
    want_adt_bets = (
        args.filter_bets_by_adt_quantile
        and not args.skip_bet_preprocess
        and pq_paths.bet_parquet.is_file()
        and 0.0 < q_thr < 1.0
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
                mapping_parquet_path,
                duckdb_runtime=args.duckdb_runtime,
            )
        if args.canonical_mapping.compile_patron_profile_csv:
            compile_canonical_patron_profile_csv(
                cleaned_path,
                mapping_parquet_path,
                duckdb_runtime=args.duckdb_runtime,
            )
        elif want_adt_bets:
            raise ValueError(
                "Bet ADT top-patron filter requires canonical_patron_profile.csv. "
                "Enable CanonicalMappingConfig.compile_patron_profile_csv, or set "
                "filter_bets_by_adt_quantile=False / objective.theo_train_quantile outside (0, 1)."
            )
    elif want_adt_bets:
        raise ValueError(
            "Bet ADT top-patron filter requires canonical mapping + profile artifacts. "
            "Enable canonical_mapping.enabled or set filter_bets_by_adt_quantile=False."
        )

    allowed_players_pq: Path | None = None
    if want_adt_bets and args.canonical_mapping.enabled:
        allowed_players_pq = default_adt_allowed_players_parquet_path(q_thr)

    effective_bet_cfg = args.bet_preprocess
    if want_adt_bets and args.canonical_mapping.enabled:
        effective_bet_cfg = replace(
            args.bet_preprocess,
            adt_filter_quantile=q_thr,
            patron_profile_csv=profile_csv_path,
            canonical_mapping_parquet=mapping_parquet_path,
            adt_allowed_players_parquet=allowed_players_pq,
        )

    if not args.skip_bet_preprocess and pq_paths.bet_parquet.is_file():
        _ingest.validate_bet_ingress_or_raise(pq_paths)
        reg_yaml = (
            Path(args.bet_preprocess.preprocess_registry_yaml)
            if args.bet_preprocess.preprocess_registry_yaml is not None
            else _hpre.default_preprocess_registry_yaml_path()
        )
        bet_cache_ok = (
            use_preprocess_caches
            and _hpre.bet_clean_cache_is_hit(
                pq_paths.bet_parquet,
                cleaned_bet_path,
                preprocess_registry_yaml=reg_yaml,
                dedup_hash_buckets=effective_bet_cfg.dedup_hash_buckets,
                cleaned_session_parquet=cleaned_path,
                adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
            )
        )
        if bet_cache_ok:
            logger.info(
                "[Step 2b] bet clean cache hit; skip (use --ignore-caches to force): %s",
                cleaned_bet_path.resolve(),
            )
        else:
            if (
                want_adt_bets
                and args.canonical_mapping.enabled
                and allowed_players_pq is not None
            ):
                materialize_adt_allowed_players_parquet(
                    profile_csv_path,
                    mapping_parquet_path,
                    quantile=q_thr,
                    duckdb_runtime=args.duckdb_runtime,
                    output_parquet=allowed_players_pq,
                )
            out_b = _hpre.preprocess_bets_from_parquet_streaming(
                pq_paths.bet_parquet,
                cleaned_bet_path,
                cfg=effective_bet_cfg,
                duckdb_runtime=args.duckdb_runtime,
            )
            _hpre.write_bet_clean_cache_manifest(
                pq_paths.bet_parquet,
                out_b,
                preprocess_registry_yaml=reg_yaml,
                dedup_hash_buckets=effective_bet_cfg.dedup_hash_buckets,
                cleaned_session_parquet=cleaned_path,
                adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
            )
            n_b = int(pq.ParquetFile(out_b).metadata.num_rows) if pq.ParquetFile(out_b).metadata else 0
            logger.info(
                "[Step 2b] bet preprocess OK: cleaned rows=%d; written %s",
                n_b,
                out_b,
            )
    elif pq_paths.bet_parquet.is_file() and args.skip_bet_preprocess:
        logger.info("[Step 2b] bet preprocess skipped (skip_bet_preprocess=True); %s", pq_paths.bet_parquet)
    else:
        logger.info(
            "[Step 2b] no %s; skip bet preprocess",
            pq_paths.bet_parquet.name,
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
        "--ignore-caches",
        "--no-cache",
        action="store_true",
        dest="ignore_caches",
        help=(
            "Bypass preprocess disk caches (session-clean + bet-clean manifests) and recompute those steps. "
            "Heavy IO on large L0; --no-cache is an alternate spelling."
        ),
    )
    return p


def main() -> None:
    """Parse cache flag and invoke :func:`run_training` with package defaults."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ns = _build_argparser().parse_args()
    duckdb_rt, session_pre, bet_pre = configs_from_run_profile(get_run_profile(DEFAULT_RUN_PROFILE_NAME))
    args = HighTierTrainArgs(
        output_dir=Path(".data") / "trainer_hightier" / "run",
        data_dir=_ingest.default_data_dir(),
        ignore_caches=bool(ns.ignore_caches),
        duckdb_runtime=duckdb_rt,
        session_preprocess=session_pre,
        bet_preprocess=bet_pre,
    )
    run_training(args)


if __name__ == "__main__":
    main()
