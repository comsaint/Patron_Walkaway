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
import hashlib
import importlib
import json
import logging
import shutil
import subprocess
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
from trainer_hightier.config import (
    BetPreprocessConfig,
    CanonicalMappingConfig,
    DEFAULT_MODEL_DIR,
    DEFAULT_RANDOM_SEED,
    FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME,
    DEFAULT_RUN_PROFILE_NAME,
    DEFAULT_TRAINING_FEATURE_SERVICE,
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    MLFLOW_EXPERIMENT_TRAIN_HIGHTIER,
    MLFLOW_HIGHTIER_ARTIFACT_PREFIX,
    MID_TERM_SNAPSHOT_MAX_LOOKBACK_DAYS,
    MID_TERM_SNAPSHOT_SCOPE_TRAINING,
    PartitionIngressConfig,
    Step5TrainConfig,
    Step6ParityConfig,
    SessionPreprocessConfig,
    Step4SplitConfig,
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
from trainer_hightier.core.model_bundle_paths import safe_version_subdirectory, write_latest_model_manifest
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
from trainer_hightier.utils.walkaway_labels import (
    default_walkaway_labels_parquet_path,
    materialize_walkaway_labels_from_cleaned_bet,
)

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

RUN_SUMMARY_FILENAME: Final[str] = "run_summary.json"
METRICS_DETAILED_FILENAME: Final[str] = "metrics_detailed.json"
PIPELINE_DEBUG_FILENAME: Final[str] = "pipeline_debug.json"
SPLIT_REPORT_FILENAME: Final[str] = "split_report.json"

_STEP5_CONFIG_DEFAULTS = Step5TrainConfig()
_STEP6_CONFIG_DEFAULTS = Step6ParityConfig()
_PARTITION_INGRESS_DEFAULTS = PartitionIngressConfig()


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
    # When ``build_training_dataset``: materialize trial 1h + slow 180d Parquets before Feast (default on; very heavy).
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
    # Step 4: deterministic arrange + time split on ``gaming_day`` (after Step 3 or --start-from-features).
    run_step4: bool = True
    step4_split: Step4SplitConfig = field(default_factory=Step4SplitConfig)
    # When True: skip Step 1-3; require an existing training features parquet and run Step 4 only.
    start_from_features: bool = False
    features_input_parquet: Path | None = None
    # Step 5: LightGBM + optional Optuna on Step 4 split Parquets.
    step5: Step5TrainConfig = field(default_factory=Step5TrainConfig)
    # Step 6: train/serve parity verification after Step 5 bundle materialization.
    step6: Step6ParityConfig = field(default_factory=Step6ParityConfig)
    # Feature ledger for Step 5 baseline columns; ``None`` uses default contracts YAML path.
    feature_candidate_registry: Path | None = None
    #: ``--run-profile`` CLI name (MLflow param); programmatic callers default to :data:`DEFAULT_RUN_PROFILE_NAME`.
    run_profile_name: str = DEFAULT_RUN_PROFILE_NAME
    #: Explicit patron sampling ratio for logs (e.g. ``0.01`` vs ``0.10``). When ``None`` and ADT bet filter is on,
    #: :func:`build_run_summary` derives approximate segment fraction as ``1 - objective.theo_train_quantile``.
    patron_sampling_ratio: float | None = None


def _repo_root() -> Path:
    """Return repository root (parent of ``trainer_hightier`` package directory)."""

    return Path(__file__).resolve().parents[1]


def _epoch_ms_to_iso_z(epoch_ms: int | None) -> str | None:
    """Render epoch milliseconds as UTC ISO-8601 ``…Z`` string."""

    if epoch_ms is None:
        return None
    try:
        ms = int(epoch_ms)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolved_patron_sampling_ratio(args: HighTierTrainArgs) -> tuple[float | None, str]:
    """Return ``(ratio, source)`` for run-summary ``data_scope`` (explicit vs ADT-derived)."""

    if args.patron_sampling_ratio is not None:
        return float(args.patron_sampling_ratio), "explicit"
    if args.filter_bets_by_adt_quantile:
        q = float(args.objective.theo_train_quantile)
        if 0.0 < q < 1.0:
            return round(1.0 - q, 8), "adt_quantile_derived"
    return None, "unknown"


def _feature_list_sha256_hex(columns: object) -> str | None:
    """Stable SHA256 over sorted feature column names (hex digest, no prefix)."""

    if not isinstance(columns, list):
        return None
    names = [str(x) for x in columns]
    payload = json.dumps(sorted(names), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_relative_to_repo(path_str: str | None, *, repo_root: Path) -> str | None:
    """Best-effort repo-relative path string for logs."""

    if path_str is None:
        return None
    raw = str(path_str).strip()
    if not raw:
        return None
    try:
        p = Path(raw).resolve()
        rel = p.relative_to(repo_root.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return raw.replace("\\", "/")


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
        "basis": "gaming_day",
        "train_day_fraction": report.get("train_day_fraction"),
        "val_day_fraction": report.get("val_day_fraction"),
        "distinct_gaming_days": report.get("distinct_gaming_days"),
        "by_split": by_split,
    }


def build_run_summary(metrics: dict[str, Any], args: HighTierTrainArgs) -> dict[str, Any]:
    """Build compact ``run_summary.json`` payload (cross-run comparison)."""

    repo_root = _repo_root()
    run_id = str(metrics.get("model_version") or "").strip() or "unknown_run"
    started = _epoch_ms_to_iso_z(metrics.get("start_epoch_ms")) if isinstance(metrics.get("start_epoch_ms"), int) else None
    finished = _epoch_ms_to_iso_z(metrics.get("finish_epoch_ms")) if isinstance(metrics.get("finish_epoch_ms"), int) else None
    duration_sec = metrics.get("run_training_total_seconds")
    patron_ratio, patron_src = _resolved_patron_sampling_ratio(args)
    warn_keys = ("val_ap", "val_precision", "test_ap", "test_precision")
    for wk in warn_keys:
        if wk not in metrics:
            logger.warning("[run_summary] missing metrics[%r]; cross-run comparison degraded.", wk)
    feat_cols = metrics.get("step5_feature_columns")
    n_feat = len(feat_cols) if isinstance(feat_cols, list) else None
    feat_hash = _feature_list_sha256_hex(feat_cols) if isinstance(feat_cols, list) else None
    thr = metrics.get("step5_threshold")
    if "step5_optuna_skipped" not in metrics:
        opt_enabled = False
    else:
        opt_enabled = not bool(metrics.get("step5_optuna_skipped"))
    summary = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": finished,
        "duration_sec": float(duration_sec) if isinstance(duration_sec, (int, float)) else None,
        "data_scope": {
            "population_scope": "adt_filtered_bets_when_enabled",
            "filter_bets_by_adt_quantile": bool(args.filter_bets_by_adt_quantile),
            "theo_train_quantile": float(args.objective.theo_train_quantile),
            "patron_sampling_ratio": patron_ratio,
            "patron_sampling_ratio_source": patron_src,
        },
        "model": {"algorithm": "lightgbm", "n_features_used": n_feat, "feature_list_sha256_hex": feat_hash},
        "thresholding": {
            "policy": "min_precision",
            "policy_param": {"min_precision": float(args.objective.min_precision)},
            "selected_threshold": float(thr) if isinstance(thr, (int, float)) else None,
        },
        "metrics": {
            "val": {
                "ap": metrics.get("val_ap"),
                "precision": metrics.get("val_precision"),
                "recall": metrics.get("val_recall"),
                "f1": metrics.get("val_f1"),
                "samples": metrics.get("val_samples"),
                "positives": metrics.get("val_positives"),
                "alerts": metrics.get("val_alerts"),
                "alerts_per_hour": metrics.get("val_alerts_per_hour"),
            },
            "test": {
                "ap": metrics.get("test_ap"),
                "precision": metrics.get("test_precision"),
                "recall": metrics.get("test_recall"),
                "f1": metrics.get("test_f1"),
                "samples": metrics.get("test_samples"),
                "positives": metrics.get("test_positives"),
                "alerts": metrics.get("test_alerts"),
                "alerts_per_hour": metrics.get("test_alerts_per_hour"),
            },
        },
        "optimization": {
            "enabled": opt_enabled,
            "backend": "optuna",
            "max_time_sec_configured": metrics.get("optuna_max_time_sec_configured"),
            "max_trials_configured": metrics.get("optuna_max_trials_configured"),
            "wall_time_sec_actual": metrics.get("optuna_wall_time_sec_actual"),
            "trials_completed": metrics.get("optuna_trials_completed"),
            "trials_total": metrics.get("optuna_trials_total"),
            "stopping_reason": metrics.get("optuna_stopping_reason"),
            "best_value": metrics.get("optuna_best_value"),
        },
        "git_commit_short": _git_short_head(repo_root),
        "run_profile": str(args.run_profile_name),
        "split_periods": metrics.get("step4_split_periods"),
    }
    if patron_ratio is None and patron_src == "unknown":
        logger.warning(
            "[run_summary] patron_sampling_ratio unknown; set HighTierTrainArgs.patron_sampling_ratio "
            "or enable filter_bets_by_adt_quantile with theo_train_quantile in (0,1).",
        )
    return summary


def build_metrics_detailed(metrics: dict[str, Any]) -> dict[str, Any]:
    """Build ``metrics_detailed.json`` (train/val/test blocks + feature list)."""

    run_id = str(metrics.get("model_version") or "").strip() or "unknown_run"

    def _split(prefix: str) -> dict[str, Any]:
        keys = ("ap", "precision", "recall", "f1")
        return {k: metrics.get(f"{prefix}_{k}") for k in keys}

    return {
        "run_id": run_id,
        "split_metrics": {"train": _split("train"), "val": _split("val"), "test": _split("test")},
        "threshold_analysis": {
            "selection_policy": f"min_precision={metrics.get('step5_min_precision')}",
            "selected_threshold": metrics.get("step5_threshold"),
            "val_pick_feasible": metrics.get("step5_val_pick_feasible"),
        },
        "budget_points": {
            "alerts_per_hour": {
                "train": metrics.get("train_alerts_per_hour"),
                "val": metrics.get("val_alerts_per_hour"),
                "test": metrics.get("test_alerts_per_hour"),
            }
        },
        "feature_columns": metrics.get("step5_feature_columns"),
        "candidate_registry": metrics.get("candidate_registry"),
        "split_periods": metrics.get("step4_split_periods"),
    }


def build_pipeline_debug(metrics: dict[str, Any]) -> dict[str, Any]:
    """Build ``pipeline_debug.json`` (timings, caches, lineage paths)."""

    repo_root = _repo_root()
    run_id = str(metrics.get("model_version") or "").strip() or "unknown_run"
    return {
        "run_id": run_id,
        "cache": {
            "session_clean_cache_hit": metrics.get("session_clean_cache_hit"),
            "bet_base_clean_cache_hit": metrics.get("bet_base_clean_cache_hit"),
            "bet_segment_clean_cache_hit": metrics.get("bet_segment_clean_cache_hit"),
            "bet_clean_cache_hit": metrics.get("bet_clean_cache_hit"),
        },
        "partition": {
            "inventory_fingerprint_sha256_hex": metrics.get("partition_inventory_fingerprint_sha256_hex"),
            "recompute_months": metrics.get("partition_recompute_months"),
            "snapshot_dir_effective": metrics.get("partition_snapshot_dir_effective"),
            "inventory_baseline_path": metrics.get("partition_inventory_baseline_path"),
        },
        "timings_sec": {
            "prepare_training_frame": metrics.get("prepare_training_frame_seconds"),
            "build_training_dataset": metrics.get("build_training_dataset_seconds"),
            "step4": metrics.get("step4_seconds"),
            "step5": metrics.get("step5_seconds"),
            "run_training_total": metrics.get("run_training_total_seconds"),
            "main_trainer_fe_materialize": metrics.get("main_trainer_fe_materialize_sec"),
            "main_trainer_fe_enrich": metrics.get("main_trainer_fe_enrich_sec"),
        },
        "feast_auto_apply": metrics.get("feast_auto_apply"),
        "split_periods": metrics.get("step4_split_periods"),
        "artifacts": {
            "model_path": _path_relative_to_repo(
                str(metrics.get("model_path") or metrics.get("step5_model_path") or ""),
                repo_root=repo_root,
            ),
            "training_metrics_path": _path_relative_to_repo(
                str(metrics.get("training_metrics_path") or metrics.get("step5_training_metrics_path") or ""),
                repo_root=repo_root,
            ),
            "step4_split_report": _path_relative_to_repo(
                str(metrics.get("step4_split_report_bundle") or metrics.get("step4_split_report") or ""),
                repo_root=repo_root,
            ),
            "step4_splits_dir": _path_relative_to_repo(str(metrics.get("step4_splits_dir") or ""), repo_root=repo_root),
            "main_trainer_training_parquet_for_step4": _path_relative_to_repo(
                str(metrics.get("main_trainer_training_parquet_for_step4") or ""),
                repo_root=repo_root,
            ),
        },
        "session_dedup_hash_buckets_effective": metrics.get("session_dedup_hash_buckets_effective"),
        "bet_dedup_hash_buckets_effective": metrics.get("bet_dedup_hash_buckets_effective"),
    }


def _materialize_split_report_in_bundle(
    bundle_dir: Path,
    *,
    source_report: str | Path | None,
) -> Path | None:
    """Copy Step 4 ``split_report.json`` into the model bundle directory."""

    if source_report is None:
        return None
    src = Path(source_report).resolve()
    if not src.is_file():
        logger.warning("[Step 7] split report missing at %s; skip bundle copy.", src)
        return None
    dest = Path(bundle_dir).resolve() / SPLIT_REPORT_FILENAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    logger.info("[Step 7] copied split report to %s", dest.resolve())
    return dest


def write_hightier_training_logs(parent_dir: Path, metrics: dict[str, Any], args: HighTierTrainArgs) -> None:
    """Write ``run_summary.json``, ``metrics_detailed.json``, ``pipeline_debug.json`` under ``parent_dir``."""

    pd = Path(parent_dir).resolve()
    pd.mkdir(parents=True, exist_ok=True)
    rs = build_run_summary(metrics, args)
    md = build_metrics_detailed(metrics)
    dbg = build_pipeline_debug(metrics)
    (pd / RUN_SUMMARY_FILENAME).write_text(json.dumps(rs, indent=2, default=str), encoding="utf-8")
    (pd / METRICS_DETAILED_FILENAME).write_text(json.dumps(md, indent=2, default=str), encoding="utf-8")
    (pd / PIPELINE_DEBUG_FILENAME).write_text(json.dumps(dbg, indent=2, default=str), encoding="utf-8")
    logger.info(
        "[Step 7b] wrote %s, %s, %s under %s",
        RUN_SUMMARY_FILENAME,
        METRICS_DETAILED_FILENAME,
        PIPELINE_DEBUG_FILENAME,
        pd,
    )


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


def _log_mlflow_whitelist_artifacts(args: HighTierTrainArgs, metrics: dict[str, Any]) -> None:
    """Log minimal training artifacts under :data:`MLFLOW_HIGHTIER_ARTIFACT_PREFIX`."""

    prefix = MLFLOW_HIGHTIER_ARTIFACT_PREFIX
    mbd = metrics.get("model_bundle_dir")
    rp = Path(mbd).resolve() / "run_report.json" if mbd else Path(args.output_dir).resolve() / "run_report.json"
    if rp.is_file():
        log_artifact_safe(rp, artifact_path=prefix)
    smp = metrics.get("training_metrics_path") or metrics.get("step5_training_metrics_path")
    if smp:
        p = Path(str(smp))
        if p.is_file():
            log_artifact_safe(p, artifact_path=prefix)
    smp_model = metrics.get("model_path") or metrics.get("step5_model_path")
    if smp_model:
        pm = Path(str(smp_model))
        if pm.is_file():
            log_artifact_safe(pm, artifact_path=prefix)
    sr = metrics.get("step4_split_report_bundle") or metrics.get("step4_split_report")
    if sr:
        sp = Path(str(sr))
        if sp.is_file():
            log_artifact_safe(sp, artifact_path=prefix)
    bundle_root = rp.parent
    for fname in (RUN_SUMMARY_FILENAME, METRICS_DETAILED_FILENAME, PIPELINE_DEBUG_FILENAME):
        aux = bundle_root / fname
        if aux.is_file():
            log_artifact_safe(aux, artifact_path=prefix)


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
    inv_fp, bet_partition_paths, session_partition_paths, recompute_months = _materialize_partition_inventory(
        manifests_dir=manifests_dir,
        correction_months=args.partition_correction_months,
        backfill_month_count=args.partition_backfill_month_count,
        previous_manifest_path=baseline_used,
        snapshot_dir=snap_dir,
    )

    sess_report = _ingest.validate_partition_session_ingress_or_raise(session_partition_paths)
    logger.info(
        "[Step 1] t_session partition shards OK: %d file(s), %s rows (metadata); "
        "t_bet deferred until after session clean / downstream",
        len(session_partition_paths),
        sess_report.session.num_rows,
    )

    ordered_sess = tuple(sorted((Path(p).resolve() for p in session_partition_paths), key=str))
    session_primary = ordered_sess[0]
    session_extras = ordered_sess[1:]

    cleaned_path = _hpre.default_cleaned_session_parquet_path()
    use_preprocess_caches = not args.ignore_caches
    ses_cache_ok = use_preprocess_caches and _hpre.session_clean_cache_is_hit(
        session_primary,
        cleaned_path,
        dedup_hash_buckets=args.session_preprocess.dedup_hash_buckets,
        extra_source_session_parquets=session_extras or None,
        partition_inventory_fingerprint_sha256_hex=inv_fp,
    )
    if metrics is not None:
        metrics["session_clean_cache_hit"] = bool(ses_cache_ok)
        metrics["partition_inventory_fingerprint_sha256_hex"] = inv_fp
        metrics["partition_recompute_months"] = list(recompute_months)
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
            session_primary,
            cleaned_path,
            cfg=args.session_preprocess,
            duckdb_runtime=args.duckdb_runtime,
            extra_partition_sources=session_extras or None,
        )
        if metrics is not None:
            metrics["session_dedup_hash_buckets_effective"] = int(session_dedup_effective)
        _hpre.write_session_clean_cache_manifest(
            session_primary,
            out_parquet,
            dedup_hash_buckets=int(session_dedup_effective),
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

    effective_bet_cfg = args.bet_preprocess
    if want_adt_bets and args.canonical_mapping.enabled:
        effective_bet_cfg = replace(
            args.bet_preprocess,
            adt_filter_quantile=q_thr,
            patron_profile_csv=profile_csv_path,
            canonical_mapping_parquet=mapping_parquet_path,
            adt_allowed_players_parquet=allowed_players_pq,
        )

    if not args.skip_bet_preprocess and bet_partition_paths:
        bet_report = _ingest.validate_partition_bet_ingress_or_raise(bet_partition_paths)
        logger.info(
            "[Step 1] t_bet partition shards OK: %d file(s), %s rows (metadata)",
            len(bet_partition_paths),
            bet_report.num_rows,
        )
        ordered_bets = tuple(sorted((Path(p).resolve() for p in bet_partition_paths), key=str))
        bet_primary = ordered_bets[0]
        bet_extras = ordered_bets[1:]
        reg_yaml = (
            Path(args.bet_preprocess.preprocess_registry_yaml)
            if args.bet_preprocess.preprocess_registry_yaml is not None
            else _hpre.default_preprocess_registry_yaml_path()
        )
        merged_bet_sources = _hbet.merge_bet_source_paths(bet_primary, bet_extras or None)
        base_bet_cfg = replace(
            effective_bet_cfg,
            adt_filter_quantile=None,
            patron_profile_csv=None,
            canonical_mapping_parquet=None,
            adt_allowed_players_parquet=None,
        )
        bet_extras_arg = bet_extras or None

        if want_adt_bets and args.canonical_mapping.enabled and allowed_players_pq is not None:
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
            base_hit = use_preprocess_caches and _hbet.bet_base_clean_cache_is_hit(
                merged_bet_sources,
                base_bet_path,
                preprocess_registry_yaml=reg_yaml,
                dedup_hash_buckets=base_bet_cfg.dedup_hash_buckets,
                cleaned_session_parquet=cleaned_path,
                partition_inventory_fingerprint_sha256_hex=inv_fp,
            )
            seg_hit = use_preprocess_caches and _hbet.bet_clean_cache_is_hit(
                bet_primary,
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
                bet_dedup_eff = int(base_bet_cfg.dedup_hash_buckets)
                mf_bkt = _hbet.bet_base_manifest_dedup_hash_buckets(base_bet_path)
                if base_hit and mf_bkt is not None:
                    bet_dedup_eff = int(mf_bkt)
                if not base_hit:
                    _, bet_dedup_eff = _hpre.preprocess_bets_from_parquet_streaming(
                        bet_primary,
                        base_bet_path,
                        cfg=base_bet_cfg,
                        duckdb_runtime=args.duckdb_runtime,
                        extra_partition_sources=bet_extras_arg,
                    )
                    _hbet.write_bet_base_clean_cache_manifest(
                        merged_bet_sources,
                        base_bet_path,
                        preprocess_registry_yaml=reg_yaml,
                        dedup_hash_buckets=int(bet_dedup_eff),
                        cleaned_session_parquet=cleaned_path,
                        partition_inventory_fingerprint_sha256_hex=inv_fp,
                    )
                elif not seg_hit and mf_bkt is None:
                    logger.warning(
                        "[Step 2b] bet base cache hit without readable manifest buckets; "
                        "using nominal dedup_hash_buckets=%s for segment manifest fingerprint",
                        bet_dedup_eff,
                    )
                _hbet.segment_cleaned_bet_from_base_parquet(
                    base_bet_path,
                    allowed_players_pq,
                    cleaned_bet_path,
                    duckdb_runtime=args.duckdb_runtime,
                )
                _hbet.write_bet_clean_cache_manifest(
                    bet_primary,
                    cleaned_bet_path,
                    preprocess_registry_yaml=reg_yaml,
                    dedup_hash_buckets=int(bet_dedup_eff),
                    cleaned_session_parquet=cleaned_path,
                    adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                    patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                    canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                    adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                    extra_source_bet_parquets=bet_extras_arg,
                    bet_base_cleaned_parquet=base_bet_path,
                    partition_inventory_fingerprint_sha256_hex=inv_fp,
                )
                n_b = _hbet.partitioned_cleaned_bet_total_rows(cleaned_bet_path)
                logger.info(
                    "[Step 2b] bet preprocess OK (base+ADT segment): cleaned rows=%d; written %s",
                    n_b,
                    cleaned_bet_path,
                )
                if metrics is not None:
                    metrics["bet_dedup_hash_buckets_effective"] = int(bet_dedup_eff)
        else:
            bet_cache_ok = use_preprocess_caches and _hbet.bet_clean_cache_is_hit(
                bet_primary,
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
                out_b, bet_dedup_eff = _hpre.preprocess_bets_from_parquet_streaming(
                    bet_primary,
                    cleaned_bet_path,
                    cfg=effective_bet_cfg,
                    duckdb_runtime=args.duckdb_runtime,
                    extra_partition_sources=bet_extras_arg,
                )
                _hbet.write_bet_clean_cache_manifest(
                    bet_primary,
                    out_b,
                    preprocess_registry_yaml=reg_yaml,
                    dedup_hash_buckets=int(bet_dedup_eff),
                    cleaned_session_parquet=cleaned_path,
                    adt_filter_quantile=effective_bet_cfg.adt_filter_quantile,
                    patron_profile_csv=effective_bet_cfg.patron_profile_csv,
                    canonical_mapping_parquet=effective_bet_cfg.canonical_mapping_parquet,
                    adt_allowed_players_parquet=effective_bet_cfg.adt_allowed_players_parquet,
                    extra_source_bet_parquets=bet_extras_arg,
                    partition_inventory_fingerprint_sha256_hex=inv_fp,
                )
                n_b = _hbet.partitioned_cleaned_bet_total_rows(out_b)
                logger.info(
                    "[Step 2b] bet preprocess OK: cleaned rows=%d; written %s",
                    n_b,
                    out_b,
                )
                if metrics is not None:
                    metrics["bet_dedup_hash_buckets_effective"] = int(bet_dedup_eff)
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
            labels_out = args.objective.labels_parquet or default_walkaway_labels_parquet_path()
            materialize_walkaway_labels_from_cleaned_bet(
                cleaned_bet_parquet=cleaned_bet_path,
                canonical_mapping_parquet=mapping_parquet_path,
                out_parquet=labels_out,
                duckdb_runtime=args.duckdb_runtime,
            )
            logger.info("[Step 2c] walkaway labels written %s", labels_out.resolve())


def _resolve_features_parquet(args: HighTierTrainArgs) -> Path:
    """Return training features Parquet path (Step 3 default or explicit override)."""

    if args.features_input_parquet is not None:
        return Path(args.features_input_parquet).resolve()
    return _b3.DEFAULT_OUTPUT.resolve()


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
    if not fe_baseline:
        return base_training_parquet

    raw_rows = load_registry_raw_feature_dicts(reg_p)
    cadence_mod = importlib.import_module("trainer_hightier.feature_experiment.feature_cadence")
    fe_mod = importlib.import_module("trainer_hightier.feature_experiment.materialize_fe_derived")
    mid_mod = importlib.import_module("trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot")
    en_mod = importlib.import_module("trainer_hightier.feature_experiment.dataset_enrich")
    freg = importlib.import_module("trainer_hightier.feature_experiment.feature_registry")
    freg.set_candidate_registry_path(reg_p)

    audit = cadence_mod.build_feature_cadence_audit(snap, baseline, raw_rows=raw_rows)
    fe_split = cadence_mod.classify_model_fe_features(snap, fe_baseline, raw_rows=raw_rows)
    short_cols = cadence_mod.short_term_enrich_columns_with_dependencies(
        fe_split["short_term"],
        fe_split["mid_term"],
    )
    mid_cols = fe_split["mid_term"]

    cleaned = _hpre.default_cleaned_bet_parquet_path().resolve()
    out_dir = base_training_parquet.parent
    audit_path = out_dir / "feature_cadence_audit.json"
    audit_path.write_text(json.dumps(audit, indent=2, sort_keys=True), encoding="utf-8")

    fe_short_out = out_dir / "_main_trainer_fe_short_term.parquet"
    mid_snap_out = out_dir / "_main_trainer_mid_term_daily_snapshot.parquet"
    enriched = out_dir / "training_set_fe_enriched.parquet"

    t_m0 = time.perf_counter()
    if short_cols:
        fe_mod.materialize_fe_derived_short_term_parquet(
            cleaned_bet_parquet=cleaned,
            training_parquet_for_bet_ids=base_training_parquet,
            out_parquet=fe_short_out,
            duckdb_runtime=args.duckdb_runtime,
            short_term_columns=short_cols,
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
            anchor_gaming_day_start=anchor_start,
            anchor_gaming_day_end=anchor_end,
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
                anchor_gaming_day_start=anchor_start,
                anchor_gaming_day_end=anchor_end,
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
        metrics["main_trainer_mid_term_snapshot_parquet"] = str(mid_snap_out.resolve()) if mid_cols else None
        metrics["feature_cadence_audit"] = audit
        metrics["feature_cadence_audit_path"] = str(audit_path.resolve())
        if mid_meta:
            metrics["main_trainer_mid_term_snapshot_meta"] = mid_meta
    logger.info(
        "[Step 3.5] cadence enrich: short=%d mid=%d -> %s (short %.3fs, mid %.3fs, enrich %.3fs)",
        len(short_cols),
        len(mid_cols),
        enriched.name,
        mat_short_sec,
        mat_mid_sec,
        enr_sec,
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
    fp_eff = _ensure_fe_enriched_training_parquet_for_step4(args, fp, metrics=metrics)
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
        logger.info(
            "[Step 5] training: train_lgbm_from_splits (splits_dir=%s, n_features=%d, skip_optuna=%s, out_dir=%s) …",
            splits_dir.resolve(),
            len(feat_cols),
            bool(args.step5.skip_optuna),
            step5_out_dir.resolve(),
        )
        result = _b5.train_lgbm_from_splits(
            splits_dir=splits_dir,
            duckdb_runtime=args.duckdb_runtime,
            objective_min_precision=float(args.objective.min_precision),
            random_seed=int(args.random_seed),
            step5=args.step5,
            output_dir=step5_out_dir,
            feature_columns=feat_cols,
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
    tm_path = bd / _b5.DEFAULT_METRICS_FILENAME
    if tm_path.is_file():
        try:
            body = json.loads(tm_path.read_text(encoding="utf-8"))
            if isinstance(body, dict):
                body["feature_candidate_registry_snapshot"] = dest.name
                body["feature_candidate_registry_sha256"] = digest
                body["feature_candidate_registry_frozen_from"] = str(src)[:500]
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
        alt_short = bd / "_main_trainer_fe_short_term.parquet"
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
            anchor_max = mid_meta.get("mid_term_anchor_gaming_day_max")
            if anchor_max is None:
                anchor_max = mid_meta.get("anchor_gaming_day_max")
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


def _run_step6_parity_verification(
    args: HighTierTrainArgs,
    metrics: dict[str, Any],
    *,
    bundle_dir: Path,
) -> None:
    """Run Step 6 parity gate and persist JSON beside the model bundle."""
    if not args.step6.run_step6:
        logger.info("[Step 6] skipped (step6.run_step6=False)")
        return
    from datetime import date as _date_cls

    import importlib.util

    repo = _repo_root()
    _sync_feast_online_for_step6(repo, metrics, bundle_dir=bundle_dir)

    step06_path = Path(__file__).resolve().parent / "06_verify_training_serving_parity.py"
    spec = importlib.util.spec_from_file_location("trainer_hightier_step06_verify", step06_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Step 6 module from {step06_path}")
    step06_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step06_mod)

    splits_dir = args.step4_split.splits_output_dir
    if splits_dir is None:
        test_parquet = repo / "trainer_hightier" / "artifacts" / "training_data" / "splits" / "test.parquet"
    else:
        test_parquet = Path(splits_dir).resolve() / "test.parquet"
    cleaned_bet = repo / "trainer_hightier" / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet"
    feast_repo = repo / "trainer_hightier" / "feast_repo"
    out_json = Path(bundle_dir).resolve() / "feature_parity_verification.json"
    report = step06_mod.build_report_from_config(
        model_dirs=[Path(bundle_dir).resolve()],
        test_parquet=test_parquet,
        cleaned_bet_root=cleaned_bet,
        feast_repo=feast_repo,
        as_of_date=_date_cls.today(),
        parity_cfg=args.step6,
    )
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
            if exec_args.step5_bundle_dir is not None:
                metrics["model_version"] = model_version
                metrics["model_bundle_dir"] = str(exec_args.step5_bundle_dir.resolve())
            try:
                _run_training_execute_steps(exec_args, metrics)
                metrics["finish_epoch_ms"] = int(time.time() * 1000)
                metrics["run_training_total_seconds"] = round(time.perf_counter() - t0, 3)
                if exec_args.step5_bundle_dir is not None:
                    bundle_sr = _materialize_split_report_in_bundle(
                        exec_args.step5_bundle_dir.resolve(),
                        source_report=metrics.get("step4_split_report"),
                    )
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
                rp_parent = exec_args.step5_bundle_dir.resolve() if exec_args.step5_bundle_dir else versions_root
                rp = rp_parent / "run_report.json"
                rp.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
                logger.info("[Step 7] run_training skeleton finished (report %s)", rp.resolve())
                write_hightier_training_logs(rp_parent, metrics, exec_args)
                log_params_safe(_mlflow_post_run_string_params(metrics))
                log_metrics_safe(_mlflow_scalar_metrics(metrics))
                _log_mlflow_whitelist_artifacts(exec_args, metrics)
                if (
                    exec_args.step5_bundle_dir is not None
                    and (exec_args.step5_bundle_dir.resolve() / _b5.DEFAULT_MODEL_FILENAME).is_file()
                ):
                    write_latest_model_manifest(versions_root, model_version, exec_args.step5_bundle_dir.resolve())
                log_tags_safe({"status": "SUCCESS"})
            except Exception as e:
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
            "Heavy IO on large L0; --no-cache is an alternate spelling."
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
            "Optional explicit patron sampling ratio recorded in run_summary.json "
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
        skip_bet_preprocess=bool(ns.skip_bet_preprocess),
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
