"""High-tier training entry: partition ingest → preprocess → Feast training set → splits → LightGBM.

Analogous role to ``trainer.training.trainer.run_pipeline`` / ``main``: orchestrate
load → fit → artifact write for the reduced high-tier objective.

Pipeline steps: ``01_data_ingest`` (inside ``prepare_training_frame``) →
``02_preprocess`` → optional walkaway labels (2c) → ``03_build_training_data`` (default on;
``--skip-training-dataset`` to skip) → ``04_split_dataset`` (default on; ``--skip-step4`` to skip)
→ Step 5 ``fit_model`` (LightGBM + optional Optuna; ``--skip-step5`` / ``--skip-optuna``).
``--skip-bet-preprocess`` skips Step 2b (reuse existing cleaned bet artifacts).
Use ``--start-from-features`` to run only Step 4 on an existing training parquet.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Final
from dataclasses import dataclass, field, replace
from pathlib import Path

import pyarrow.parquet as pq

from trainer_hightier.feature_experiment.candidate_registry_loader import (
    baseline_features_for_main_trainer,
    default_registry_path,
    load_candidate_registry,
    load_registry_raw_feature_dicts,
)
from trainer_hightier.feature_experiment.feature_cadence import classify_model_fe_features
from trainer_hightier.config import (
    BetPreprocessConfig,
    CanonicalMappingConfig,
    DEFAULT_DEPLOY_OUTPUT_ROOT,
    DEFAULT_MODEL_DIR,
    DEFAULT_RANDOM_SEED,
    FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME,
    DEFAULT_RUN_PROFILE_NAME,
    DEFAULT_TRAINING_FEATURE_SERVICE,
    DEFAULT_TRAINING_SHORT_TERM_MATERIALIZE_BATCH_SIZE,
    DEFAULT_STEP35_MISS_PATH,
    SHORT_TERM_TRIAL_BET_COLUMNS,
    TRAINING_SHORT_TERM_PIT_CACHE_BASENAME,
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    MLFLOW_EXPERIMENT_TRAIN_HIGHTIER,
    MLFLOW_HIGHTIER_ARTIFACT_PREFIX,
    MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS,
    MID_TERM_SNAPSHOT_SCOPE_TRAINING,
    PartitionIngressConfig,
    PreTrainFeatureGateConfig,
    PreTrainFeatureGateConfig,
    Step5TrainConfig,
    Step6ParityConfig,
    SessionPreprocessConfig,
    Step4SplitConfig,
    SamplePolicy,
    FeatureScreeningPolicy,
    TrainingScopePolicy,
    TrainingRunKind,
    TRAINING_RUN_KIND_SPEED,
    DATA_COMPLETENESS_MODE_STRICT,
    ResolvedTrainingScope,
    TrainingDataScopeConfig,
    feature_selection_policy_fingerprint,
    resolve_training_scope,
    sample_policy_fingerprint,
    training_scope_policy_fingerprint,
    validate_sample_policy_for_run,
    validate_feature_screening_policy,
    configs_from_run_profile,
    get_run_profile,
    list_run_profile_names,
)
from trainer_hightier.core.mlflow_adapter import (
    log_artifact_safe,
    log_metrics_safe,
    log_params_safe,
    log_tags_safe,
    safe_start_run,
    warm_up_mlflow_run_safe,
)
from trainer_hightier.core.model_bundle_paths import (
    DEPLOY_E2E_GATE_REPORT_FILENAME,
    FEATURE_PARITY_REPORT_FILENAME,
    model_bundle_report_path,
    safe_version_subdirectory,
    write_latest_model_manifest,
)
from trainer_hightier.reporting.writer import BundleReportWriter
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
from trainer_hightier.utils.slow_patron_180d_monthly import (
    default_slow_patron_180d_monthly_parquet_path,
)
from trainer_hightier.utils.walkaway_labels import default_walkaway_labels_parquet_path

# Subdirectory beside Step 5 bundle with Frozen snapshot + manifest for offline packaging.
_DEPLOY_INPUTS_DIRNAME = "deploy_inputs"

# Module names cannot start with a digit in ``import …`` syntax; load by full name.
_ingest = importlib.import_module("trainer_hightier.01_data_ingest")
_hpre = importlib.import_module("trainer_hightier.02_preprocess")
_hbet = importlib.import_module("trainer_hightier.utils.bet_l0_preprocess")
_b3 = importlib.import_module("trainer_hightier.03_build_training_data")
_b4 = importlib.import_module("trainer_hightier.04_split_dataset")
_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")


logger = logging.getLogger("trainer_hightier")

_STEP5_CONFIG_DEFAULTS = Step5TrainConfig()
_STEP6_CONFIG_DEFAULTS = Step6ParityConfig()
_PRE_TRAIN_GATE_DEFAULTS = PreTrainFeatureGateConfig()
_PRE_TRAIN_GATE_DEFAULTS = PreTrainFeatureGateConfig()
_PARTITION_INGRESS_DEFAULTS = PartitionIngressConfig()


def _materialize_partition_inventory(
    *,
    manifests_dir: Path,
    previous_manifest_path: Path | None,
    snapshot_dir: Path,
) -> tuple[str | None, tuple[Path, ...], tuple[Path, ...], list[Any], list[Any], dict[str, Any]]:
    """Scan snapshot parquet shards → inventory JSON with fingerprint (diagnostic)."""

    from trainer_hightier.utils.partition_inventory import (
        default_partition_inventory_path,
        infer_snapshot_id,
        inventory_to_manifest_dict,
        scan_partition_snapshot_dir,
        write_partition_inventory_manifest,
    )

    sd = snapshot_dir.resolve()
    bet_rows, sess_rows = scan_partition_snapshot_dir(sd)
    snap_id = infer_snapshot_id(sd)
    manifest = inventory_to_manifest_dict(snap_id, snapshot_dir=sd, bet_stats=bet_rows, session_stats=sess_rows)
    fp = manifest.get("fingerprint_sha256_hex")
    fp_s = str(fp).strip() if fp is not None else None

    manifests_dir.mkdir(parents=True, exist_ok=True)
    out_manifest = write_partition_inventory_manifest(
        default_partition_inventory_path(manifests_dir=manifests_dir, snapshot_id=snap_id),
        manifest,
    )
    logger.info(
        "[Step 1b] partition inventory wrote %s fingerprint=%s",
        out_manifest.resolve(),
        fp_s,
    )
    if bet_rows or sess_rows:
        logger.warning(
            "[Step 1b] Merging %d bet shard(s) + %d session shard(s); full rebuild can be RAM/IO heavy on laptops.",
            len(bet_rows),
            len(sess_rows),
        )

    bet_paths = tuple(sorted({r.path.resolve() for r in bet_rows}, key=str))
    sess_paths = tuple(sorted({r.path.resolve() for r in sess_rows}, key=str))
    return fp_s, bet_paths, sess_paths, bet_rows, sess_rows, manifest


@dataclass
class HighTierTrainArgs:
    """Programmatic run configuration for :func:`run_training` (defaults + optional overrides)."""

    #: Versioned bundle parent (default `<repo>/out/models_high_tier_mvp`).
    #: ``run_training`` may set ``step5_bundle_dir`` to ``output_dir /<model_version>/``.
    output_dir: Path
    #: When set (by :func:`run_training`), Step 5 writes ``model.pkl`` here; otherwise Step 5 uses ``output_dir`` flat (e.g. feature experiment scratch dirs).
    step5_bundle_dir: Path | None = None
    random_seed: int = DEFAULT_RANDOM_SEED
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
    # When ``build_training_dataset``: materialize slow 180d Parquet before Feast (default on; trial 1h skipped).
    training_materialize_derived: bool = True
    # Feast feature service for Step 3 (default matches ``03_build_training_data``).
    training_feature_service: str = DEFAULT_TRAINING_FEATURE_SERVICE
    # When False: skip month×group Feast disk cache in Step 3 (always full retrieval per month).
    feast_retrieval_cache: bool = True
    # When Step 3: if feast_repo/registry.db missing, run `feast apply` in feast_repo first (default on).
    auto_feast_apply: bool = True
    # Partition snapshot folder (YYYYMM parquet shards): inventory manifest + recompute bookkeeping.
    # When ``None``, defaults to ``<repo>/data/partitions`` and must exist.
    partition_snapshot_dir: Path | None = None
    # Explicit baseline JSON for inventory diff; when ``None``, auto-pick same-snapshot manifest if present.
    partition_inventory_previous_manifest: Path | None = None
    partition_correction_months: tuple[str, ...] = ()
    partition_backfill_month_count: int = _PARTITION_INGRESS_DEFAULTS.backfill_month_count
    #: When True (default): Step 3 / 3.5 read entity set v1 instead of ADT-segmented cleaned bet.
    use_entity_set_v1: bool = True
    #: When True: Step 2c labels use month×canonical_shard cache (default off until smoke sign-off).
    use_sharded_labels_cache: bool = False
    # Step 4: deterministic arrange + time split on ``gaming_day`` (after Step 3 or --start-from-features).
    run_step4: bool = True
    step4_split: Step4SplitConfig = field(default_factory=Step4SplitConfig)
    # When True: skip Step 1-3; require an existing training features parquet and run Step 4 only.
    start_from_features: bool = False
    features_input_parquet: Path | None = None
    # Step 5: LightGBM + optional Optuna on Step 4 split Parquets.
    step5: Step5TrainConfig = field(default_factory=Step5TrainConfig)
    # Step 4.5: short-term PIT train/serve gate before Step 5 (after Step 4 splits).
    pre_train_gate: PreTrainFeatureGateConfig = field(default_factory=PreTrainFeatureGateConfig)
    # Step 6: train/serve parity verification after Step 5 bundle materialization.
    step6: Step6ParityConfig = field(default_factory=Step6ParityConfig)
    # Feature ledger for Step 5 baseline columns; ``None`` uses default contracts YAML path.
    feature_candidate_registry: Path | None = None
    #: When True: bypass short-term PIT month-shard cache and recompute all shards.
    force_refresh_short_term_pit: bool = False
    #: Offline Step 3.5 batch size (decoupled from scorer ``hightier_scorer_max_bets_per_cycle``).
    training_short_term_materialize_batch_size: int = DEFAULT_TRAINING_SHORT_TERM_MATERIALIZE_BATCH_SIZE
    #: Step 3.5 cache miss materializer (``indexed_replay`` default; fail-fast, no auto fallback).
    step35_miss_path: str = DEFAULT_STEP35_MISS_PATH
    #: ``--run-profile`` CLI name (MLflow param); programmatic callers default to :data:`DEFAULT_RUN_PROFILE_NAME`.
    run_profile_name: str = DEFAULT_RUN_PROFILE_NAME
    #: Explicit patron sampling ratio for logs (e.g. ``0.01`` vs ``0.10``). When ``None`` and ADT bet filter is on,
    #: :func:`build_run_summary` derives approximate segment fraction as ``1 - objective.theo_train_quantile``.
    patron_sampling_ratio: float | None = None
    #: Drop training rows outside inclusive ``gaming_day_event`` bounds before Step 3.5 / split.
    training_data_scope: TrainingDataScopeConfig = field(default_factory=TrainingDataScopeConfig)
    #: Target horizon / completeness policy (SSOT Training Acceleration).
    training_scope_policy: TrainingScopePolicy = field(default_factory=TrainingScopePolicy)
    #: Train-only negative downsampling (Step 4 → Step 5 layer).
    sample_policy: SamplePolicy = field(default_factory=SamplePolicy)
    #: Optional feature screening hook (default off).
    feature_screening_policy: FeatureScreeningPolicy = field(default_factory=FeatureScreeningPolicy)
    #: ``release_promoted`` forbids ``neg_sample_frac < 1.0``.
    training_run_kind: TrainingRunKind = TRAINING_RUN_KIND_SPEED


def _repo_root() -> Path:
    """Return repository root (parent of ``trainer_hightier`` package directory)."""

    return Path(__file__).resolve().parents[1]


def _step4_gaming_day_periods_from_report(report: dict[str, Any]) -> dict[str, Any] | None:
    """Extract train/val/test ``gaming_day`` date ranges from Step 4 report (``split_report.json`` body)."""

    splits = report.get("splits")
    if not isinstance(splits, list):
        return None
    by_split: dict[str, dict[str, Any]] = {}
    for row in splits:
        if not isinstance(row, dict):
            continue
        tag = str(row.get("split") or "").strip().lower()
        if tag not in ("train", "val", "test"):
            continue
        by_split[tag] = {
            "min_gaming_day": row.get("min_gaming_day"),
            "max_gaming_day": row.get("max_gaming_day"),
            "row_count": row.get("row_count"),
        }
    if not by_split:
        return None
    return {
        "basis": "gaming_day_event",
        "train_day_fraction": report.get("train_day_fraction"),
        "val_day_fraction": report.get("val_day_fraction"),
        "distinct_gaming_days": report.get("distinct_gaming_days"),
        "by_split": by_split,
    }


def _get_report_writer(metrics: dict[str, Any]) -> BundleReportWriter | None:
    """Return the active :class:`BundleReportWriter` when attached to *metrics*."""

    writer = metrics.get("report_writer")
    return writer if isinstance(writer, BundleReportWriter) else None


def _finalize_training_reports(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
    writer: BundleReportWriter,
    *,
    status: str,
    error: str | None = None,
) -> Path:
    """Write nested ``run_report.json`` via the bundle report writer."""

    path = writer.finalize(metrics, args, status=status, error=error)
    logger.info("[Step 7] wrote %s status=%s", path.resolve(), status)
    return path


def _git_short_head(repo_root: Path) -> str:
    """Return ``git rev-parse --short HEAD`` or ``nogit``."""

    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "nogit"


def _mlflow_hightier_run_name(*, repo_root: Path) -> str:
    """Build MLflow ``run_name``: ``YYYYMMDD-HHMMSS-<git_short>``."""

    ts = time.strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{_git_short_head(repo_root)}"


def _mlflow_initial_string_params(args: HighTierTrainArgs) -> dict[str, str]:
    """Scalar string params logged at run start (bounded; details live in ``run_report.json``)."""

    reg = args.feature_candidate_registry or default_registry_path()
    out = {
        "pipeline": "trainer_hightier",
        "run_profile": str(args.run_profile_name),
        "random_seed": str(int(args.random_seed)),
        "objective_min_precision": str(float(args.objective.min_precision)),
        "step5_skip_optuna": str(bool(args.step5.skip_optuna)),
        "step5_run_step5": str(bool(args.step5.run_step5)),
        "run_step4": str(bool(args.run_step4)),
        "start_from_features": str(bool(args.start_from_features)),
        "feature_candidate_registry_path": str(Path(reg).resolve())[:500],
        "git_commit": _git_short_head(_repo_root()),
        "filter_bets_by_adt_quantile": str(bool(args.filter_bets_by_adt_quantile)),
        "objective_theo_train_quantile": str(float(args.objective.theo_train_quantile)),
    }
    if args.patron_sampling_ratio is not None:
        out["patron_sampling_ratio"] = str(float(args.patron_sampling_ratio))[:64]
    out["training_run_kind"] = str(args.training_run_kind)
    out["recent_full_months"] = str(args.training_scope_policy.recent_full_months)
    out["neg_sample_frac"] = str(float(args.sample_policy.neg_sample_frac))
    return out


def _mlflow_post_run_string_params(metrics: dict[str, Any]) -> dict[str, str]:
    """Append bounded lineage params after training (partition/registry echoes)."""

    out: dict[str, str] = {}
    cr = metrics.get("candidate_registry")
    if isinstance(cr, dict):
        rv = cr.get("registry_version")
        if rv is not None:
            out["candidate_registry_version"] = str(rv)[:200]
        rp = cr.get("resolved_path")
        if rp is not None:
            out["candidate_registry_resolved_path"] = str(rp)[:500]
        nb = cr.get("n_baseline_features")
        if nb is not None:
            out["candidate_registry_n_baseline_features"] = str(nb)
    fp = metrics.get("partition_inventory_fingerprint_sha256_hex")
    if fp is not None and str(fp).strip():
        out["partition_inventory_fingerprint_sha256_hex"] = str(fp).strip()[:128]
    snap = metrics.get("partition_snapshot_dir_effective")
    if snap is not None:
        out["partition_snapshot_dir_effective"] = str(snap)[:500]
    mv_out = metrics.get("model_version")
    if mv_out is not None and str(mv_out).strip():
        out["model_version"] = str(mv_out).strip()[:128]
    mbd = metrics.get("model_bundle_dir")
    if mbd is not None and str(mbd).strip():
        out["model_bundle_dir"] = str(mbd).strip()[:500]
    return out


def _mlflow_scalar_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Flatten top-level numeric metrics and Optuna best-params for ``log_metrics_safe``."""

    skip = frozenset(
        {
            "candidate_registry",
            "feast_auto_apply",
            "partition_recompute_months",
            "model_bundle_dir",
            "model_version",
        },
    )
    out: dict[str, Any] = {}
    for k, v in metrics.items():
        if k in skip or isinstance(v, (dict, list)):
            continue
        out[k] = v
    obp = metrics.get("optuna_best_params")
    if isinstance(obp, dict):
        for pk, pv in obp.items():
            key = f"optuna_best_{pk}"
            try:
                out[key] = float(pv)
            except (TypeError, ValueError):
                continue
    rem = metrics.get("partition_recompute_months")
    if isinstance(rem, list):
        out["partition_recompute_months_count"] = float(len(rem))
    return out


def _log_mlflow_whitelist_artifacts(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
    writer: BundleReportWriter | None = None,
) -> None:
    """Log registered JSON artifacts and ``model.pkl`` under :data:`MLFLOW_HIGHTIER_ARTIFACT_PREFIX`."""

    prefix = MLFLOW_HIGHTIER_ARTIFACT_PREFIX
    active_writer = writer or _get_report_writer(metrics)
    if active_writer is not None:
        for path in active_writer.registered_json_paths():
            if path.is_file():
                log_artifact_safe(path, artifact_path=prefix)
    smp_model = metrics.get("model_path") or metrics.get("step5_model_path")
    if smp_model:
        pm = Path(str(smp_model))
        if pm.is_file():
            log_artifact_safe(pm, artifact_path=prefix)


def _resolve_splits_dir(args: HighTierTrainArgs) -> Path:
    """Directory with ``train.parquet`` / ``val.parquet`` / ``test.parquet`` (Step 4)."""

    if args.step4_split.splits_output_dir is not None:
        return Path(args.step4_split.splits_output_dir).resolve()
    return _b4.default_splits_output_dir().resolve()


def _step5_splits_have_feature_columns(
    splits_dir: Path,
    feature_columns: tuple[str, ...],
    *,
    registry_path: Path,
) -> None:
    """Raise if ``train.parquet`` exists but lacks any baseline column from the registry."""

    train_p = Path(splits_dir).resolve() / "train.parquet"
    if not train_p.is_file():
        return
    names = frozenset(pq.ParquetFile(train_p).schema_arrow.names)
    missing = sorted(set(feature_columns) - names)
    if missing:
        raise ValueError(
            "Step 5 cannot load training split: Parquet is missing baseline columns "
            f"{missing} selected from registry {registry_path}. "
            "Regenerate Step 3/4 so these columns exist, or adjust the registry.",
        )


def prepare_training_frame(args: HighTierTrainArgs, *, metrics: dict[str, Any] | None = None) -> None:
    """Ingest partition shards (schema QC) then build cleaned session/bet artifacts."""
    from trainer_hightier.utils.partition_inventory import (
        collapsed_preprocess_read_sources,
        expect_default_partition_snapshot_dir,
        expect_existing_partition_snapshot_dir,
        resolve_partition_inventory_previous_for_run,
    )

    snap_dir: Path = (
        expect_default_partition_snapshot_dir()
        if args.partition_snapshot_dir is None
        else expect_existing_partition_snapshot_dir(args.partition_snapshot_dir)
    )

    inv_fp: str | None = None
    bet_partition_paths: tuple[Path, ...] = ()
    session_partition_paths: tuple[Path, ...] = ()
    recompute_months: list[str] = []
    manifests_dir = Path(__file__).resolve().parent / "artifacts" / "manifests"

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
    (
        inv_fp,
        bet_partition_paths,
        session_partition_paths,
        bet_inventory_rows,
        session_inventory_rows,
        partition_manifest,
    ) = _materialize_partition_inventory(
        manifests_dir=manifests_dir,
        previous_manifest_path=baseline_used,
        snapshot_dir=snap_dir,
    )

    from trainer_hightier.utils.cache_invalidation_v1 import (
        RECOMPUTE_SOURCE_V2,
        compute_l1_recompute_months,
    )
    from trainer_hightier.utils.partition_inventory import (
        compute_recompute_months,
        load_partition_inventory_manifest,
        partition_month_union_from_manifest,
    )
    from trainer_hightier.utils.source_manifest_v2 import materialize_source_manifest_v2_phase1

    sm_v2_meta = materialize_source_manifest_v2_phase1(
        snapshot_dir=snap_dir,
        bet_stats=bet_inventory_rows,
        session_stats=session_inventory_rows,
    )
    available_months = partition_month_union_from_manifest(partition_manifest)
    recompute_months = compute_l1_recompute_months(
        changed_partitions=sm_v2_meta["source_manifest_v2_changed_partitions"],
        correction_months=args.partition_correction_months,
        backfill_month_count=args.partition_backfill_month_count,
        available_months=available_months,
    )
    prev_inv: dict[str, Any] | None = None
    if baseline_used is not None and Path(baseline_used).is_file():
        prev_inv = load_partition_inventory_manifest(Path(baseline_used))
    legacy_recompute = compute_recompute_months(
        current_manifest=partition_manifest,
        previous_manifest=prev_inv,
        correction_months=args.partition_correction_months,
        backfill_month_count=args.partition_backfill_month_count,
    )
    if legacy_recompute != recompute_months:
        logger.info(
            "[Step 1b] l1_recompute_months source=%s months=%s "
            "(inventory_mtime_legacy=%s; scheduling uses source_manifest_v2 only)",
            RECOMPUTE_SOURCE_V2,
            recompute_months,
            legacy_recompute,
        )
    else:
        logger.info(
            "[Step 1b] l1_recompute_months source=%s months=%s",
            RECOMPUTE_SOURCE_V2,
            recompute_months,
        )
    source_fp = str(sm_v2_meta.get("source_manifest_v2_aggregate_fingerprint_sha256_hex") or "").strip() or None
    if metrics is not None:
        metrics.update(sm_v2_meta)
        metrics["l1_recompute_months"] = list(recompute_months)
        metrics["l1_recompute_months_source"] = RECOMPUTE_SOURCE_V2
        metrics["partition_recompute_months_inventory_legacy"] = list(legacy_recompute)
        metrics["partition_recompute_months"] = list(recompute_months)

    sess_report = _ingest.validate_partition_session_ingress_or_raise(session_partition_paths)
    logger.info(
        "[Step 1] t_session partition shards OK: %d file(s), %s rows (metadata); "
        "t_bet deferred until after session clean / downstream",
        len(session_partition_paths),
        sess_report.session.num_rows,
    )

    ordered_sess = tuple(sorted((Path(p).resolve() for p in session_partition_paths), key=str))
    session_cache_primary = ordered_sess[0] if ordered_sess else None
    session_cache_extras = ordered_sess[1:] if ordered_sess else ()
    session_read = collapsed_preprocess_read_sources(
        snapshot_dir=snap_dir,
        table_name="t_session",
        legacy_paths=ordered_sess,
    )
    if not session_read:
        raise FileNotFoundError(f"no session preprocess sources under snapshot {snap_dir}")
    session_preprocess_primary = session_read[0]
    session_preprocess_extras = session_read[1:]

    cleaned_path = _hpre.default_cleaned_session_parquet_path()
    use_preprocess_caches = not args.ignore_caches
    ses_cache_ok = (
        session_cache_primary is not None
        and use_preprocess_caches
        and _hpre.session_clean_cache_is_hit(
            session_cache_primary,
            cleaned_path,
            dedup_hash_buckets=args.session_preprocess.dedup_hash_buckets,
            extra_source_session_parquets=session_cache_extras or None,
            source_manifest_v2_fingerprint_sha256_hex=source_fp,
            data_scope=args.session_preprocess.data_scope,
        )
    )
    if metrics is not None:
        metrics["session_clean_cache_hit"] = bool(ses_cache_ok)
        metrics["l0_preprocess_data_scope"] = args.session_preprocess.data_scope.manifest_block()
        metrics["partition_inventory_fingerprint_sha256_hex"] = inv_fp
        metrics["partition_snapshot_dir_effective"] = str(snap_dir.resolve())
        metrics["partition_inventory_baseline_path"] = (
            str(baseline_used.resolve()) if baseline_used is not None else None
        )
    if ses_cache_ok:
        logger.info(
            "[Step 2] session clean cache hit; skip preprocess (use --ignore-caches to force): %s",
            cleaned_path.resolve(),
        )
    else:
        out_parquet, session_dedup_effective = _hpre.preprocess_sessions_from_parquet_streaming(
            session_preprocess_primary,
            cleaned_path,
            cfg=args.session_preprocess,
            duckdb_runtime=args.duckdb_runtime,
            extra_partition_sources=session_preprocess_extras or None,
        )
        if metrics is not None:
            metrics["session_dedup_hash_buckets_effective"] = int(session_dedup_effective)
        _hpre.write_session_clean_cache_manifest(
            session_cache_primary,
            out_parquet,
            dedup_hash_buckets=int(session_dedup_effective),
            extra_source_session_parquets=session_cache_extras or None,
            source_manifest_v2_fingerprint_sha256_hex=source_fp,
            data_scope=args.session_preprocess.data_scope,
        )
        n_clean = int(pq.ParquetFile(out_parquet).metadata.num_rows) if pq.ParquetFile(out_parquet).metadata else 0
        logger.info(
            "[Step 2] session preprocess OK (gaming_day_event scope=%s): cleaned rows=%d; written %s",
            args.session_preprocess.data_scope.manifest_block(),
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
        and bool(bet_partition_paths)
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

    rank_meta: dict[str, Any] | None = None
    selected_universe_fp: str | None = None
    if args.canonical_mapping.enabled and profile_csv_path.is_file() and mapping_parquet_path.is_file():
        from datetime import date as _date_cls

        from trainer_hightier.config import default_hightier_serving_config
        from trainer_hightier.utils.slow_month_turn import resolve_slow_month_turn_context
        from trainer_hightier.utils.universe_cache_v1 import (
            materialize_adt_rank_table_v1_cached,
            write_selected_universe_manifest,
        )

        _rank_slow_ctx = resolve_slow_month_turn_context(_date_cls.today())
        _rank_svc_cfg = default_hightier_serving_config()
        rank_meta = materialize_adt_rank_table_v1_cached(
            patron_profile_csv=profile_csv_path,
            canonical_mapping_parquet=mapping_parquet_path,
            duckdb_runtime=args.duckdb_runtime,
            cleaned_session_parquet=cleaned_path if cleaned_path.is_file() else None,
            slow_active_anchor=_rank_slow_ctx.slow_anchor_required,
            slow_lookback_days=int(_rank_svc_cfg.production_slow_lookback_days),
        )
        if metrics is not None:
            metrics.update(rank_meta)
        if want_adt_bets and 0.0 < q_thr < 1.0:
            sel_meta = write_selected_universe_manifest(
                rank_table_path=Path(str(rank_meta["universe_adt_rank_table_path"])),
                quantile=q_thr,
                rank_fingerprint_sha256_hex=str(rank_meta["universe_adt_rank_fingerprint_sha256_hex"]),
            )
            selected_universe_fp = str(sel_meta["selected_universe_fingerprint_sha256_hex"])
            if metrics is not None:
                metrics.update(sel_meta)

    effective_bet_cfg = args.bet_preprocess
    if want_adt_bets and args.canonical_mapping.enabled:
        effective_bet_cfg = replace(
            args.bet_preprocess,
            adt_filter_quantile=q_thr,
            patron_profile_csv=profile_csv_path,
            canonical_mapping_parquet=mapping_parquet_path,
            adt_allowed_players_parquet=allowed_players_pq,
        )

    entity_set_policy_fp: str | None = None

    if not args.skip_bet_preprocess and bet_partition_paths:
        bet_report = _ingest.validate_partition_bet_ingress_or_raise(bet_partition_paths)
        logger.info(
            "[Step 1] t_bet partition shards OK: %d file(s), %s rows (metadata)",
            len(bet_partition_paths),
            bet_report.num_rows,
        )
        ordered_bets = tuple(sorted((Path(p).resolve() for p in bet_partition_paths), key=str))
        bet_cache_primary = ordered_bets[0]
        bet_cache_extras = ordered_bets[1:]
        bet_read = collapsed_preprocess_read_sources(
            snapshot_dir=snap_dir,
            table_name="t_bet",
            legacy_paths=ordered_bets,
        )
        bet_preprocess_primary = bet_read[0]
        bet_preprocess_extras = bet_read[1:]
        reg_yaml = (
            Path(args.bet_preprocess.preprocess_registry_yaml)
            if args.bet_preprocess.preprocess_registry_yaml is not None
            else _hpre.default_preprocess_registry_yaml_path()
        )
        merged_bet_sources = _hbet.merge_bet_source_paths(bet_cache_primary, bet_cache_extras or None)
        base_bet_cfg = replace(
            effective_bet_cfg,
            adt_filter_quantile=None,
            patron_profile_csv=None,
            canonical_mapping_parquet=None,
            adt_allowed_players_parquet=None,
        )
        bet_cache_extras_arg = bet_cache_extras or None
        bet_preprocess_extras_arg = bet_preprocess_extras or None

        if want_adt_bets and args.canonical_mapping.enabled and allowed_players_pq is not None:
            base_hit = use_preprocess_caches and _hbet.bet_base_clean_cache_is_hit(
                merged_bet_sources,
                base_bet_path,
                preprocess_registry_yaml=reg_yaml,
                dedup_hash_buckets=base_bet_cfg.dedup_hash_buckets,
                cleaned_session_parquet=cleaned_path,
                source_manifest_v2_fingerprint_sha256_hex=source_fp,
                data_scope=base_bet_cfg.data_scope,
            )
            bet_dedup_eff = int(base_bet_cfg.dedup_hash_buckets)
            mf_bkt = _hbet.bet_base_manifest_dedup_hash_buckets(base_bet_path)
            if base_hit and mf_bkt is not None:
                bet_dedup_eff = int(mf_bkt)
            if not base_hit:
                _, bet_dedup_eff = _hpre.preprocess_bets_from_parquet_streaming(
                    bet_preprocess_primary,
                    base_bet_path,
                    cfg=base_bet_cfg,
                    duckdb_runtime=args.duckdb_runtime,
                    extra_partition_sources=bet_preprocess_extras_arg,
                )
                _hbet.write_bet_base_clean_cache_manifest(
                    merged_bet_sources,
                    base_bet_path,
                    preprocess_registry_yaml=reg_yaml,
                    dedup_hash_buckets=int(bet_dedup_eff),
                    cleaned_session_parquet=cleaned_path,
                    source_manifest_v2_fingerprint_sha256_hex=source_fp,
                    data_scope=base_bet_cfg.data_scope,
                )
            if metrics is not None:
                metrics["bet_base_clean_cache_hit"] = bool(base_hit)
                metrics["bet_dedup_hash_buckets_effective"] = int(bet_dedup_eff)

            if args.use_entity_set_v1:
                if rank_meta is None or source_fp is None:
                    raise ValueError(
                        "entity set v1 requires ADT rank table and source_manifest_v2 fingerprint",
                    )
                from trainer_hightier.utils.entity_set_v1 import materialize_entity_set_v1_cached
                from trainer_hightier.utils.universe_cache_v1 import selected_universe_membership_fingerprint

                if selected_universe_fp is None:
                    selected_universe_fp = selected_universe_membership_fingerprint(
                        Path(str(rank_meta["universe_adt_rank_table_path"])),
                        quantile=q_thr,
                    )
                es_meta = materialize_entity_set_v1_cached(
                    base_cleaned_parquet=base_bet_path,
                    rank_table_path=Path(str(rank_meta["universe_adt_rank_table_path"])),
                    selected_universe_fingerprint_sha256_hex=selected_universe_fp,
                    selected_quantile=q_thr,
                    training_scope=base_bet_cfg.data_scope,
                    source_manifest_v2_fingerprint_sha256_hex=source_fp,
                    duckdb_runtime=args.duckdb_runtime,
                    output_parquet=cleaned_bet_path,
                    use_cache=use_preprocess_caches,
                )
                entity_set_policy_fp = str(es_meta.get("entity_set_policy_fingerprint_sha256_hex") or "").strip() or None
                if metrics is not None:
                    metrics.update(es_meta)
                    metrics["bet_segment_legacy_fallback_used"] = False
                logger.info(
                    "[Step 2b] entity set v1 OK: rows=%d cache_hit=%s -> %s",
                    int(es_meta["entity_set_row_count"]),
                    bool(es_meta["entity_set_cache_hit"]),
                    cleaned_bet_path.resolve(),
                )
            else:
                from trainer_hightier.utils.entity_set_v1 import (
                    bet_base_cleaned_fingerprint_sha256_hex,
                    legacy_bet_segment_policy_fingerprint_sha256_hex,
                    training_scope_fingerprint,
                )
                from datetime import date as _date_cls

                from trainer_hightier.config import default_hightier_serving_config
                from trainer_hightier.utils.slow_month_turn import resolve_slow_month_turn_context

                _slow_ctx = resolve_slow_month_turn_context(_date_cls.today())
                _svc_cfg = default_hightier_serving_config()
                materialize_adt_allowed_players_parquet(
                    profile_csv_path,
                    mapping_parquet_path,
                    quantile=q_thr,
                    duckdb_runtime=args.duckdb_runtime,
                    output_parquet=allowed_players_pq,
                    cleaned_session_parquet=cleaned_path,
                    slow_active_anchor=_slow_ctx.slow_anchor_required,
                    slow_lookback_days=int(_svc_cfg.production_slow_lookback_days),
                )
                seg_hit = use_preprocess_caches and _hbet.bet_clean_cache_is_hit(
                    bet_cache_primary,
                    cleaned_bet_path,
                    preprocess_registry_yaml=reg_yaml,
                    dedup_hash_buckets=effective_bet_cfg.dedup_hash_buckets,
                    cleaned_session_parquet=cleaned_path,
                    adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                    patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                    canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                    adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                    extra_source_bet_parquets=bet_cache_extras_arg,
                    bet_base_cleaned_parquet=base_bet_path,
                    partition_inventory_fingerprint_sha256_hex=inv_fp,
                    data_scope=effective_bet_cfg.data_scope,
                )
                if metrics is not None:
                    metrics["bet_segment_clean_cache_hit"] = bool(seg_hit)
                    metrics["bet_segment_legacy_fallback_used"] = True
                if base_hit and seg_hit:
                    logger.info(
                        "[Step 2b] bet base+legacy segment cache hit; skip: %s",
                        cleaned_bet_path.resolve(),
                    )
                else:
                    if not seg_hit:
                        _hbet.segment_cleaned_bet_from_base_parquet(
                            base_bet_path,
                            allowed_players_pq,
                            cleaned_bet_path,
                            duckdb_runtime=args.duckdb_runtime,
                        )
                        _hbet.write_bet_clean_cache_manifest(
                            bet_cache_primary,
                            cleaned_bet_path,
                            preprocess_registry_yaml=reg_yaml,
                            dedup_hash_buckets=int(bet_dedup_eff),
                            cleaned_session_parquet=cleaned_path,
                            adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                            patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                            canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                            adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                            extra_source_bet_parquets=bet_cache_extras_arg,
                            bet_base_cleaned_parquet=base_bet_path,
                            partition_inventory_fingerprint_sha256_hex=inv_fp,
                            data_scope=effective_bet_cfg.data_scope,
                        )
                    n_b = _hbet.partitioned_cleaned_bet_total_rows(cleaned_bet_path)
                    logger.info(
                        "[Step 2b] bet preprocess OK (legacy base+segment): rows=%d -> %s",
                        n_b,
                        cleaned_bet_path,
                    )
                entity_set_policy_fp = legacy_bet_segment_policy_fingerprint_sha256_hex(
                    selected_quantile=q_thr,
                    bet_base_fingerprint_sha256_hex=bet_base_cleaned_fingerprint_sha256_hex(base_bet_path),
                    training_scope_fingerprint_sha256_hex=training_scope_fingerprint(base_bet_cfg.data_scope),
                    partition_inventory_fingerprint_sha256_hex=inv_fp or "",
                    source_manifest_v2_fingerprint_sha256_hex=source_fp,
                )
        else:
            bet_cache_ok = use_preprocess_caches and _hbet.bet_clean_cache_is_hit(
                bet_cache_primary,
                cleaned_bet_path,
                preprocess_registry_yaml=reg_yaml,
                dedup_hash_buckets=effective_bet_cfg.dedup_hash_buckets,
                cleaned_session_parquet=cleaned_path,
                adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                extra_source_bet_parquets=bet_cache_extras_arg,
                partition_inventory_fingerprint_sha256_hex=inv_fp,
                data_scope=effective_bet_cfg.data_scope,
            )
            if metrics is not None:
                metrics["bet_clean_cache_hit"] = bool(bet_cache_ok)
            if bet_cache_ok:
                logger.info(
                    "[Step 2b] bet clean cache hit; skip (use --ignore-caches to force): %s",
                    cleaned_bet_path.resolve(),
                )
            else:
                out_b, bet_dedup_eff = _hpre.preprocess_bets_from_parquet_streaming(
                    bet_preprocess_primary,
                    cleaned_bet_path,
                    cfg=effective_bet_cfg,
                    duckdb_runtime=args.duckdb_runtime,
                    extra_partition_sources=bet_preprocess_extras_arg,
                )
                _hbet.write_bet_clean_cache_manifest(
                    bet_cache_primary,
                    out_b,
                    preprocess_registry_yaml=reg_yaml,
                    dedup_hash_buckets=int(bet_dedup_eff),
                    cleaned_session_parquet=cleaned_path,
                    adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                    patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                    canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                    adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                    extra_source_bet_parquets=bet_cache_extras_arg,
                    partition_inventory_fingerprint_sha256_hex=inv_fp,
                    data_scope=effective_bet_cfg.data_scope,
                )
                n_b = _hbet.partitioned_cleaned_bet_total_rows(out_b)
                logger.info(
                    "[Step 2b] bet preprocess OK: cleaned rows=%d; written %s",
                    n_b,
                    out_b,
                )
                if metrics is not None:
                    metrics["bet_dedup_hash_buckets_effective"] = int(bet_dedup_eff)
                from trainer_hightier.utils.entity_set_v1 import (
                    bet_base_cleaned_fingerprint_sha256_hex,
                    bet_clean_policy_fingerprint_sha256_hex,
                    training_scope_fingerprint,
                )

                entity_set_policy_fp = bet_clean_policy_fingerprint_sha256_hex(
                    cleaned_bet_fingerprint_sha256_hex=bet_base_cleaned_fingerprint_sha256_hex(
                        cleaned_bet_path,
                    ),
                    training_scope_fingerprint_sha256_hex=training_scope_fingerprint(
                        effective_bet_cfg.data_scope,
                    ),
                    source_manifest_v2_fingerprint_sha256_hex=source_fp,
                    partition_inventory_fingerprint_sha256_hex=inv_fp,
                )
    elif bet_partition_paths and args.skip_bet_preprocess:
        logger.info(
            "[Step 2b] bet preprocess skipped (skip_bet_preprocess=True); %d bet shard(s) available",
            len(bet_partition_paths),
        )
    else:
        logger.info(
            "[Step 2b] no t_bet partition shards found under snapshot dir; skip bet preprocess",
        )

    if (
        args.materialize_walkaway_labels
        and not args.skip_bet_preprocess
        and bool(bet_partition_paths)
        and _hbet.cleaned_bet_dataset_has_any_parquet(cleaned_bet_path)
    ):
        if not mapping_parquet_path.is_file():
            logger.warning(
                "[Step 2c] skip walkaway labels: canonical mapping missing at %s",
                mapping_parquet_path,
            )
        else:
            from trainer_hightier.utils.cache_invalidation_v1 import label_invalid_months
            from trainer_hightier.utils.entity_set_v1 import (
                bet_base_cleaned_fingerprint_sha256_hex,
                bet_clean_policy_fingerprint_sha256_hex,
                training_scope_fingerprint,
            )
            from trainer_hightier.utils.labels_cache_v1 import materialize_labels_v1_cached

            labels_fp = entity_set_policy_fp
            if labels_fp is None:
                labels_fp = bet_clean_policy_fingerprint_sha256_hex(
                    cleaned_bet_fingerprint_sha256_hex=bet_base_cleaned_fingerprint_sha256_hex(
                        cleaned_bet_path,
                    ),
                    training_scope_fingerprint_sha256_hex=training_scope_fingerprint(
                        effective_bet_cfg.data_scope,
                    ),
                    source_manifest_v2_fingerprint_sha256_hex=source_fp,
                    partition_inventory_fingerprint_sha256_hex=inv_fp,
                )
            labels_invalid = tuple(sorted(label_invalid_months(set(recompute_months))))
            labels_out = args.objective.labels_parquet or default_walkaway_labels_parquet_path()
            lbl_meta = materialize_labels_v1_cached(
                cleaned_bet_parquet=cleaned_bet_path,
                canonical_mapping_parquet=mapping_parquet_path,
                entity_set_fingerprint=labels_fp,
                duckdb_runtime=args.duckdb_runtime,
                out_parquet=labels_out,
                invalid_months=labels_invalid,
                use_cache=use_preprocess_caches,
                use_sharded_cache=bool(args.use_sharded_labels_cache),
            )
            if metrics is not None:
                metrics.update(lbl_meta)
                metrics["entity_set_policy_fingerprint_sha256_hex"] = labels_fp
                metrics["labels_invalid_months"] = list(labels_invalid)
            logger.info(
                "[Step 2c] walkaway labels %s cache_hit=%s rows=%d -> %s",
                "OK" if lbl_meta.get("labels_row_count", 0) > 0 else "empty",
                bool(lbl_meta.get("labels_cache_hit")),
                int(lbl_meta.get("labels_row_count") or 0),
                labels_out.resolve(),
            )


def _resolve_features_parquet(args: HighTierTrainArgs) -> Path:
    """Return training features Parquet path (Step 3 default or explicit override)."""

    if args.features_input_parquet is not None:
        return Path(args.features_input_parquet).resolve()
    return _b3.DEFAULT_OUTPUT.resolve()


def _ensure_training_parquet_gaming_day_event_column(
    parquet_path: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    cleaned_bet_parquet: Path | None = None,
) -> Path:
    """Sync ``gaming_day_event`` from cleaned bet event time; drop legacy ``gaming_day``."""

    import duckdb

    from trainer_hightier.utils.bet_l0_preprocess import resolved_cleaned_bet_read_parquet_sql
    from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

    p = Path(parquet_path).resolve()
    cleaned = Path(cleaned_bet_parquet or _hpre.default_cleaned_bet_parquet_path()).resolve()
    names = set(pq.read_schema(p).names)
    if "bet_id" not in names:
        raise ValueError(f"training parquet missing bet_id; path={p}")

    exclude = ["gaming_day", "gaming_day_event"]
    exclude_present = [c for c in exclude if c in names]
    other_cols = [c for c in names if c not in exclude_present]
    if not other_cols:
        raise ValueError(f"training parquet has no columns to preserve; path={p}")

    tmp = p.parent / f"{p.stem}.__gde_migrate__.parquet"
    p_esc = str(p).replace("\\", "/").replace("'", "''")
    tmp_esc = str(tmp).replace("\\", "/").replace("'", "''")
    bet_from = resolved_cleaned_bet_read_parquet_sql(cleaned)
    select_cols = ",\n    ".join(f'h."{c}"' for c in other_cols)
    legacy_gday = "CAST(TRY_CAST(h.gaming_day AS DATE) AS DATE)" if "gaming_day" in names else "CAST(NULL AS DATE)"
    existing_gde = (
        "CAST(TRY_CAST(h.gaming_day_event AS DATE) AS DATE)"
        if "gaming_day_event" in names
        else "CAST(NULL AS DATE)"
    )
    sql = f"""
COPY (
  SELECT
    {select_cols},
    COALESCE(
      CAST(b.gaming_day_event AS DATE),
      {existing_gde},
      {legacy_gday}
    ) AS gaming_day_event
  FROM read_parquet('{p_esc}') h
  LEFT JOIN (
    SELECT
      TRY_CAST(bet_id AS DOUBLE) AS bet_id,
      MIN(CAST(gaming_day_event AS DATE)) AS gaming_day_event
    FROM {bet_from} AS _cbd
    WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
    GROUP BY 1
  ) b ON TRY_CAST(h.bet_id AS DOUBLE) = b.bet_id
) TO '{tmp_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(sql)
        n_bad = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{tmp_esc}') "
            "WHERE gaming_day_event IS NULL",
        ).fetchone()
        n_bad_i = int(n_bad[0]) if n_bad else 0
        if n_bad_i > 0:
            logger.warning(
                "[Step 4] gaming_day_event sync left %d NULL row(s) in %s "
                "(no cleaned-bet match); re-run Step 3 to rebuild training_set.",
                n_bad_i,
                p.name,
            )
    finally:
        con.close()
    backup = p.with_suffix(".parquet.legacy_gaming_day.bak")
    if not backup.is_file() and p.is_file():
        shutil.copy2(p, backup)
    tmp.replace(p)
    logger.info(
        "[Step 4] synced gaming_day_event from cleaned bet for %s (dropped legacy gaming_day=%s)",
        p.name,
        "gaming_day" in names,
    )
    return p


def _init_training_acceleration_metrics(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
) -> ResolvedTrainingScope:
    """Resolve acceleration policies, validate guards, and seed run-report fields."""
    validate_sample_policy_for_run(args.sample_policy, run_kind=args.training_run_kind)
    validate_feature_screening_policy(args.feature_screening_policy)
    resolved = resolve_training_scope(args.training_scope_policy)
    scope_fp = training_scope_policy_fingerprint(resolved)
    sample_fp = sample_policy_fingerprint(args.sample_policy)
    screening_fp = feature_selection_policy_fingerprint(args.feature_screening_policy)
    block: dict[str, Any] = {
        "training_run_kind": str(args.training_run_kind),
        "training_scope_policy_fingerprint": scope_fp,
        "sample_policy_fingerprint": sample_fp,
        "feature_selection_policy_fingerprint": screening_fp,
        "resolved_target_scope": resolved.manifest_block(),
        "sample_policy": args.sample_policy.manifest_block(),
        "feature_screening_policy": args.feature_screening_policy.manifest_block(),
        "training_scope_completeness": None,
        "step35_indexed_replay_gate_summary": None,
        "cache_hit_miss_summary": None,
        "negative_sampling_summary": None,
        "feature_screening_summary": None,
    }
    metrics["training_acceleration_policy"] = block
    metrics["training_scope_policy_fingerprint"] = scope_fp
    metrics["sample_policy_fingerprint"] = sample_fp
    metrics["feature_selection_policy_fingerprint"] = screening_fp
    metrics["_resolved_training_scope"] = resolved
    return resolved


def _apply_training_scope_horizon_to_parquet(
    parquet_path: Path,
    *,
    resolved: ResolvedTrainingScope,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> tuple[Path, dict[str, Any]]:
    """Keep only rows in resolved target months and inclusive target date bounds."""
    if not resolved.horizon_enabled:
        return parquet_path, {"horizon_filter_applied": False}
    if resolved.target_start_date is None or resolved.target_end_date is None:
        raise ValueError("resolved training scope missing target_start_date/target_end_date")

    import duckdb

    from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

    p = Path(parquet_path).resolve()
    if "gaming_day_event" not in pq.read_schema(p).names:
        raise ValueError(
            f"training parquet missing gaming_day_event before horizon filter; path={p}",
        )
    months_sql = ", ".join(f"'{ym}'" for ym in resolved.target_months)
    start = resolved.target_start_date.isoformat()
    end = resolved.target_end_date.isoformat()
    p_esc = str(p).replace("\\", "/").replace("'", "''")
    tmp = p.parent / f"{p.stem}.__horizon_filter__.parquet"
    tmp_esc = str(tmp).replace("\\", "/").replace("'", "''")
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        n_before = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{p_esc}')").fetchone()[0])
        con.execute(
            f"""
            COPY (
                SELECT *
                FROM read_parquet('{p_esc}')
                WHERE strftime(CAST(gaming_day_event AS DATE), '%Y%m') IN ({months_sql})
                  AND CAST(gaming_day_event AS DATE) >= DATE '{start}'
                  AND CAST(gaming_day_event AS DATE) <= DATE '{end}'
            ) TO '{tmp_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)
            """,
        )
        n_after = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{tmp_esc}')").fetchone()[0])
    finally:
        con.close()
    if n_after == 0:
        raise ValueError(
            "training scope horizon removed all rows "
            f"(before={n_before}, resolved={resolved.manifest_block()})",
        )
    tmp.replace(p)
    summary = {
        "horizon_filter_applied": True,
        "rows_before": n_before,
        "rows_after": n_after,
        "target_months": list(resolved.target_months),
    }
    logger.info(
        "[Step 4] training scope horizon %s: rows %d -> %d (%s)",
        resolved.manifest_block(),
        n_before,
        n_after,
        p.name,
    )
    return p, summary


def _audit_training_scope_completeness(
    parquet_path: Path,
    *,
    resolved: ResolvedTrainingScope,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> dict[str, Any]:
    """Audit per-month row counts; strict mode fails on empty full target months."""
    if not resolved.horizon_enabled:
        return {"skipped": True, "reason": "horizon_disabled"}

    import duckdb

    from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

    p = Path(parquet_path).resolve()
    p_esc = str(p).replace("\\", "/").replace("'", "''")
    schema_names = pq.read_schema(p).names
    censored_expr = (
        "SUM(CASE WHEN walkaway_censored = TRUE THEN 1 ELSE 0 END)"
        if "walkaway_censored" in schema_names
        else "0"
    )
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        rows = con.execute(
            f"""
            SELECT
                strftime(CAST(gaming_day_event AS DATE), '%Y%m') AS ym,
                COUNT(*) AS row_count,
                MIN(CAST(gaming_day_event AS DATE)) AS min_day,
                MAX(CAST(gaming_day_event AS DATE)) AS max_day,
                {censored_expr} AS censored_rows
            FROM read_parquet('{p_esc}')
            GROUP BY 1
            ORDER BY 1
            """,
        ).fetchall()
    finally:
        con.close()

    by_month: dict[str, dict[str, Any]] = {}
    for ym, row_count, min_day, max_day, censored_rows in rows:
        by_month[str(ym)] = {
            "row_count": int(row_count),
            "min_gaming_day_event": str(min_day),
            "max_gaming_day_event": str(max_day),
            "censored_row_count": int(censored_rows),
            "is_partial_month": str(ym) in resolved.partial_target_months,
        }

    full_months = [ym for ym in resolved.target_months if ym not in resolved.partial_target_months]
    empty_full_months = [ym for ym in full_months if by_month.get(ym, {}).get("row_count", 0) == 0]
    report: dict[str, Any] = {
        "expected_target_months": list(resolved.target_months),
        "full_target_months": full_months,
        "partial_target_months": sorted(resolved.partial_target_months),
        "by_month": by_month,
        "empty_full_target_months": empty_full_months,
        "data_completeness_mode": str(resolved.policy.data_completeness_mode),
    }
    if empty_full_months:
        msg = f"training scope completeness: empty full target month(s) {empty_full_months}"
        if resolved.policy.data_completeness_mode == DATA_COMPLETENESS_MODE_STRICT:
            raise ValueError(msg)
        logger.warning("[Step 4] %s (mode=warn)", msg)
        report["completeness_warnings"] = [msg]
    return report


def _apply_training_data_scope_to_parquet(
    parquet_path: Path,
    *,
    scope: TrainingDataScopeConfig,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Drop training rows outside inclusive ``gaming_day_event`` bounds."""

    if scope.gaming_day_event_min is None and scope.gaming_day_event_max is None:
        return parquet_path

    import duckdb

    from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
    from trainer_hightier.utils.hk_time_semantics import duckdb_gaming_day_event_scope_and_sql

    p = Path(parquet_path).resolve()
    if "gaming_day_event" not in pq.read_schema(p).names:
        raise ValueError(
            f"training parquet missing gaming_day_event before data-scope filter; path={p}",
        )
    scope_sql = duckdb_gaming_day_event_scope_and_sql(
        min_day=scope.gaming_day_event_min,
        max_day=scope.gaming_day_event_max,
    )
    p_esc = str(p).replace("\\", "/").replace("'", "''")
    tmp = p.parent / f"{p.stem}.__scope_filter__.parquet"
    tmp_esc = str(tmp).replace("\\", "/").replace("'", "''")
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        n_before = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{p_esc}')").fetchone()[0])
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{p_esc}') WHERE TRUE{scope_sql}) "
            f"TO '{tmp_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)",
        )
        n_after = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{tmp_esc}')").fetchone()[0])
    finally:
        con.close()
    if n_after == 0:
        raise ValueError(
            f"training data scope removed all rows (before={n_before}, scope={scope.manifest_block()})",
        )
    tmp.replace(p)
    logger.info(
        "[Step 4] training data scope %s: rows %d -> %d (%s)",
        scope.manifest_block(),
        n_before,
        n_after,
        p.name,
    )
    return p


def _prepare_training_features_parquet(
    parquet_path: Path,
    *,
    args: HighTierTrainArgs,
    metrics: dict[str, Any] | None = None,
) -> Path:
    """Sync ``gaming_day_event``, apply date scope, optional target horizon, completeness audit."""

    fp = _ensure_training_parquet_gaming_day_event_column(
        parquet_path,
        duckdb_runtime=args.duckdb_runtime,
    )
    fp = _apply_training_data_scope_to_parquet(
        fp,
        scope=args.training_data_scope,
        duckdb_runtime=args.duckdb_runtime,
    )
    resolved_raw = metrics.get("_resolved_training_scope") if metrics is not None else None
    resolved = (
        resolved_raw
        if isinstance(resolved_raw, ResolvedTrainingScope)
        else resolve_training_scope(args.training_scope_policy)
    )
    fp, horizon_summary = _apply_training_scope_horizon_to_parquet(
        fp,
        resolved=resolved,
        duckdb_runtime=args.duckdb_runtime,
    )
    completeness = _audit_training_scope_completeness(
        fp,
        resolved=resolved,
        duckdb_runtime=args.duckdb_runtime,
    )
    if metrics is not None:
        metrics["training_data_scope"] = args.training_data_scope.manifest_block()
        accel = metrics.get("training_acceleration_policy")
        if isinstance(accel, dict):
            accel["horizon_filter"] = horizon_summary
            accel["training_scope_completeness"] = completeness
        metrics["training_scope_completeness"] = completeness
    return fp


def _assert_training_parquet_has_short_term_pit_columns(
    parquet_path: Path,
    *,
    required_columns: tuple[str, ...],
) -> None:
    """Fail closed when Step 4/5 input lacks bounded short-term PIT columns."""

    import pyarrow.parquet as pq

    if not required_columns:
        return
    have = set(pq.read_schema(Path(parquet_path).resolve()).names)
    missing = [c for c in required_columns if c not in have]
    if missing:
        raise ValueError(
            f"training features parquet missing bounded short-term columns {missing}; "
            f"path={parquet_path.resolve()}. Use training_set_fe_enriched.parquet from Step 3.5, "
            "not raw training_set.parquet (bet__* are not joined in Step 3).",
        )


def _ensure_fe_enriched_training_parquet_for_step4(
    args: HighTierTrainArgs,
    base_training_parquet: Path,
    *,
    metrics: dict[str, Any] | None = None,
) -> Path:
    """When registry baseline includes ``fe__*``, materialize cadence suppliers and join onto Step 3 output."""

    reg_p = Path(args.feature_candidate_registry).resolve() if args.feature_candidate_registry else None
    snap = load_candidate_registry(reg_p)
    baseline = baseline_features_for_main_trainer(snap)
    fe_baseline = tuple(c for c in baseline if str(c).startswith("fe__"))
    trial_baseline = tuple(c for c in SHORT_TERM_TRIAL_BET_COLUMNS if c in baseline)
    if not fe_baseline and not trial_baseline:
        return base_training_parquet

    raw_rows = load_registry_raw_feature_dicts(reg_p)
    cadence_mod = importlib.import_module("trainer_hightier.feature_experiment.feature_cadence")
    mid_mod = importlib.import_module("trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot")
    en_mod = importlib.import_module("trainer_hightier.feature_experiment.dataset_enrich")
    freg = importlib.import_module("trainer_hightier.feature_experiment.feature_registry")
    freg.set_candidate_registry_path(reg_p)

    audit = cadence_mod.build_feature_cadence_audit(snap, baseline, raw_rows=raw_rows)
    fe_split = cadence_mod.classify_model_fe_features(snap, fe_baseline, raw_rows=raw_rows)
    short_fe_cols = cadence_mod.short_term_enrich_columns_with_dependencies(
        fe_split["short_term"],
        fe_split["mid_term"],
    )
    short_cols = tuple(dict.fromkeys([*trial_baseline, *short_fe_cols]))
    mid_cols = fe_split["mid_term"]

    cleaned = _hpre.default_cleaned_bet_parquet_path().resolve()
    out_dir = base_training_parquet.parent
    audit_path = out_dir / "feature_cadence_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")

    fe_short_out = out_dir / TRAINING_SHORT_TERM_PIT_CACHE_BASENAME
    mid_snap_out = out_dir / "_main_trainer_mid_term_daily_snapshot.parquet"
    enriched = out_dir / "training_set_fe_enriched.parquet"

    from trainer_hightier.utils.assembly_cache_v1 import (
        assembly_cache_is_hit,
        assembly_manifest_path,
        assembly_policy_fingerprint_sha256_hex,
        enrich_module_fingerprint_sha256_hex,
        parquet_content_fingerprint,
        registry_baseline_fingerprint_sha256_hex,
        write_assembly_manifest,
    )
    from trainer_hightier.utils.source_manifest_v2 import sha256_file_bytes

    entity_fp_for_assembly: str | None = None
    if metrics is not None:
        entity_raw = metrics.get("entity_set_policy_fingerprint_sha256_hex")
        if entity_raw is not None and str(entity_raw).strip():
            entity_fp_for_assembly = str(entity_raw).strip()
    registry_fp = registry_baseline_fingerprint_sha256_hex(baseline)
    training_base_fp = sha256_file_bytes(base_training_parquet)
    enrich_fp = enrich_module_fingerprint_sha256_hex()
    assembly_manifest_p = assembly_manifest_path(enriched)

    def _assembly_policy_fp(*, short_fp: str | None, mid_fp: str | None) -> str:
        if not entity_fp_for_assembly:
            raise ValueError("entity_set_policy_fingerprint_sha256_hex required for assembly policy")
        return assembly_policy_fingerprint_sha256_hex(
            registry_baseline_fingerprint_sha256_hex=registry_fp,
            training_base_fingerprint_sha256_hex=training_base_fp,
            entity_set_fingerprint_sha256_hex=entity_fp_for_assembly,
            enrich_module_fingerprint_sha256_hex=enrich_fp,
            short_term_parquet_fingerprint_sha256_hex=short_fp,
            mid_term_snapshot_fingerprint_sha256_hex=mid_fp,
        )

    if (
        entity_fp_for_assembly
        and not args.ignore_caches
        and not args.force_refresh_short_term_pit
    ):
        probe_fp = _assembly_policy_fp(
            short_fp=parquet_content_fingerprint(fe_short_out if short_cols else None),
            mid_fp=parquet_content_fingerprint(mid_snap_out if mid_cols else None),
        )
        if assembly_cache_is_hit(
            manifest_path=assembly_manifest_p,
            enriched_parquet=enriched,
            assembly_policy_fingerprint_sha256_hex=probe_fp,
        ):
            if metrics is not None:
                metrics["assembly_cache_hit"] = True
                metrics["assembly_policy_fingerprint_sha256_hex"] = probe_fp
                metrics["assembly_manifest_path"] = str(assembly_manifest_p.resolve())
                metrics["main_trainer_training_parquet_for_step4"] = str(enriched.resolve())
            logger.info("[Step 3.5] assembly cache hit -> %s", enriched.resolve())
            return enriched
        if metrics is not None:
            metrics["assembly_policy_fingerprint_sha256_hex"] = probe_fp

    t_m0 = time.perf_counter()
    short_fe_only = tuple(c for c in short_cols if str(c).startswith("fe__"))
    short_cache_meta: dict[str, Any] = {}
    if short_cols:
        cache_mod = importlib.import_module("trainer_hightier.feature_experiment.short_term_pit_cache")
        from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path

        cmap_path = default_canonical_mapping_parquet_path().resolve()
        inv_fp: str | None = None
        recompute_months: tuple[str, ...] = ()
        if metrics is not None:
            inv_fp_raw = metrics.get("partition_inventory_fingerprint_sha256_hex")
            if inv_fp_raw is not None and str(inv_fp_raw).strip():
                inv_fp = str(inv_fp_raw).strip()
            rem = metrics.get("partition_recompute_months")
            if isinstance(rem, list):
                recompute_months = tuple(str(m).strip() for m in rem if str(m).strip())
        entity_fp: str | None = None
        entity_delta_ids: tuple[int, ...] = ()
        prev_entity_fp: str | None = None
        if metrics is not None:
            entity_raw = metrics.get("entity_set_policy_fingerprint_sha256_hex")
            if entity_raw is not None and str(entity_raw).strip():
                entity_fp = str(entity_raw).strip()
            raw_ids = metrics.get("entity_delta_added_player_ids")
            if isinstance(raw_ids, list):
                entity_delta_ids = tuple(int(x) for x in raw_ids if str(x).strip())
            prev_raw = metrics.get("entity_delta_previous_entity_set_fingerprint_sha256_hex")
            if prev_raw is not None and str(prev_raw).strip():
                prev_entity_fp = str(prev_raw).strip()
        force_short_refresh = bool(args.force_refresh_short_term_pit) or bool(args.ignore_caches)
        _, short_cache_meta = cache_mod.materialize_fe_derived_short_term_parquet_with_cache(
            cleaned_bet_parquet=cleaned,
            training_parquet_for_bet_ids=base_training_parquet,
            out_parquet=fe_short_out,
            duckdb_runtime=args.duckdb_runtime,
            canonical_mapping_parquet=cmap_path,
            short_term_columns=short_fe_only,
            trial_columns=trial_baseline,
            batch_size=int(args.training_short_term_materialize_batch_size),
            entity_set_fingerprint_sha256_hex=entity_fp,
            entity_delta_added_player_ids=entity_delta_ids,
            previous_entity_set_fingerprint_sha256_hex=prev_entity_fp,
            recompute_months=recompute_months,
            force_refresh=force_short_refresh,
            step35_miss_path=str(args.step35_miss_path),
        )
    mat_short_sec = round(time.perf_counter() - t_m0, 3)

    t_m1 = time.perf_counter()
    mid_meta: dict[str, Any] = {}
    if mid_cols:
        from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path

        cmap_path = default_canonical_mapping_parquet_path().resolve()
        universe_pq = out_dir / "_main_trainer_mid_term_canonical_universe.parquet"
        anchor_start, anchor_end, bets_start, bets_end = mid_mod.compute_training_mid_term_bounds(
            base_training_parquet,
            duckdb_runtime=args.duckdb_runtime,
            lookback_days=MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS,
        )
        universe_rows = mid_mod.write_training_canonical_universe_parquet(
            base_training_parquet,
            universe_pq,
            duckdb_runtime=args.duckdb_runtime,
        )
        lb = int(MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS)
        cached = mid_mod.try_reuse_mid_term_snapshot_cache(
            mid_snap_out,
            snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_TRAINING,
            cleaned_bet_parquet=cleaned,
            canonical_mapping_parquet=cmap_path,
            canonical_universe_parquet=universe_pq,
            lookback_days=lb,
            anchor_gaming_day_event_start=anchor_start,
            anchor_gaming_day_event_end=anchor_end,
        )
        if cached is not None:
            _, mid_meta = cached
            mid_meta["cache_hit"] = True
            logger.info(
                "[Step 3.5] mid-term snapshot cache hit scope=%s universe_rows=%d anchor_end=%s",
                MID_TERM_SNAPSHOT_SCOPE_TRAINING,
                universe_rows,
                anchor_end,
            )
        else:
            logger.info(
                "[Step 3.5] materializing mid-term snapshot scope=%s universe_rows=%d "
                "anchor_end=%s bets_gday=[%s,%s]",
                MID_TERM_SNAPSHOT_SCOPE_TRAINING,
                universe_rows,
                anchor_end,
                bets_start,
                bets_end,
            )
            _, mid_meta = mid_mod.materialize_mid_term_daily_snapshot(
                cleaned_bet_parquet=cleaned,
                out_parquet=mid_snap_out,
                duckdb_runtime=args.duckdb_runtime,
                canonical_mapping_parquet=cmap_path,
                canonical_universe_parquet=universe_pq,
                lookback_days=lb,
                anchor_gaming_day_event_start=anchor_start,
                anchor_gaming_day_event_end=anchor_end,
                bets_gaming_day_start=bets_start,
                bets_gaming_day_end=bets_end,
                snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_TRAINING,
            )
    mat_mid_sec = round(time.perf_counter() - t_m1, 3)

    t_e0 = time.perf_counter()
    en_mod.enrich_training_parquet_with_cadence_suppliers(
        base_training_parquet=base_training_parquet,
        fe_short_term_parquet=fe_short_out if short_cols else None,
        mid_term_snapshot_parquet=mid_snap_out if mid_cols else None,
        out_parquet=enriched,
        duckdb_runtime=args.duckdb_runtime,
        short_term_columns=short_cols,
        mid_term_columns=mid_cols,
        include_audit_columns=True,
    )
    enr_sec = round(time.perf_counter() - t_e0, 3)

    if metrics is not None:
        metrics["main_trainer_fe_materialize_short_sec"] = mat_short_sec
        metrics["main_trainer_fe_materialize_mid_sec"] = mat_mid_sec
        metrics["main_trainer_fe_enrich_sec"] = enr_sec
        metrics["main_trainer_training_parquet_for_step4"] = str(enriched.resolve())
        metrics["main_trainer_fe_short_term_parquet"] = str(fe_short_out.resolve()) if short_cols else None
        if short_cache_meta:
            metrics["main_trainer_fe_short_term_cache"] = short_cache_meta
            metrics["short_term_pit_primitive_hit_ratio"] = short_cache_meta.get(
                "short_term_pit_primitive_hit_ratio",
            )
            metrics["short_term_pit_recompute_months"] = short_cache_meta.get(
                "short_term_pit_recompute_months",
            )
            metrics["short_term_pit_source_invalid_months"] = short_cache_meta.get(
                "short_term_pit_source_invalid_months",
            )
            metrics["short_term_pit_delta_fill_shards"] = short_cache_meta.get(
                "short_term_pit_delta_fill_shards",
            )
            metrics["entity_delta_fill_elapsed_seconds"] = short_cache_meta.get(
                "entity_delta_fill_elapsed_seconds",
            )
            accel = metrics.get("training_acceleration_policy")
            if isinstance(accel, dict):
                accel["cache_hit_miss_summary"] = {
                    "step35_miss_path": short_cache_meta.get("step35_miss_path"),
                    "cache_hit": short_cache_meta.get("cache_hit"),
                    "cache_hit_ratio": short_cache_meta.get("cache_hit_ratio"),
                    "cache_hit_shards": short_cache_meta.get("cache_hit_shards"),
                    "cache_miss_shards": short_cache_meta.get("cache_miss_shards"),
                    "cache_reason_counts": short_cache_meta.get("cache_reason_counts"),
                    "step35_materializer_by_shard": short_cache_meta.get(
                        "step35_materializer_by_shard",
                    ),
                    "step35_indexed_replay_shard_seconds": short_cache_meta.get(
                        "step35_indexed_replay_shard_seconds",
                    ),
                    "indexed_replay_gate_mode": short_cache_meta.get("indexed_replay_gate_mode"),
                }
        metrics["main_trainer_mid_term_snapshot_parquet"] = str(mid_snap_out.resolve()) if mid_cols else None
        metrics["feature_cadence_audit"] = audit
        metrics["feature_cadence_audit_path"] = str(audit_path.resolve())
        if mid_meta:
            metrics["main_trainer_mid_term_snapshot_meta"] = mid_meta
        if entity_fp_for_assembly and not args.ignore_caches:
            final_fp = _assembly_policy_fp(
                short_fp=parquet_content_fingerprint(fe_short_out if short_cols else None),
                mid_fp=parquet_content_fingerprint(mid_snap_out if mid_cols else None),
            )
            write_assembly_manifest(
                manifest_path=assembly_manifest_p,
                enriched_parquet=enriched,
                assembly_policy_fingerprint_sha256_hex=final_fp,
                registry_baseline_fingerprint_sha256_hex=registry_fp,
                training_base_fingerprint_sha256_hex=training_base_fp,
                entity_set_fingerprint_sha256_hex=entity_fp_for_assembly,
            )
            metrics["assembly_cache_hit"] = False
            metrics["assembly_manifest_path"] = str(assembly_manifest_p.resolve())
            metrics["assembly_policy_fingerprint_sha256_hex"] = final_fp
    logger.info(
        "[Step 3.5] cadence enrich: short_pit=%d (bet__=%d fe__=%d) mid=%d -> %s "
        "(short %.3fs, mid %.3fs, enrich %.3fs%s)",
        len(short_cols),
        len(trial_baseline),
        len(short_fe_only),
        len(mid_cols),
        enriched.name,
        mat_short_sec,
        mat_mid_sec,
        enr_sec,
        (
            f", short_cache_hit={short_cache_meta.get('cache_hit')}"
            f" ratio={short_cache_meta.get('cache_hit_ratio')}"
            if short_cache_meta
            else ""
        ),
    )
    return enriched


def _maybe_run_step4(args: HighTierTrainArgs, *, metrics: dict[str, Any] | None) -> None:
    """Step 4: project/cast columns and write train/val/test splits by ``gaming_day``."""

    if not args.run_step4:
        logger.info("[Step 4] skipped (run_step4=False); using any existing splits for Step 5 if present.")
        return
    fp = _resolve_features_parquet(args)
    if not fp.is_file():
        if args.start_from_features:
            raise FileNotFoundError(
                f"--start-from-features requires features parquet at {fp}; "
                "use --features-input or build Step 3 output first.",
            )
        logger.warning("[Step 4] skip: no features parquet at %s", fp.resolve())
        return
    logger.info("[Step 4] starting from features parquet %s", fp.resolve())
    fp = _prepare_training_features_parquet(fp, args=args, metrics=metrics)
    fp_eff = _ensure_fe_enriched_training_parquet_for_step4(args, fp, metrics=metrics)
    reg_p = Path(args.feature_candidate_registry).resolve() if args.feature_candidate_registry else None
    snap = load_candidate_registry(reg_p)
    baseline = baseline_features_for_main_trainer(snap)
    pit_required = tuple(
        dict.fromkeys(
            [
                *(c for c in SHORT_TERM_TRIAL_BET_COLUMNS if c in baseline),
                *(c for c in baseline if str(c).startswith("fe__")),
            ],
        ),
    )
    _assert_training_parquet_has_short_term_pit_columns(fp_eff, required_columns=pit_required)
    logger.info(
        "[Step 4] calling arrange_and_split_training_data (input %s → splits dir %s)",
        fp_eff.resolve(),
        _resolve_splits_dir(args).resolve(),
    )
    slow_for_split = default_slow_patron_180d_monthly_parquet_path().resolve()
    step4_cfg = args.step4_split
    if slow_for_split.is_file():
        step4_cfg = replace(args.step4_split, slow_patron_parquet=slow_for_split)
    else:
        logger.warning(
            "[Step 4] slow_patron_parquet missing at %s; splits will not filter slow coverage",
            slow_for_split,
        )
    t0 = time.perf_counter()
    res = _b4.arrange_and_split_training_data(
        features_parquet=fp_eff,
        duckdb_runtime=args.duckdb_runtime,
        step4=step4_cfg,
    )
    elapsed = round(time.perf_counter() - t0, 3)
    if metrics is not None:
        metrics["step4_seconds"] = elapsed
        metrics["step4_split_report"] = str(res.split_report_json.resolve())
        metrics["step4_splits_dir"] = str(res.splits_dir.resolve())
        periods = _step4_gaming_day_periods_from_report(res.report)
        if periods is not None:
            metrics["step4_split_periods"] = periods
        accel = metrics.get("training_acceleration_policy")
        if isinstance(accel, dict):
            report = dict(res.report)
            report["target_scope"] = accel.get("resolved_target_scope")
            report["training_scope_completeness"] = metrics.get("training_scope_completeness")
            res.split_report_json.write_text(
                json.dumps(report, indent=2, default=str),
                encoding="utf-8",
            )
            res = replace(res, report=report)
    logger.info("[Step 4] splits written %s (%.3fs)", res.splits_dir.resolve(), elapsed)


def _maybe_build_training_dataset(args: HighTierTrainArgs, *, metrics: dict[str, Any] | None = None) -> None:
    """Step 3: Feast + labels training Parquet (see ``03_build_training_data``); default on."""
    if not args.build_training_dataset:
        return
    cleaned_bet_path = _hpre.default_cleaned_bet_parquet_path()
    if not _hbet.cleaned_bet_dataset_has_any_parquet(cleaned_bet_path):
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
    ensure_res = _b3.ensure_feast_registry_ready(feast_repo.resolve(), auto_apply=args.auto_feast_apply)
    if metrics is not None:
        metrics["feast_auto_apply"] = _b3.feast_registry_ensure_result_to_metrics(ensure_res)
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
        feast_retrieval_cache_enabled=bool(args.feast_retrieval_cache) and not bool(args.ignore_caches),
        auto_feast_apply=bool(args.auto_feast_apply),
    )
    out = _b3.build_training_data(cfg)
    _prepare_training_features_parquet(out, args=args, metrics=metrics)
    logger.info("[Step 3] training dataset written %s", out)
    logger.info(
        "[Pipeline] After Step 3: Step 4 will split by gaming_day (if run_step4), then Step 5 LightGBM (if run_step5). "
        "If the console is quiet, heavy work is usually DuckDB split or model fit.",
    )


def fit_model(args: HighTierTrainArgs, *, metrics: dict[str, Any] | None = None) -> _b5.Step5Result | None:
    """Train Step 5 LightGBM on Step 4 splits; merge metrics into ``metrics`` when provided."""

    if not args.step5.run_step5:
        logger.info("[Step 5] skipped (step5.run_step5=False)")
        return None
    splits_dir = _resolve_splits_dir(args)
    if not (splits_dir / "train.parquet").is_file():
        logger.warning(
            "[Step 5] skip: no train.parquet under %s (run Step 4 or set splits_output_dir)",
            splits_dir,
        )
        return None
    registry_arg = Path(args.feature_candidate_registry).resolve() if args.feature_candidate_registry else None
    try:
        snap = load_candidate_registry(registry_arg)
    except FileNotFoundError as exc:
        attempted = registry_arg if registry_arg is not None else default_registry_path()
        raise FileNotFoundError(
            "Feature candidate registry file not found. "
            f"Tried {attempted!s}; default is {default_registry_path()!s}. "
            "Pass --feature-candidate-registry <path> or restore the YAML.",
        ) from exc
    feat_cols = baseline_features_for_main_trainer(snap)
    from trainer_hightier.utils.feature_screening_hook import resolve_step5_feature_columns

    feat_cols, screening_meta = resolve_step5_feature_columns(
        baseline_features=feat_cols,
        policy=args.feature_screening_policy,
    )
    if metrics is not None:
        metrics["feature_screening_summary"] = screening_meta
        accel = metrics.get("training_acceleration_policy")
        if isinstance(accel, dict):
            accel["feature_screening_summary"] = screening_meta
    logger.info(
        "[Step 5] model input features n=%d (baseline=%d screening=%s)",
        len(feat_cols),
        screening_meta.get("baseline_feature_count"),
        "enabled" if screening_meta.get("enabled") else "noop",
    )
    _step5_splits_have_feature_columns(splits_dir, feat_cols, registry_path=snap.path)
    logger.info(
        "[Step 5] baseline features from registry %s (n=%s)",
        snap.path,
        len(feat_cols),
    )
    reg_echo = {
        "registry_version": snap.registry_version,
        "updated_at": snap.updated_at,
        "resolved_path": str(snap.path),
        "n_baseline_features": len(feat_cols),
    }
    try:
        step5_out_dir = Path(args.step5_bundle_dir).resolve() if args.step5_bundle_dir is not None else Path(args.output_dir).resolve()
        from trainer_hightier.utils.train_negative_sampling import materialize_sampled_train_parquet

        train_source = splits_dir / "train.parquet"
        sampled_train_p, sample_meta = materialize_sampled_train_parquet(
            train_parquet=train_source,
            splits_dir=splits_dir,
            policy=args.sample_policy,
            force_refresh=bool(args.ignore_caches),
        )
        if metrics is not None:
            metrics["negative_sampling_summary"] = sample_meta
            accel = metrics.get("training_acceleration_policy")
            if isinstance(accel, dict):
                accel["negative_sampling_summary"] = sample_meta
        logger.info(
            "[Step 5] training: train_lgbm_from_splits (splits_dir=%s, n_features=%d, skip_optuna=%s, out_dir=%s) …",
            splits_dir.resolve(),
            len(feat_cols),
            bool(args.step5.skip_optuna),
            step5_out_dir.resolve(),
        )
        result = _b5.train_lgbm_from_splits(
            splits_dir=splits_dir,
            train_parquet=sampled_train_p,
            sample_policy_meta=sample_meta,
            feature_screening_meta=screening_meta,
            duckdb_runtime=args.duckdb_runtime,
            objective_min_precision=float(args.objective.min_precision),
            random_seed=int(args.random_seed),
            step5=args.step5,
            output_dir=step5_out_dir,
            feature_columns=feat_cols,
            persist_training_metrics=False,
        )
        logger.info(
            "[Step 5] train_lgbm_from_splits finished (model=%s, threshold=%s).",
            result.model_path.resolve(),
            result.threshold,
        )
    except ValueError as exc:
        msg = str(exc)
        if "Step 5 schema gate failed" in msg or "requires split parquet" in msg:
            raise ValueError(f"{msg} (feature_candidate_registry={snap.path})") from exc
        raise
    if metrics is not None:
        metrics.update(result.report)
        writer = _get_report_writer(metrics)
        if writer is not None:
            tm_path = writer.write_training_metrics(result.report)
            metrics["training_metrics_path"] = str(tm_path.resolve())
            metrics["step5_training_metrics_path"] = str(tm_path.resolve())
        metrics["model_path"] = str(result.model_path.resolve())
        metrics["candidate_registry"] = reg_echo
        _freeze_feature_registry_snapshot(
            bundle_dir=Path(step5_out_dir),
            registry_path=snap.path,
            metrics=metrics,
        )
    return result


def _freeze_feature_registry_snapshot(
    bundle_dir: Path,
    *,
    registry_path: Path,
    metrics: dict[str, Any],
) -> None:
    """Copy training-time ``feature_candidate_registry`` YAML into bundle; record SHA-256 in metrics + JSON."""

    src = Path(registry_path).resolve()
    if not src.is_file():
        logger.warning("[registry_frozen] skip: registry path not a file: %s", src)
        return
    raw = src.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    bd = Path(bundle_dir).resolve()
    bd.mkdir(parents=True, exist_ok=True)
    dest = bd / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    dest.write_bytes(raw)
    metrics["feature_candidate_registry_snapshot"] = dest.name
    metrics["feature_candidate_registry_sha256"] = digest
    metrics["feature_candidate_registry_frozen_from"] = str(src)[:500]
    patch = {
        "feature_candidate_registry_snapshot": dest.name,
        "feature_candidate_registry_sha256": digest,
        "feature_candidate_registry_frozen_from": str(src)[:500],
    }
    writer = _get_report_writer(metrics)
    if writer is not None:
        writer.patch_training_metrics(patch)
        return
    tm_path = bd / _b5.DEFAULT_METRICS_FILENAME
    if tm_path.is_file():
        try:
            body = json.loads(tm_path.read_text(encoding="utf-8"))
            if isinstance(body, dict):
                body.update(patch)
                tm_path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning("[registry_frozen] skip training_metrics merge: %s", exc)


def write_artifacts(args: HighTierTrainArgs, *, step5_result: _b5.Step5Result | None = None) -> None:
    """Log persisted Step 5 artifacts (model written during ``fit_model``)."""
    if step5_result is not None:
        logger.info("[Step 6] Step 5 model at %s", step5_result.model_path.resolve())


def _deploy_inputs_copy_maybe(di: Path, label: str, src: Path) -> Path | None:
    """Copy *src* into *deploy_inputs/* when the file exists; otherwise log WARN and return ``None``."""

    if not src.is_file():
        logger.warning("[deploy_inputs] missing %s source: %s", label, src)
        return None
    dest = di / src.name
    shutil.copy2(src, dest)
    logger.info("[deploy_inputs] copied %s -> %s", label, dest.name)
    return dest


def _persist_adt_allowlist_sha(
    *,
    metrics: dict[str, Any],
    bundle_dir: Path,
    allow_dest: Path,
    training_metrics_rel: str,
) -> str | None:
    """SHA256-copy allowlist parquet; merge hash into metrics dict and optional ``training_metrics.json``."""

    from trainer_hightier.serving.adt_allowlist import sha256_file

    if allow_dest.is_file():
        al_sha = sha256_file(allow_dest)
        metrics["adt_allowlist_sha256"] = al_sha
        writer = _get_report_writer(metrics)
        if writer is not None:
            writer.patch_training_metrics({"adt_allowlist_sha256": al_sha})
            logger.info("[deploy_inputs] merged adt_allowlist_sha256 into training_metrics.json")
            return al_sha
        tm_path = bundle_dir / training_metrics_rel
        if tm_path.is_file():
            try:
                body = json.loads(tm_path.read_text(encoding="utf-8"))
                if isinstance(body, dict):
                    body["adt_allowlist_sha256"] = al_sha
                    tm_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
                    logger.info("[deploy_inputs] merged adt_allowlist_sha256 into %s", tm_path.name)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                logger.warning("[deploy_inputs] skip training_metrics merge: %s", exc)
        return al_sha
    return None


def _training_cutoff_iso_from_metrics(metrics: dict[str, Any]) -> str | None:
    """Return the first known training cutoff timestamp from metrics, if present."""

    for key in ("training_cutoff_iso", "step5_training_cutoff_iso", "data_cutoff_iso"):
        val = metrics.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _freeze_deploy_inputs(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
    *,
    bundle_dir: Path,
    model_version: str,
) -> None:
    """Copy mapping/allowlist/slow beside bundle and write frozen manifest (relative filenames).

    Best-effort: missing sources WARN only; merges allowlist SHA into metrics and disk JSON when copied."""

    bd = Path(bundle_dir).resolve()
    di = bd / _DEPLOY_INPUTS_DIRNAME
    di.mkdir(parents=True, exist_ok=True)
    map_src = default_canonical_mapping_parquet_path().resolve()
    q_thr = float(args.objective.theo_train_quantile)
    allow_src = default_adt_allowed_players_parquet_path(q_thr).resolve()
    slow_src = default_slow_patron_180d_monthly_parquet_path().resolve()

    _deploy_inputs_copy_maybe(di, "canonical_mapping", map_src)
    allow_dest = _deploy_inputs_copy_maybe(di, "adt_allowlist", allow_src)
    _deploy_inputs_copy_maybe(di, "slow_patron", slow_src)
    snap_frozen = bd / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
    _deploy_inputs_copy_maybe(di, "feature_candidate_registry_snapshot", snap_frozen)
    from trainer_hightier.config import (
        FE_DERIVED_DEPLOY_PARQUET_BASENAME,
        FE_SHORT_TERM_DEPLOY_PARQUET_BASENAME,
        MANIFEST_KEY_FE_SHORT_TERM,
        MANIFEST_KEY_MID_TERM_ANCHOR_MAX,
        MANIFEST_KEY_MID_TERM_COVERAGE_END,
        MANIFEST_KEY_MID_TERM_GENERATED_AT,
        MANIFEST_KEY_MID_TERM_GRAIN,
        MANIFEST_KEY_MID_TERM_SNAPSHOT,
        MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS,
        MANIFEST_KEY_SLOW_ANCHOR_EFFECTIVE,
        MANIFEST_KEY_SLOW_ANCHOR_MAX,
        MANIFEST_KEY_SLOW_ANCHOR_TARGET,
        MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS,
        MANIFEST_KEY_SLOW_MONTH_TURN_PHASE,
        MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS,
        MID_TERM_GRAIN_CANONICAL_DAILY_ASOF,
        MID_TERM_SNAPSHOT_DEPLOY_PARQUET_BASENAME,
        MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
        MID_TERM_STALE_HARD_CAP_DAYS,
        SLOW_MONTHLY_GRACE_DAYS,
        SLOW_PATRON_GRAIN_CANONICAL_ASOF,
        SLOW_STALE_HARD_CAP_DAYS,
        FE_DERIVED_SOURCE_KIND_SHIPPED,
    )
    from trainer_hightier.utils.slow_month_turn import resolve_slow_month_turn_context
    from datetime import date as _date_cls

    fe_src: Path | None = None
    fe_metric = metrics.get("main_trainer_fe_derived_parquet")
    if isinstance(fe_metric, str) and fe_metric.strip():
        fe_src = Path(fe_metric.strip()).expanduser().resolve()
    if fe_src is None or not fe_src.is_file():
        alt = bd / "_main_trainer_fe_derived.parquet"
        if alt.is_file():
            fe_src = alt
    fe_dest: Path | None = None
    fe_short_dest: Path | None = None
    mid_dest: Path | None = None
    if fe_src is not None and fe_src.is_file():
        fe_dest = di / FE_DERIVED_DEPLOY_PARQUET_BASENAME
        shutil.copy2(fe_src, fe_dest)
        logger.info("[deploy_inputs] copied fe_derived -> %s", fe_dest.name)

    fe_short_metric = metrics.get("main_trainer_fe_short_term_parquet")
    fe_short_src: Path | None = None
    if isinstance(fe_short_metric, str) and fe_short_metric.strip():
        fe_short_src = Path(fe_short_metric.strip()).expanduser().resolve()
    if fe_short_src is None or not fe_short_src.is_file():
        alt_short = bd / TRAINING_SHORT_TERM_PIT_CACHE_BASENAME
        if alt_short.is_file():
            fe_short_src = alt_short
    if fe_short_src is not None and fe_short_src.is_file():
        fe_short_dest = di / FE_SHORT_TERM_DEPLOY_PARQUET_BASENAME
        shutil.copy2(fe_short_src, fe_short_dest)
        logger.info("[deploy_inputs] copied fe_short_term -> %s", fe_short_dest.name)

    mid_metric = metrics.get("main_trainer_mid_term_snapshot_parquet")
    mid_src: Path | None = None
    if isinstance(mid_metric, str) and mid_metric.strip():
        mid_src = Path(mid_metric.strip()).expanduser().resolve()
    if mid_src is None or not mid_src.is_file():
        alt_mid = bd / "_main_trainer_mid_term_daily_snapshot.parquet"
        if alt_mid.is_file():
            mid_src = alt_mid
    if mid_src is not None and mid_src.is_file():
        mid_meta = metrics.get("main_trainer_mid_term_snapshot_meta")
        from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
            mid_term_snapshot_production_safe,
        )

        if mid_term_snapshot_production_safe(mid_meta if isinstance(mid_meta, dict) else None):
            mid_dest = di / MID_TERM_SNAPSHOT_DEPLOY_PARQUET_BASENAME
            shutil.copy2(mid_src, mid_dest)
            logger.info("[deploy_inputs] copied mid_term_snapshot -> %s", mid_dest.name)
        else:
            logger.warning(
                "[deploy_inputs] skip mid_term_snapshot copy: scope=%s (training-scoped artifacts "
                "must not ship to production deploy_inputs)",
                (mid_meta or {}).get("snapshot_scope") if isinstance(mid_meta, dict) else None,
            )
            mid_src = None

    al_sha: str | None = None
    if allow_dest is not None:
        al_sha = _persist_adt_allowlist_sha(
            metrics=metrics,
            bundle_dir=bd,
            allow_dest=allow_dest,
            training_metrics_rel=_b5.DEFAULT_METRICS_FILENAME,
        )

    coverage_end = datetime.now(timezone.utc).isoformat()
    man: dict[str, Any] = {
        "version": str(model_version),
        "coverage_end_exclusive": coverage_end,
        "training_cutoff_iso": _training_cutoff_iso_from_metrics(metrics),
        "model_version": str(model_version),
    }
    if slow_src.is_file() and (di / slow_src.name).is_file():
        man["slow_patron_parquet"] = slow_src.name
        man["slow_patron_grain"] = SLOW_PATRON_GRAIN_CANONICAL_ASOF
        slow_ctx = resolve_slow_month_turn_context(_date_cls.today())
        man[MANIFEST_KEY_SLOW_ANCHOR_TARGET] = slow_ctx.slow_anchor_target.isoformat()
        man[MANIFEST_KEY_SLOW_ANCHOR_EFFECTIVE] = slow_ctx.slow_anchor_effective.isoformat()
        man[MANIFEST_KEY_SLOW_MONTH_TURN_PHASE] = slow_ctx.phase
        man[MANIFEST_KEY_SLOW_ANCHOR_MAX] = slow_ctx.slow_anchor_required.isoformat()
    if fe_dest is not None and fe_dest.is_file():
        man["fe_derived_parquet"] = fe_dest.name
    if fe_short_dest is not None and fe_short_dest.is_file():
        man[MANIFEST_KEY_FE_SHORT_TERM] = fe_short_dest.name
    if mid_dest is not None and mid_dest.is_file():
        man[MANIFEST_KEY_MID_TERM_SNAPSHOT] = mid_dest.name
        man[MANIFEST_KEY_MID_TERM_GRAIN] = MID_TERM_GRAIN_CANONICAL_DAILY_ASOF
        man[MANIFEST_KEY_MID_TERM_COVERAGE_END] = coverage_end
        man[MANIFEST_KEY_MID_TERM_GENERATED_AT] = coverage_end
        mid_meta = metrics.get("main_trainer_mid_term_snapshot_meta")
        if isinstance(mid_meta, dict) and str(mid_meta.get("snapshot_scope", "")).strip() == MID_TERM_SNAPSHOT_SCOPE_PRODUCTION:
            anchor_max = mid_meta.get("mid_term_anchor_gaming_day_event_max")
            if anchor_max is None:
                anchor_max = mid_meta.get("anchor_gaming_day_event_max")
            if anchor_max is not None:
                man[MANIFEST_KEY_MID_TERM_ANCHOR_MAX] = str(anchor_max)
    man[MANIFEST_KEY_MID_TERM_STALE_HARD_CAP_DAYS] = MID_TERM_STALE_HARD_CAP_DAYS
    man[MANIFEST_KEY_SLOW_MONTHLY_GRACE_DAYS] = SLOW_MONTHLY_GRACE_DAYS
    man[MANIFEST_KEY_SLOW_STALE_HARD_CAP_DAYS] = SLOW_STALE_HARD_CAP_DAYS
    man["fe_derived_source_kind"] = FE_DERIVED_SOURCE_KIND_SHIPPED
    if allow_dest is not None and allow_dest.is_file():
        man["adt_allowlist_parquet"] = allow_dest.name
        man["adt_allowlist_version"] = al_sha if al_sha else ""

    mf = di / "active_manifest.json"
    mf.write_text(json.dumps(man, indent=2), encoding="utf-8")
    metrics["deploy_inputs_dir"] = str(di.resolve())
    metrics["deploy_inputs_active_manifest"] = str(mf.resolve())


def _sync_feast_online_for_step6(
    repo: Path,
    metrics: dict[str, Any],
    *,
    bundle_dir: Path,
) -> None:
    """Materialize training slow/mid Parquet into Feast online before Step 6 replay."""
    feast_repo = repo / "trainer_hightier" / "feast_repo"
    from trainer_hightier.serving.feature_supply import (
        build_scorer_supplier_plan,
        load_frozen_registry_for_bundle,
    )
    from trainer_hightier.serving.feast_online_refresh import (
        sync_training_mid_snapshot_to_feast_online,
        sync_training_slow_parquet_to_feast_online,
    )
    from trainer_hightier.serving.model_bundle import load_hightier_model_bundle

    bundle = load_hightier_model_bundle(Path(bundle_dir).resolve())
    snap = load_frozen_registry_for_bundle(Path(bundle_dir).resolve())
    plan = build_scorer_supplier_plan(snap, bundle.feature_columns)
    needs_mid_feast = bool(plan.feast_mid_cols or plan.mid_composite_cols)
    default_mid = (
        repo / "trainer_hightier" / "artifacts" / "training_data" / "_main_trainer_mid_term_daily_snapshot.parquet"
    )
    if needs_mid_feast:
        mid_metric = metrics.get("main_trainer_mid_term_snapshot_parquet")
        mid_p: Path | None = None
        if isinstance(mid_metric, str) and mid_metric.strip():
            mid_p = Path(mid_metric.strip()).expanduser().resolve()
        if mid_p is None or not mid_p.is_file():
            if default_mid.is_file():
                mid_p = default_mid
        if mid_p is None or not mid_p.is_file():
            raise FileNotFoundError(
                "[Step 6] cannot sync Feast mid online: training mid snapshot missing "
                f"(checked metrics and {default_mid})",
            )
        t0_mid = time.perf_counter()
        mid_sync = sync_training_mid_snapshot_to_feast_online(feast_repo, mid_snapshot=mid_p)
        metrics["step6_feast_mid_online_sync_seconds"] = round(time.perf_counter() - t0_mid, 3)
        metrics["step6_feast_mid_online_sync"] = mid_sync
        logger.info(
            "[Step 6] synced training mid snapshot to Feast online (rows=%s, %.3fs)",
            mid_sync.get("feast_rows"),
            metrics["step6_feast_mid_online_sync_seconds"],
        )

    slow_p = default_slow_patron_180d_monthly_parquet_path().resolve()
    if not slow_p.is_file():
        raise FileNotFoundError(
            f"[Step 6] cannot sync Feast slow online: training artifact missing at {slow_p}",
        )
    t0 = time.perf_counter()
    sync_meta = sync_training_slow_parquet_to_feast_online(feast_repo, slow_parquet=slow_p)
    metrics["step6_feast_slow_online_sync_seconds"] = round(time.perf_counter() - t0, 3)
    metrics["step6_feast_slow_online_sync"] = sync_meta
    logger.info(
        "[Step 6] synced training slow parquet to Feast online (rows=%s, %.3fs)",
        sync_meta.get("feast_rows"),
        metrics["step6_feast_slow_online_sync_seconds"],
    )


def _load_step06_verify_module() -> Any:
    """Load ``06_verify_training_serving_parity`` (non-importable module name)."""
    import importlib.util

    step06_path = Path(__file__).resolve().parent / "06_verify_training_serving_parity.py"
    spec = importlib.util.spec_from_file_location("trainer_hightier_step06_verify", step06_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Step 6 module from {step06_path}")
    step06_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step06_mod)
    return step06_mod


def _pre_train_gate_columns(args: HighTierTrainArgs) -> tuple[str, ...]:
    """Short-layer baseline columns for Step 4.5 (``bet__*`` + short ``fe__*``)."""
    reg_p = Path(args.feature_candidate_registry).resolve() if args.feature_candidate_registry else None
    snap = load_candidate_registry(reg_p)
    baseline = baseline_features_for_main_trainer(snap)
    fe_split = classify_model_fe_features(snap, baseline)
    return tuple(
        dict.fromkeys(
            [
                *(c for c in SHORT_TERM_TRIAL_BET_COLUMNS if c in baseline),
                *fe_split["short_term"],
            ],
        ),
    )


def _maybe_run_pre_train_feature_gate(args: HighTierTrainArgs, metrics: dict[str, Any]) -> None:
    """Step 4.5: short-term train vs live PIT replay before Step 5."""
    if not args.pre_train_gate.run_pre_train_gate:
        logger.info("[Step 4.5] skipped (pre_train_gate.run_pre_train_gate=False)")
        return
    if not args.run_step4:
        logger.info("[Step 4.5] skipped (Step 4 not run; no test split)")
        return
    gate_cols = _pre_train_gate_columns(args)
    if not gate_cols:
        logger.info("[Step 4.5] skipped (no short-layer baseline columns in registry)")
        return
    step06_mod = _load_step06_verify_module()
    repo = _repo_root()
    splits_dir = args.step4_split.splits_output_dir
    if splits_dir is None:
        test_parquet = repo / "trainer_hightier" / "artifacts" / "training_data" / "splits" / "test.parquet"
    else:
        test_parquet = Path(splits_dir).resolve() / "test.parquet"
    if not test_parquet.is_file():
        raise FileNotFoundError(f"[Step 4.5] test split missing at {test_parquet}")
    cleaned_bet = repo / "trainer_hightier" / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet"
    mapping = default_canonical_mapping_parquet_path().resolve()
    out_json = step06_mod.default_pre_train_gate_json_path()
    from trainer_hightier.utils.partition_inventory import default_partition_snapshot_dir

    report = step06_mod.run_pre_train_feature_gate(
        test_parquet,
        columns=gate_cols,
        cleaned_bet_root=cleaned_bet,
        mapping_parquet=mapping,
        gate_cfg=args.pre_train_gate,
        duckdb_runtime=args.duckdb_runtime,
        output_json=out_json,
        raw_partition_dir=default_partition_snapshot_dir(),
    )
    metrics["pre_train_feature_gate_json"] = str(out_json.resolve())
    metrics["pre_train_feature_gate_verdict"] = report.get("verdict")
    logger.info("[Step 4.5] wrote %s verdict=%s", out_json.name, report.get("verdict"))
    if report.get("verdict") != "pass":
        raise RuntimeError(f"[Step 4.5] pre-train feature gate failed; see {out_json}")


class _Step6GateTimeout(RuntimeError):
    """Raised when Step 6 exceeds the configured wall-clock budget."""


def _step6_remaining_seconds(deadline: float) -> float:
    """Seconds left before Step 6 deadline (minimum 1s for subprocess timeout)."""
    return max(1.0, deadline - time.perf_counter())


def _step6_assert_within_budget(deadline: float, *, phase: str) -> None:
    """Fail fast when Step 6 has exceeded ``step6_total_timeout_seconds``."""
    if time.perf_counter() > deadline:
        raise _Step6GateTimeout(f"[Step 6] timed out before {phase}")


def _resolve_step6_test_parquet(args: HighTierTrainArgs, repo: Path) -> Path:
    """Resolve test split parquet for Step 6 parity replay."""
    splits_dir = args.step4_split.splits_output_dir
    if splits_dir is None:
        return repo / "trainer_hightier" / "artifacts" / "training_data" / "splits" / "test.parquet"
    return Path(splits_dir).resolve() / "test.parquet"


def _run_step6_parity_gate(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
    *,
    bundle_dir: Path,
    repo: Path,
    step06_mod: Any,
) -> None:
    """Run train/serve parity gate and write ``feature_parity_verification.json``."""
    from datetime import date as _date_cls

    _sync_feast_online_for_step6(repo, metrics, bundle_dir=bundle_dir)
    test_parquet = _resolve_step6_test_parquet(args, repo)
    cleaned_bet = repo / "trainer_hightier" / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet"
    feast_repo = repo / "trainer_hightier" / "feast_repo"
    report = step06_mod.build_report_from_config(
        model_dirs=[Path(bundle_dir).resolve()],
        test_parquet=test_parquet,
        cleaned_bet_root=cleaned_bet,
        feast_repo=feast_repo,
        as_of_date=_date_cls.today(),
        parity_cfg=args.step6,
    )
    writer = _get_report_writer(metrics)
    if writer is not None:
        out_json = writer.write_gate_report(FEATURE_PARITY_REPORT_FILENAME, report)
    else:
        out_json = Path(bundle_dir).resolve() / FEATURE_PARITY_REPORT_FILENAME
        out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    metrics["feature_parity_verification_json"] = str(out_json.resolve())
    metrics["step6_slow_gate_failed"] = int(report.get("n_failed_slow_gate", 0))
    metrics["step6_all_feature_gate_failed"] = int(report.get("n_failed_all_feature_gate", 0))
    logger.info(
        "[Step 6] wrote %s slow_gate_failed=%s all_feature_gate_failed=%s",
        out_json.name,
        report.get("n_failed_slow_gate"),
        report.get("n_failed_all_feature_gate"),
    )
    exit_code = step06_mod.report_exit_code(report, parity_cfg=args.step6)
    if exit_code != 0:
        raise RuntimeError(
            "[Step 6] parity gate failed "
            f"(hard_fail_slow_gate={args.step6.hard_fail_slow_gate}, "
            f"hard_fail_all_feature_gate={args.step6.hard_fail_all_feature_gate}); "
            f"see {out_json}"
        )


def _run_step6_deploy_e2e_gate(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
    *,
    bundle_dir: Path,
    repo: Path,
    deadline: float,
) -> None:
    """Build deploy bundle, run fresh-venv deploy E2E, write report beside model bundle."""
    from trainer_hightier.build_deploy_package import build_deploy_package
    from trainer_hightier.utils.session_l0_preprocess import default_cleaned_session_parquet_path

    _step6_assert_within_budget(deadline, phase="deploy_e2e")
    bundle = Path(bundle_dir).resolve()
    ver_path = bundle / "model_version"
    model_version = ver_path.read_text(encoding="utf-8").strip() if ver_path.is_file() else bundle.name
    deploy_out = (DEFAULT_DEPLOY_OUTPUT_ROOT / model_version).resolve()
    metrics["step6_deploy_bundle_dir"] = str(deploy_out)
    t_pack = time.perf_counter()
    build_deploy_package(
        [
            "--model-source",
            str(bundle),
            "--output-dir",
            str(deploy_out),
            "--overwrite",
            "--skip-deploy-e2e-gate",
        ],
    )
    metrics["step6_deploy_package_seconds"] = round(time.perf_counter() - t_pack, 3)

    _step6_assert_within_budget(deadline, phase="deploy_e2e_gate")
    cleaned_bet = repo / "trainer_hightier" / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet"
    cleaned_session = default_cleaned_session_parquet_path()
    e2e_report_path = model_bundle_report_path(bundle, DEPLOY_E2E_GATE_REPORT_FILENAME)
    from trainer_hightier.serving.deploy_e2e_gate import run_cli

    gate_argv = [
        "--bundle-dir",
        str(deploy_out),
        "--local-cleaned-bet",
        str(cleaned_bet.resolve()),
        "--local-cleaned-session",
        str(cleaned_session.resolve()),
        "--output-json",
        str(e2e_report_path.resolve()),
        "--max-bets",
        str(int(args.step6.step6_deploy_e2e_max_bets)),
    ]
    t_e2e = time.perf_counter()
    remaining = _step6_remaining_seconds(deadline)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            exit_code = int(
                pool.submit(run_cli, gate_argv).result(timeout=remaining),
            )
    except concurrent.futures.TimeoutError as exc:
        raise _Step6GateTimeout(
            f"[Step 6] deploy E2E gate exceeded remaining budget ({remaining:.0f}s)",
        ) from exc
    metrics["step6_deploy_e2e_seconds"] = round(time.perf_counter() - t_e2e, 3)
    metrics["step6_deploy_e2e_exit_code"] = exit_code
    metrics["deploy_e2e_gate_report_json"] = str(e2e_report_path.resolve())

    verdict = "fail"
    if e2e_report_path.is_file():
        try:
            raw = json.loads(e2e_report_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                verdict = str(raw.get("verdict") or "fail")
        except (OSError, UnicodeError, json.JSONDecodeError):
            verdict = "fail"
    metrics["step6_deploy_e2e_verdict"] = verdict
    logger.info("[Step 6] deploy_e2e verdict=%s report=%s", verdict, e2e_report_path.name)

    writer = _get_report_writer(metrics)
    if writer is not None and e2e_report_path.is_file():
        writer.register_existing_json(e2e_report_path)

    if exit_code != 0 or (args.step6.hard_fail_deploy_e2e_gate and verdict != "pass"):
        raise RuntimeError(
            f"[Step 6] deploy E2E gate failed (exit={exit_code}, verdict={verdict}); "
            f"see {e2e_report_path}",
        )


def _run_step6_single_attempt(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
    *,
    bundle_dir: Path,
    deadline: float,
) -> None:
    """One Step 6 attempt: parity then optional deploy E2E."""
    repo = _repo_root()
    step06_mod = _load_step06_verify_module()
    _step6_assert_within_budget(deadline, phase="parity")
    _run_step6_parity_gate(
        args,
        metrics,
        bundle_dir=bundle_dir,
        repo=repo,
        step06_mod=step06_mod,
    )
    if args.step6.step6_deploy_e2e_enabled:
        _run_step6_deploy_e2e_gate(args, metrics, bundle_dir=bundle_dir, repo=repo, deadline=deadline)


def _run_step6_parity_verification(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
    *,
    bundle_dir: Path,
) -> None:
    """Run Step 6 parity + deploy E2E gates with shared timeout and one retry."""
    if not args.step6.run_step6:
        logger.info("[Step 6] skipped (step6.run_step6=False)")
        return

    deadline = time.perf_counter() + float(args.step6.step6_total_timeout_seconds)
    max_attempts = 2 if args.step6.step6_auto_retry_once else 1
    metrics["step6_total_timeout_seconds"] = int(args.step6.step6_total_timeout_seconds)
    metrics["step6_auto_retry_once"] = bool(args.step6.step6_auto_retry_once)
    metrics["step6_deploy_e2e_enabled"] = bool(args.step6.step6_deploy_e2e_enabled)

    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        metrics["step6_attempt"] = attempt
        try:
            _run_step6_single_attempt(args, metrics, bundle_dir=bundle_dir, deadline=deadline)
            metrics["step6_verdict"] = "pass"
            logger.info("[Step 6] passed on attempt %s", attempt)
            return
        except _Step6GateTimeout as exc:
            last_error = exc
            logger.warning("[Step 6] attempt %s timed out: %s", attempt, exc)
        except Exception as exc:
            last_error = exc
            logger.warning("[Step 6] attempt %s failed: %s", attempt, exc)
        if attempt < max_attempts:
            logger.info("[Step 6] retrying (%s/%s)", attempt + 1, max_attempts)

    metrics["step6_verdict"] = "fail"
    raise RuntimeError(f"[Step 6] failed after {max_attempts} attempt(s)") from last_error


def _run_training_execute_steps(args: HighTierTrainArgs, metrics: dict[str, Any]) -> _b5.Step5Result | None:
    """Run preprocess → dataset → split → Step 5; mutate ``metrics``; return Step 5 result if any."""

    if args.start_from_features:
        logger.info("[Pipeline] --start-from-features: skipping ingest and Step 3; Step 4 then Step 5.")
        _maybe_run_step4(args, metrics=metrics)
    else:
        t_prep = time.perf_counter()
        prepare_training_frame(args, metrics=metrics)
        metrics["prepare_training_frame_seconds"] = round(time.perf_counter() - t_prep, 3)
        t_step3 = time.perf_counter()
        _maybe_build_training_dataset(args, metrics=metrics)
        metrics["build_training_dataset_seconds"] = round(time.perf_counter() - t_step3, 3)
        logger.info(
            "[Pipeline] prepare_training_frame %.3fs; build_training_dataset %.3fs — entering Step 4.",
            float(metrics["prepare_training_frame_seconds"]),
            float(metrics["build_training_dataset_seconds"]),
        )
        _maybe_run_step4(args, metrics=metrics)

    _maybe_run_pre_train_feature_gate(args, metrics=metrics)

    logger.info("[Pipeline] Step 4 finished or skipped; invoking Step 5 (fit_model) …")
    step5_result = fit_model(args, metrics=metrics)
    if step5_result is not None:
        logger.info("[Pipeline] Step 5 returned a model; continuing to write_artifacts / bundle steps.")
    elif args.step5.run_step5:
        logger.warning(
            "[Pipeline] Step 5 was enabled but no model result (missing splits or early skip); see Step 4/5 logs above.",
        )
    write_artifacts(args, step5_result=step5_result)
    return step5_result


def run_training(args: HighTierTrainArgs) -> None:
    """Run the high-tier training pipeline in order."""

    t0 = time.perf_counter()
    try:
        repo_root = _repo_root()
        versions_root = Path(args.output_dir).resolve()
        versions_root.mkdir(parents=True, exist_ok=True)
        model_version = _mlflow_hightier_run_name(repo_root=repo_root)
        exec_args = args
        if args.step5.run_step5:
            bundle_dir = safe_version_subdirectory(versions_root, model_version)
            if (bundle_dir / _b5.DEFAULT_MODEL_FILENAME).is_file():
                raise FileExistsError(
                    f"Refusing to overwrite existing high-tier Step 5 model bundle under {bundle_dir}. "
                    "Remove the directory or wait for a new model_version timestamp."
                )
            bundle_dir.mkdir(parents=True, exist_ok=True)
            (bundle_dir / "model_version").write_text(model_version + "\n", encoding="utf-8")
            exec_args = replace(args, step5_bundle_dir=bundle_dir)

        run_name = model_version
        with safe_start_run(
            experiment_name=MLFLOW_EXPERIMENT_TRAIN_HIGHTIER,
            run_name=run_name,
            tags={"pipeline": "trainer_hightier", "component": "training"},
        ):
            warm_up_mlflow_run_safe()
            log_tags_safe({"status": "RUNNING"})
            log_params_safe(_mlflow_initial_string_params(exec_args))
            metrics: dict[str, Any] = {"start_epoch_ms": int(time.time() * 1000)}
            report_parent = exec_args.step5_bundle_dir.resolve() if exec_args.step5_bundle_dir else versions_root
            writer = BundleReportWriter(report_parent)
            metrics["report_writer"] = writer
            _init_training_acceleration_metrics(exec_args, metrics)
            if exec_args.step5_bundle_dir is not None:
                metrics["model_version"] = model_version
                metrics["model_bundle_dir"] = str(exec_args.step5_bundle_dir.resolve())
            try:
                _run_training_execute_steps(exec_args, metrics)
                metrics["finish_epoch_ms"] = int(time.time() * 1000)
                metrics["run_training_total_seconds"] = round(time.perf_counter() - t0, 3)
                bundle_sr = writer.copy_split_report(metrics.get("step4_split_report"))
                if bundle_sr is not None:
                    metrics["step4_split_report_bundle"] = str(bundle_sr.resolve())
                if (
                    exec_args.step5_bundle_dir is not None
                    and (exec_args.step5_bundle_dir.resolve() / _b5.DEFAULT_MODEL_FILENAME).is_file()
                ):
                    _freeze_deploy_inputs(
                        exec_args,
                        metrics,
                        bundle_dir=exec_args.step5_bundle_dir.resolve(),
                        model_version=model_version,
                    )
                    _run_step6_parity_verification(
                        exec_args,
                        metrics,
                        bundle_dir=exec_args.step5_bundle_dir.resolve(),
                    )
                from trainer_hightier.utils.source_manifest_v2 import finalize_cache_report_from_metrics

                finalized = finalize_cache_report_from_metrics(metrics)
                if finalized is not None:
                    metrics["cache_report_finalized_path"] = str(finalized.resolve())
                _finalize_training_reports(exec_args, metrics, writer, status="SUCCESS")
                log_params_safe(_mlflow_post_run_string_params(metrics))
                log_metrics_safe(_mlflow_scalar_metrics(metrics))
                _log_mlflow_whitelist_artifacts(exec_args, metrics, writer)
                if (
                    exec_args.step5_bundle_dir is not None
                    and (exec_args.step5_bundle_dir.resolve() / _b5.DEFAULT_MODEL_FILENAME).is_file()
                ):
                    write_latest_model_manifest(versions_root, model_version, exec_args.step5_bundle_dir.resolve())
                log_tags_safe({"status": "SUCCESS"})
            except Exception as e:
                metrics["finish_epoch_ms"] = int(time.time() * 1000)
                metrics["run_training_total_seconds"] = round(time.perf_counter() - t0, 3)
                from trainer_hightier.utils.source_manifest_v2 import finalize_cache_report_from_metrics

                finalized = finalize_cache_report_from_metrics(metrics)
                if finalized is not None:
                    metrics["cache_report_finalized_path"] = str(finalized.resolve())
                _finalize_training_reports(
                    exec_args,
                    metrics,
                    writer,
                    status="FAILED",
                    error=str(e)[:500],
                )
                log_params_safe(_mlflow_post_run_string_params(metrics))
                log_metrics_safe(_mlflow_scalar_metrics(metrics))
                _log_mlflow_whitelist_artifacts(exec_args, metrics, writer)
                log_tags_safe({"status": "FAILED", "error": str(e)[:500]})
                raise
    finally:
        logger.info(
            "[Done] trainer_hightier total elapsed %.3fs",
            round(time.perf_counter() - t0, 3),
        )


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="High-tier patron training (skeleton).")
    p.add_argument(
        "--ignore-caches",
        "--no-cache",
        action="store_true",
        dest="ignore_caches",
        help=(
            "Bypass preprocess disk caches (session-clean + bet-clean manifests) and recompute those steps. "
            "Also forces short-term PIT cache refresh in Step 3.5. "
            "Heavy IO on large L0; --no-cache is an alternate spelling."
        ),
    )
    p.add_argument(
        "--force-refresh-short-term-pit",
        action="store_true",
        dest="force_refresh_short_term_pit",
        help=(
            "Force Step 3.5 short-term PIT month-shard cache rebuild even when shard manifests match. "
            "Does not bypass session/bet preprocess caches unless combined with --ignore-caches."
        ),
    )
    p.add_argument(
        "--run-profile",
        type=str,
        default=DEFAULT_RUN_PROFILE_NAME,
        choices=list(list_run_profile_names()),
        metavar="NAME",
        help=(
            "DuckDB PRAGMAs + session/bet dedup hash bucket counts (see trainer_hightier.config.RUN_PROFILES). "
            "Use low_peak_memory or laptop_8g when Step 2b t_bet COPY hits DuckDB OOM."
        ),
    )
    p.add_argument(
        "--skip-walkaway-labels",
        action="store_true",
        dest="skip_walkaway_labels",
        help="Do not materialize walkaway_labels.parquet after cleaned t_bet (large pandas pass).",
    )
    p.add_argument(
        "--skip-bet-preprocess",
        action="store_true",
        dest="skip_bet_preprocess",
        help=(
            "Skip Step 2b (t_bet L0 to cleaned bet). Use when cleaned bet already exists under "
            "artifacts/cleaned; downstream steps still read the default paths."
        ),
    )
    p.add_argument(
        "--use-legacy-bet-segment",
        action="store_true",
        dest="use_legacy_bet_segment",
        help=(
            "Use ADT-segmented cleaned__gmwds_t_bet preprocess path instead of entity set v1 "
            "(default is entity set v1 when materialized)."
        ),
    )
    p.add_argument(
        "--use-sharded-labels-cache",
        action="store_true",
        dest="use_sharded_labels_cache",
        help=(
            "Step 2c: materialize walkaway labels via month×canonical_shard cache "
            "(default monolithic labels cache)."
        ),
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
            "With Step 3 enabled: do not re-materialize slow 180d Parquet before Feast "
            "(faster when that file is already up to date; trial 1h is not materialized on the training path)."
        ),
    )
    p.add_argument(
        "--start-from-features",
        action="store_true",
        dest="start_from_features",
        help=(
            "Skip Step 1-3 and run Step 4 only on an existing training parquet (default: "
            "artifacts/training_data/training_set.parquet). Use --features-input to override."
        ),
    )
    p.add_argument(
        "--features-input",
        type=Path,
        default=None,
        dest="features_input_parquet",
        help="Training features parquet path for Step 4 (optional; default is Step 3 output).",
    )
    p.add_argument(
        "--skip-step4",
        action="store_true",
        dest="skip_step4",
        help="Do not run Step 4 (arrange + train/val/test split by gaming_day).",
    )
    p.add_argument(
        "--skip-step5",
        action="store_true",
        dest="skip_step5",
        help="Do not train Step 5 LightGBM on Step 4 split Parquets.",
    )
    p.add_argument(
        "--skip-pre-train-feature-gate",
        action="store_true",
        dest="skip_pre_train_feature_gate",
        help="Do not run Step 4.5 short-term PIT train vs live replay gate before Step 5.",
    )
    p.add_argument(
        "--skip-step6",
        action="store_true",
        dest="skip_step6",
        help="Do not run Step 6 train/serve parity verification after Step 5.",
    )
    p.add_argument(
        "--step6-warning-only",
        action="store_true",
        dest="step6_warning_only",
        help="Run Step 6 but do not fail training when slow_gate fails (writes JSON only).",
    )
    p.add_argument(
        "--skip-optuna",
        action="store_true",
        dest="skip_optuna",
        help="Step 5: skip Optuna and use baseline LightGBM hyperparameters.",
    )
    p.add_argument(
        "--optuna-timeout-sec",
        type=float,
        default=_STEP5_CONFIG_DEFAULTS.optuna_timeout_sec,
        metavar="SEC",
        dest="optuna_timeout_sec",
        help=(
            f"Step 5 Optuna wall-clock budget in seconds "
            f"(default {_STEP5_CONFIG_DEFAULTS.optuna_timeout_sec:g} from Step5TrainConfig). "
            "Ignored with --skip-optuna."
        ),
    )
    p.add_argument(
        "--patron-sampling-ratio",
        type=float,
        default=None,
        dest="patron_sampling_ratio",
        metavar="FRACTION",
        help=(
            "Optional explicit patron sampling ratio recorded in run_report.json summary "
            "(e.g. 0.01 vs 0.10). When omitted, ratio may be derived from "
            "HighTierObjectiveConfig.theo_train_quantile when ADT bet filtering is enabled."
        ),
    )
    p.add_argument(
        "--disable-feast-retrieval-cache",
        action="store_true",
        dest="disable_feast_retrieval_cache",
        help=(
            "With Step 3 + month-batch Feast: do not reuse per-month per-feature-group cached Parquets; "
            "each month runs a single combined feature service retrieval (heavy). Ignored when --ignore-caches is set."
        ),
    )
    p.add_argument(
        "--no-partition-snapshot",
        action="store_true",
        dest="no_partition_snapshot",
        help=(
            "Deprecated in partition-only mode. This pipeline now requires partition shards; "
            "do not set this flag."
        ),
    )
    p.add_argument(
        "--partition-snapshot-dir",
        type=Path,
        default=None,
        help=(
            "Folder with t_bet__part_* / t_session__part_* monthly Parquets. "
            "When omitted, <repo>/data/partitions must exist or the run fails. "
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
        default=_PARTITION_INGRESS_DEFAULTS.backfill_month_count,
        metavar="N",
        help=(
            f"Include N preceding calendar months for each touched month "
            f"(default {_PARTITION_INGRESS_DEFAULTS.backfill_month_count} from PartitionIngressConfig)."
        ),
    )
    p.add_argument(
        "--feature-candidate-registry",
        type=Path,
        default=None,
        dest="feature_candidate_registry",
        help=(
            "YAML baseline feature ledger for Step 5 (default: trainer_hightier/contracts/"
            "feature_candidate_registry.yaml)."
        ),
    )
    p.add_argument(
        "--disable-auto-feast-apply",
        action="store_true",
        dest="disable_auto_feast_apply",
        help=(
            "When Step 3 runs: fail if Feast registry.db is missing instead of running `feast apply` under feast_repo. "
            "Use in CI or read-only clones after a pre-published registry."
        ),
    )
    return p


def _train_args_from_cli_namespace(ns: argparse.Namespace) -> HighTierTrainArgs:
    """Build :class:`HighTierTrainArgs` from parsed CLI flags and config SSOT defaults."""

    duckdb_rt, session_pre, bet_pre = configs_from_run_profile(get_run_profile(str(ns.run_profile)))
    corr = tuple(str(x).strip() for x in (ns.partition_correction_months or []) if str(x).strip())
    return HighTierTrainArgs(
        output_dir=DEFAULT_MODEL_DIR,
        ignore_caches=bool(ns.ignore_caches),
        force_refresh_short_term_pit=bool(ns.force_refresh_short_term_pit),
        skip_bet_preprocess=bool(ns.skip_bet_preprocess),
        use_entity_set_v1=not bool(ns.use_legacy_bet_segment),
        use_sharded_labels_cache=bool(ns.use_sharded_labels_cache),
        materialize_walkaway_labels=not bool(ns.skip_walkaway_labels),
        build_training_dataset=not bool(ns.skip_training_dataset),
        training_materialize_derived=not bool(ns.skip_training_materialize_derived),
        feast_retrieval_cache=not bool(ns.disable_feast_retrieval_cache),
        auto_feast_apply=not bool(ns.disable_auto_feast_apply),
        duckdb_runtime=duckdb_rt,
        session_preprocess=session_pre,
        bet_preprocess=bet_pre,
        partition_snapshot_dir=Path(ns.partition_snapshot_dir).resolve() if ns.partition_snapshot_dir else None,
        partition_inventory_previous_manifest=(
            Path(ns.partition_inventory_previous).resolve() if ns.partition_inventory_previous else None
        ),
        partition_correction_months=corr,
        partition_backfill_month_count=int(ns.partition_backfill_count),
        run_step4=not bool(ns.skip_step4),
        start_from_features=bool(ns.start_from_features),
        features_input_parquet=(
            Path(ns.features_input_parquet).resolve() if ns.features_input_parquet else None
        ),
        step4_split=Step4SplitConfig(),
        step5=replace(
            _STEP5_CONFIG_DEFAULTS,
            run_step5=not bool(ns.skip_step5),
            skip_optuna=bool(ns.skip_optuna),
            optuna_timeout_sec=float(ns.optuna_timeout_sec),
        ),
        pre_train_gate=replace(
            _PRE_TRAIN_GATE_DEFAULTS,
            run_pre_train_gate=not bool(ns.skip_pre_train_feature_gate),
        ),
        step6=replace(
            _STEP6_CONFIG_DEFAULTS,
            run_step6=not bool(ns.skip_step6),
            hard_fail_slow_gate=not bool(ns.step6_warning_only),
        ),
        feature_candidate_registry=(
            Path(ns.feature_candidate_registry).resolve() if ns.feature_candidate_registry else None
        ),
        run_profile_name=str(ns.run_profile),
        patron_sampling_ratio=float(ns.patron_sampling_ratio) if ns.patron_sampling_ratio is not None else None,
    )


def main() -> None:
    """Parse cache flag and invoke :func:`run_training` with package defaults."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ns = _build_argparser().parse_args()
    if bool(ns.no_partition_snapshot):
        raise ValueError(
            "--no-partition-snapshot is no longer supported: trainer_hightier is now partition-only. "
            "Provide --partition-snapshot-dir, or omit it to use <repo>/data/partitions."
        )
    run_training(_train_args_from_cli_namespace(ns))


if __name__ == "__main__":
    main()
