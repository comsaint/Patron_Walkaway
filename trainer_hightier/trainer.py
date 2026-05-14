"""High-tier training entry (skeleton).

Analogous role to ``trainer.training.trainer.run_pipeline`` / ``main``: orchestrate
load → fit → artifact write for the reduced high-tier objective. Implementation
is intentionally empty beyond logging and call order.

Pipeline steps: ``01_data_ingest`` (inside ``prepare_training_frame``) →
``02_preprocess`` → optional walkaway labels (2c) → ``03_build_training_data`` (default on;
``--skip-training-dataset`` to skip) → ``fit_model`` / ``write_artifacts`` (TODO).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import time
from typing import Any
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
from trainer_hightier.utils.walkaway_labels import (
    default_walkaway_labels_parquet_path,
    materialize_walkaway_labels_from_cleaned_bet,
)

# Module names cannot start with a digit in ``import …`` syntax; load by full name.
_ingest = importlib.import_module("trainer_hightier.01_data_ingest")
_hpre = importlib.import_module("trainer_hightier.02_preprocess")
_hbet = importlib.import_module("trainer_hightier.utils.bet_l0_preprocess")
_b3 = importlib.import_module("trainer_hightier.03_build_training_data")


logger = logging.getLogger("trainer_hightier")


def _materialize_partition_inventory(
    *,
    manifests_dir: Path,
    correction_months: tuple[str, ...],
    backfill_month_count: int,
    previous_manifest_path: Path | None,
    snapshot_dir: Path,
) -> tuple[str | None, tuple[Path, ...], tuple[Path, ...], list[str]]:
    """Scan snapshot parquet shards → inventory JSON with fingerprint + recompute-month list."""

    from trainer_hightier.utils.partition_inventory import (
        compute_recompute_months,
        default_partition_inventory_path,
        infer_snapshot_id,
        inventory_to_manifest_dict,
        load_partition_inventory_manifest,
        scan_partition_snapshot_dir,
        write_partition_inventory_manifest,
    )

    sd = snapshot_dir.resolve()
    bet_rows, sess_rows = scan_partition_snapshot_dir(sd)
    snap_id = infer_snapshot_id(sd)
    manifest = inventory_to_manifest_dict(snap_id, snapshot_dir=sd, bet_stats=bet_rows, session_stats=sess_rows)
    fp = manifest.get("fingerprint_sha256_hex")
    fp_s = str(fp).strip() if fp is not None else None

    prev_obj: dict | None = None
    if previous_manifest_path is not None:
        pth = Path(previous_manifest_path).resolve()
        if pth.is_file():
            prev_obj = load_partition_inventory_manifest(pth)

    recompute_list = compute_recompute_months(
        current_manifest=manifest,
        previous_manifest=prev_obj,
        correction_months=correction_months,
        backfill_month_count=int(backfill_month_count),
    )
    manifests_dir.mkdir(parents=True, exist_ok=True)
    out_manifest = write_partition_inventory_manifest(
        default_partition_inventory_path(manifests_dir=manifests_dir, snapshot_id=snap_id),
        manifest,
    )
    logger.info(
        "[Step 1b] partition inventory wrote %s fingerprint=%s recompute_months=%s",
        out_manifest.resolve(),
        fp_s,
        recompute_list,
    )
    if bet_rows or sess_rows:
        logger.warning(
            "[Step 1b] Merging %d bet shard(s) + %d session shard(s); full rebuild can be RAM/IO heavy on laptops.",
            len(bet_rows),
            len(sess_rows),
        )

    bet_paths = tuple(sorted({r.path.resolve() for r in bet_rows}, key=str))
    sess_paths = tuple(sorted({r.path.resolve() for r in sess_rows}, key=str))
    return fp_s, bet_paths, sess_paths, recompute_list


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
    # After cleaned ``t_bet`` exists: join to mapping and write ``walkaway_labels.parquet`` (``trainer.labels`` parity).
    materialize_walkaway_labels: bool = True
    # Step 3: Feast historical features + labels → ``artifacts/training_data/training_set.parquet`` (default on).
    build_training_dataset: bool = True
    # When ``build_training_dataset``: materialize trial 1h + slow 180d Parquets before Feast (default on; very heavy).
    training_materialize_derived: bool = True
    # Feast feature service for Step 3 (default matches ``03_build_training_data``).
    training_feature_service: str = "walkaway_bet_trial_v1"
    # Partition snapshot folder (YYYYMM parquet shards): inventory manifest + recompute bookkeeping.
    # When ``partition_snapshot_dir`` is ``None`` and ``partition_snapshot_disable_default_dir`` is False,
    # :func:`prepare_training_frame` requires ``<repo>/data/partitions`` (raises if missing).
    partition_snapshot_dir: Path | None = None
    partition_snapshot_disable_default_dir: bool = False
    # Explicit baseline JSON for inventory diff; when ``None``, auto-pick same-snapshot manifest if present.
    partition_inventory_previous_manifest: Path | None = None
    partition_correction_months: tuple[str, ...] = ()
    partition_backfill_month_count: int = 1


def prepare_training_frame(args: HighTierTrainArgs, *, metrics: dict[str, Any] | None = None) -> None:
    """Ingest offline Parquet (presence + schema QC); full-session preprocess to cleaned table."""
    pq_paths = _ingest.resolve_local_parquet_paths(args.data_dir)
    sess_report = _ingest.validate_session_ingress_or_raise(pq_paths)
    logger.info(
        "[Step 1] t_session Parquet OK: %s rows (metadata); t_bet deferred until after session clean / downstream",
        sess_report.session.num_rows,
    )

    inv_fp: str | None = None
    bet_partition_paths: tuple[Path, ...] = ()
    session_partition_paths: tuple[Path, ...] = ()
    recompute_months: list[str] = []
    manifests_dir = Path(__file__).resolve().parent / "artifacts" / "manifests"

    from trainer_hightier.utils.partition_inventory import (
        expect_default_partition_snapshot_dir,
        expect_existing_partition_snapshot_dir,
        resolve_partition_inventory_previous_for_run,
    )

    snap_dir = args.partition_snapshot_dir
    if snap_dir is None and not args.partition_snapshot_disable_default_dir:
        snap_dir = expect_default_partition_snapshot_dir()
    elif snap_dir is not None:
        snap_dir = expect_existing_partition_snapshot_dir(snap_dir)
    baseline_used: Path | None = None
    if snap_dir is not None:
        baseline_used = resolve_partition_inventory_previous_for_run(
            manifests_dir=manifests_dir,
            snapshot_dir=snap_dir,
            explicit_previous=args.partition_inventory_previous_manifest,
        )
        logger.info(
            "[Step 1b] partition snapshot dir=%s inventory baseline=%s",
            snap_dir.resolve(),
            baseline_used.resolve() if baseline_used is not None else None,
        )
        inv_fp, bet_partition_paths, session_partition_paths, recompute_months = _materialize_partition_inventory(
            manifests_dir=manifests_dir,
            correction_months=args.partition_correction_months,
            backfill_month_count=args.partition_backfill_month_count,
            previous_manifest_path=baseline_used,
            snapshot_dir=snap_dir,
        )

    root_sess = Path(pq_paths.session_parquet).resolve()
    session_extras = tuple(p for p in session_partition_paths if Path(p).resolve() != root_sess)
    root_bet = Path(pq_paths.bet_parquet).resolve()
    bet_extras = tuple(p for p in bet_partition_paths if Path(p).resolve() != root_bet)

    cleaned_path = _hpre.default_cleaned_session_parquet_path()
    use_preprocess_caches = not args.ignore_caches
    ses_cache_ok = use_preprocess_caches and _hpre.session_clean_cache_is_hit(
        pq_paths.session_parquet,
        cleaned_path,
        dedup_hash_buckets=args.session_preprocess.dedup_hash_buckets,
        extra_source_session_parquets=session_extras or None,
        partition_inventory_fingerprint_sha256_hex=inv_fp,
    )
    if metrics is not None:
        metrics["session_clean_cache_hit"] = bool(ses_cache_ok)
        metrics["partition_inventory_fingerprint_sha256_hex"] = inv_fp
        metrics["partition_recompute_months"] = list(recompute_months)
        metrics["partition_snapshot_dir_effective"] = (
            str(snap_dir.resolve()) if snap_dir is not None else None
        )
        metrics["partition_inventory_baseline_path"] = (
            str(baseline_used.resolve()) if baseline_used is not None else None
        )
    if ses_cache_ok:
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
            extra_partition_sources=session_extras or None,
        )
        _hpre.write_session_clean_cache_manifest(
            pq_paths.session_parquet,
            out_parquet,
            dedup_hash_buckets=args.session_preprocess.dedup_hash_buckets,
            extra_source_session_parquets=session_extras or None,
            partition_inventory_fingerprint_sha256_hex=inv_fp,
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
    base_bet_path = _hbet.default_cleaned_bet_base_parquet_path()
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
        merged_bet_sources = _hbet.merge_bet_source_paths(pq_paths.bet_parquet, bet_extras or None)
        base_bet_cfg = replace(
            effective_bet_cfg,
            adt_filter_quantile=None,
            patron_profile_csv=None,
            canonical_mapping_parquet=None,
            adt_allowed_players_parquet=None,
        )
        bet_extras_arg = bet_extras or None

        if want_adt_bets and args.canonical_mapping.enabled and allowed_players_pq is not None:
            base_hit = use_preprocess_caches and _hbet.bet_base_clean_cache_is_hit(
                merged_bet_sources,
                base_bet_path,
                preprocess_registry_yaml=reg_yaml,
                dedup_hash_buckets=base_bet_cfg.dedup_hash_buckets,
                cleaned_session_parquet=cleaned_path,
                partition_inventory_fingerprint_sha256_hex=inv_fp,
            )
            seg_hit = use_preprocess_caches and _hbet.bet_clean_cache_is_hit(
                pq_paths.bet_parquet,
                cleaned_bet_path,
                preprocess_registry_yaml=reg_yaml,
                dedup_hash_buckets=effective_bet_cfg.dedup_hash_buckets,
                cleaned_session_parquet=cleaned_path,
                adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                extra_source_bet_parquets=bet_extras_arg,
                bet_base_cleaned_parquet=base_bet_path,
                partition_inventory_fingerprint_sha256_hex=inv_fp,
            )
            if metrics is not None:
                metrics["bet_base_clean_cache_hit"] = bool(base_hit)
                metrics["bet_segment_clean_cache_hit"] = bool(seg_hit)

            if base_hit and seg_hit:
                logger.info(
                    "[Step 2b] bet base+segment cache hit; skip (use --ignore-caches to force): %s",
                    cleaned_bet_path.resolve(),
                )
            else:
                materialize_adt_allowed_players_parquet(
                    profile_csv_path,
                    mapping_parquet_path,
                    quantile=q_thr,
                    duckdb_runtime=args.duckdb_runtime,
                    output_parquet=allowed_players_pq,
                )
                if not base_hit:
                    _hpre.preprocess_bets_from_parquet_streaming(
                        pq_paths.bet_parquet,
                        base_bet_path,
                        cfg=base_bet_cfg,
                        duckdb_runtime=args.duckdb_runtime,
                        extra_partition_sources=bet_extras_arg,
                    )
                    _hbet.write_bet_base_clean_cache_manifest(
                        merged_bet_sources,
                        base_bet_path,
                        preprocess_registry_yaml=reg_yaml,
                        dedup_hash_buckets=base_bet_cfg.dedup_hash_buckets,
                        cleaned_session_parquet=cleaned_path,
                        partition_inventory_fingerprint_sha256_hex=inv_fp,
                    )
                _hbet.segment_cleaned_bet_from_base_parquet(
                    base_bet_path,
                    allowed_players_pq,
                    cleaned_bet_path,
                    duckdb_runtime=args.duckdb_runtime,
                )
                _hbet.write_bet_clean_cache_manifest(
                    pq_paths.bet_parquet,
                    cleaned_bet_path,
                    preprocess_registry_yaml=reg_yaml,
                    dedup_hash_buckets=effective_bet_cfg.dedup_hash_buckets,
                    cleaned_session_parquet=cleaned_path,
                    adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                    patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                    canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                    adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                    extra_source_bet_parquets=bet_extras_arg,
                    bet_base_cleaned_parquet=base_bet_path,
                    partition_inventory_fingerprint_sha256_hex=inv_fp,
                )
                n_b = int(pq.ParquetFile(cleaned_bet_path).metadata.num_rows) if pq.ParquetFile(
                    cleaned_bet_path
                ).metadata else 0
                logger.info(
                    "[Step 2b] bet preprocess OK (base+ADT segment): cleaned rows=%d; written %s",
                    n_b,
                    cleaned_bet_path,
                )
        else:
            bet_cache_ok = use_preprocess_caches and _hbet.bet_clean_cache_is_hit(
                pq_paths.bet_parquet,
                cleaned_bet_path,
                preprocess_registry_yaml=reg_yaml,
                dedup_hash_buckets=effective_bet_cfg.dedup_hash_buckets,
                cleaned_session_parquet=cleaned_path,
                adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                extra_source_bet_parquets=bet_extras_arg,
                partition_inventory_fingerprint_sha256_hex=inv_fp,
            )
            if metrics is not None:
                metrics["bet_clean_cache_hit"] = bool(bet_cache_ok)
            if bet_cache_ok:
                logger.info(
                    "[Step 2b] bet clean cache hit; skip (use --ignore-caches to force): %s",
                    cleaned_bet_path.resolve(),
                )
            else:
                out_b = _hpre.preprocess_bets_from_parquet_streaming(
                    pq_paths.bet_parquet,
                    cleaned_bet_path,
                    cfg=effective_bet_cfg,
                    duckdb_runtime=args.duckdb_runtime,
                    extra_partition_sources=bet_extras_arg,
                )
                _hbet.write_bet_clean_cache_manifest(
                    pq_paths.bet_parquet,
                    out_b,
                    preprocess_registry_yaml=reg_yaml,
                    dedup_hash_buckets=effective_bet_cfg.dedup_hash_buckets,
                    cleaned_session_parquet=cleaned_path,
                    adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                    patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                    canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                    adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                    extra_source_bet_parquets=bet_extras_arg,
                    partition_inventory_fingerprint_sha256_hex=inv_fp,
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

    if (
        args.materialize_walkaway_labels
        and not args.skip_bet_preprocess
        and pq_paths.bet_parquet.is_file()
        and cleaned_bet_path.is_file()
    ):
        if not mapping_parquet_path.is_file():
            logger.warning(
                "[Step 2c] skip walkaway labels: canonical mapping missing at %s",
                mapping_parquet_path,
            )
        else:
            labels_out = args.objective.labels_parquet or default_walkaway_labels_parquet_path()
            materialize_walkaway_labels_from_cleaned_bet(
                cleaned_bet_parquet=cleaned_bet_path,
                canonical_mapping_parquet=mapping_parquet_path,
                out_parquet=labels_out,
                duckdb_runtime=args.duckdb_runtime,
            )
            logger.info("[Step 2c] walkaway labels written %s", labels_out.resolve())


def _maybe_build_training_dataset(args: HighTierTrainArgs) -> None:
    """Step 3: Feast + labels training Parquet (see ``03_build_training_data``); default on."""
    if not args.build_training_dataset:
        return
    cleaned_bet_path = _hpre.default_cleaned_bet_parquet_path()
    if not cleaned_bet_path.is_file():
        logger.warning(
            "[Step 3] skip training dataset: cleaned bet missing at %s",
            cleaned_bet_path.resolve(),
        )
        return
    labels_path = args.objective.labels_parquet or default_walkaway_labels_parquet_path()
    if not labels_path.is_file():
        logger.warning(
            "[Step 3] skip training dataset: labels missing at %s (enable walkaway labels or materialize separately)",
            labels_path.resolve(),
        )
        return
    feast_repo = Path(__file__).resolve().parent / "feast_repo"
    registry = feast_repo / "data" / "registry.db"
    if not registry.is_file():
        logger.warning(
            "[Step 3] skip training dataset: Feast registry missing at %s (run ``feast apply`` under feast_repo)",
            registry.resolve(),
        )
        return
    cfg = _b3.BuildTrainingDataArgs(
        feast_repo=feast_repo.resolve(),
        cleaned_bet_parquet=cleaned_bet_path.resolve(),
        labels_parquet=labels_path.resolve(),
        output_parquet=_b3.DEFAULT_OUTPUT.resolve(),
        feature_service_name=args.training_feature_service,
        materialize_derived_features=args.training_materialize_derived,
        max_entity_rows=None,
        duckdb_runtime=args.duckdb_runtime,
        feast_entity_batch_by_calendar_month=True,
        training_set_keep_last_n_versions=10,
    )
    out = _b3.build_training_data(cfg)
    logger.info("[Step 3] training dataset written %s", out)


def fit_model(args: HighTierTrainArgs) -> None:
    """Train a scorer on the high-tier slice (TODO)."""
    logger.info("[Step 5] fit_model: not implemented (seed=%s)", args.random_seed)


def write_artifacts(args: HighTierTrainArgs) -> None:
    """Persist model bundle + minimal metrics sidecars (TODO)."""
    logger.info("[Step 6] write_artifacts: not implemented")


def run_training(args: HighTierTrainArgs) -> None:
    """Run the high-tier training pipeline in order."""
    args.output_dir.mkdir(parents=True, exist_ok=True)
    metrics: dict[str, Any] = {
        "start_epoch_ms": int(time.time() * 1000),
    }
    t_prep = time.perf_counter()
    prepare_training_frame(args, metrics=metrics)
    metrics["prepare_training_frame_seconds"] = round(time.perf_counter() - t_prep, 3)
    t_step3 = time.perf_counter()
    _maybe_build_training_dataset(args)
    metrics["build_training_dataset_seconds"] = round(time.perf_counter() - t_step3, 3)
    fit_model(args)
    write_artifacts(args)
    metrics["finish_epoch_ms"] = int(time.time() * 1000)
    rp = Path(args.output_dir) / "run_report.json"
    rp.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    logger.info("[Step 7] run_training skeleton finished (report %s)", rp.resolve())


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
    p.add_argument(
        "--skip-walkaway-labels",
        action="store_true",
        dest="skip_walkaway_labels",
        help="Do not materialize walkaway_labels.parquet after cleaned t_bet (large pandas pass).",
    )
    p.add_argument(
        "--skip-training-dataset",
        action="store_true",
        dest="skip_training_dataset",
        help=(
            "Do not run Step 3 (Feast + labels → artifacts/training_data/training_set.parquet). "
            "Default is to build the training set after preprocess."
        ),
    )
    p.add_argument(
        "--skip-training-materialize-derived",
        action="store_true",
        dest="skip_training_materialize_derived",
        help=(
            "With Step 3 enabled: do not re-materialize trial 1h + slow 180d Parquets before Feast "
            "(faster when those files are already up to date)."
        ),
    )
    p.add_argument(
        "--no-partition-snapshot",
        action="store_true",
        dest="no_partition_snapshot",
        help=(
            "Do not merge monthly shard Parquets: do not use <repo>/data/partitions or "
            "--partition-snapshot-dir. For monolith-only runs (gmwds_t_*.parquet only)."
        ),
    )
    p.add_argument(
        "--partition-snapshot-dir",
        type=Path,
        default=None,
        help=(
            "Folder with t_bet__part_* / t_session__part_* monthly Parquets. "
            "When omitted (and not --no-partition-snapshot), <repo>/data/partitions must exist or the run fails. "
            "When set, this directory is used instead of the default path."
        ),
    )
    p.add_argument(
        "--partition-inventory-previous",
        type=Path,
        default=None,
        help=(
            "Optional explicit partition_inventory_*.json for diff-based recompute months. "
            "When omitted, uses trainer_hightier/artifacts/manifests/partition_inventory_<snapshot_id>.json "
            "if it already exists (same snapshot folder name as last run)."
        ),
    )
    p.add_argument(
        "--partition-correction-month",
        dest="partition_correction_months",
        action="append",
        default=[],
        metavar="YYYYMM",
        help="Extra YYYYMM to force into partition recompute set (repeatable).",
    )
    p.add_argument(
        "--partition-backfill-count",
        type=int,
        default=1,
        metavar="N",
        help="Include N preceding calendar months for each touched month (default 1).",
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
    corr = tuple(str(x).strip() for x in (ns.partition_correction_months or []) if str(x).strip())
    args = HighTierTrainArgs(
        output_dir=Path(".data") / "trainer_hightier" / "run",
        data_dir=_ingest.default_data_dir(),
        ignore_caches=bool(ns.ignore_caches),
        materialize_walkaway_labels=not bool(ns.skip_walkaway_labels),
        build_training_dataset=not bool(ns.skip_training_dataset),
        training_materialize_derived=not bool(ns.skip_training_materialize_derived),
        duckdb_runtime=duckdb_rt,
        session_preprocess=session_pre,
        bet_preprocess=bet_pre,
        partition_snapshot_dir=Path(ns.partition_snapshot_dir).resolve() if ns.partition_snapshot_dir else None,
        partition_snapshot_disable_default_dir=bool(ns.no_partition_snapshot),
        partition_inventory_previous_manifest=(
            Path(ns.partition_inventory_previous).resolve() if ns.partition_inventory_previous else None
        ),
        partition_correction_months=corr,
        partition_backfill_month_count=int(ns.partition_backfill_count),
    )
    run_training(args)


if __name__ == "__main__":
    main()
