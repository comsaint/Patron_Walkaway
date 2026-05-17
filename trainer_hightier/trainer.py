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
)
from trainer_hightier.config import (
    BetPreprocessConfig,
    CanonicalMappingConfig,
    DEFAULT_MODEL_DIR,
    DEFAULT_RANDOM_SEED,
    DEFAULT_RUN_PROFILE_NAME,
    DEFAULT_TRAINING_FEATURE_SERVICE,
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    MLFLOW_EXPERIMENT_TRAIN_HIGHTIER,
    MLFLOW_HIGHTIER_ARTIFACT_PREFIX,
    PartitionIngressConfig,
    Step5TrainConfig,
    SessionPreprocessConfig,
    Step4SplitConfig,
    configs_from_run_profile,
    get_run_profile,
    list_run_profile_names,
)
from trainer.core.model_bundle_paths import (
    safe_version_subdirectory,
    write_latest_model_manifest,
)
from trainer.core.mlflow_utils import (
    log_artifact_safe,
    log_metrics_safe,
    log_params_safe,
    log_tags_safe,
    safe_start_run,
    warm_up_mlflow_run_safe,
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
_b4 = importlib.import_module("trainer_hightier.04_split_dataset")
_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")


logger = logging.getLogger("trainer_hightier")

RUN_SUMMARY_FILENAME: Final[str] = "run_summary.json"
METRICS_DETAILED_FILENAME: Final[str] = "metrics_detailed.json"
PIPELINE_DEBUG_FILENAME: Final[str] = "pipeline_debug.json"

_STEP5_CONFIG_DEFAULTS = Step5TrainConfig()
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
        "artifacts": {
            "model_path": _path_relative_to_repo(
                str(metrics.get("model_path") or metrics.get("step5_model_path") or ""),
                repo_root=repo_root,
            ),
            "training_metrics_path": _path_relative_to_repo(
                str(metrics.get("training_metrics_path") or metrics.get("step5_training_metrics_path") or ""),
                repo_root=repo_root,
            ),
            "step4_split_report": _path_relative_to_repo(str(metrics.get("step4_split_report") or ""), repo_root=repo_root),
            "step4_splits_dir": _path_relative_to_repo(str(metrics.get("step4_splits_dir") or ""), repo_root=repo_root),
            "main_trainer_training_parquet_for_step4": _path_relative_to_repo(
                str(metrics.get("main_trainer_training_parquet_for_step4") or ""),
                repo_root=repo_root,
            ),
        },
        "session_dedup_hash_buckets_effective": metrics.get("session_dedup_hash_buckets_effective"),
        "bet_dedup_hash_buckets_effective": metrics.get("bet_dedup_hash_buckets_effective"),
    }


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
    sr = metrics.get("step4_split_report")
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
                materialize_adt_allowed_players_parquet(
                    profile_csv_path,
                    mapping_parquet_path,
                    quantile=q_thr,
                    duckdb_runtime=args.duckdb_runtime,
                    output_parquet=allowed_players_pq,
                )
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
    """When registry baseline includes ``fe__*``, materialize DuckDB features and join onto Step 3 output."""

    reg_p = Path(args.feature_candidate_registry).resolve() if args.feature_candidate_registry else None
    snap = load_candidate_registry(reg_p)
    baseline = baseline_features_for_main_trainer(snap)
    if not any(name.startswith("fe__") for name in baseline):
        return base_training_parquet
    cleaned = _hpre.default_cleaned_bet_parquet_path().resolve()
    fe_mod = importlib.import_module("trainer_hightier.feature_experiment.materialize_fe_derived")
    en_mod = importlib.import_module("trainer_hightier.feature_experiment.dataset_enrich")
    freg = importlib.import_module("trainer_hightier.feature_experiment.feature_registry")
    freg.set_candidate_registry_path(reg_p)
    fe_out = base_training_parquet.parent / "_main_trainer_fe_derived.parquet"
    enriched = base_training_parquet.parent / "training_set_fe_enriched.parquet"
    t_m0 = time.perf_counter()
    fe_mod.materialize_fe_derived_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=base_training_parquet,
        out_parquet=fe_out,
        duckdb_runtime=args.duckdb_runtime,
    )
    mat_sec = round(time.perf_counter() - t_m0, 3)
    t_e0 = time.perf_counter()
    en_mod.enrich_training_parquet(
        base_training_parquet=base_training_parquet,
        fe_derived_parquet=fe_out,
        out_parquet=enriched,
        duckdb_runtime=args.duckdb_runtime,
    )
    enr_sec = round(time.perf_counter() - t_e0, 3)
    if metrics is not None:
        metrics["main_trainer_fe_materialize_sec"] = mat_sec
        metrics["main_trainer_fe_enrich_sec"] = enr_sec
        metrics["main_trainer_training_parquet_for_step4"] = str(enriched.resolve())
    logger.info(
        "[Step 3.5] baseline includes fe__*: materialized %s, enriched -> %s (%.3fs + %.3fs)",
        fe_out.name,
        enriched.name,
        mat_sec,
        enr_sec,
    )
    return enriched


def _maybe_run_step4(args: HighTierTrainArgs, *, metrics: dict[str, Any] | None) -> None:
    """Step 4: project/cast columns and write train/val/test splits by ``gaming_day``."""

    if not args.run_step4:
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
    fp_eff = _ensure_fe_enriched_training_parquet_for_step4(args, fp, metrics=metrics)
    t0 = time.perf_counter()
    res = _b4.arrange_and_split_training_data(
        features_parquet=fp_eff,
        duckdb_runtime=args.duckdb_runtime,
        step4=args.step4_split,
    )
    elapsed = round(time.perf_counter() - t0, 3)
    if metrics is not None:
        metrics["step4_seconds"] = elapsed
        metrics["step4_split_report"] = str(res.split_report_json.resolve())
        metrics["step4_splits_dir"] = str(res.splits_dir.resolve())
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
        result = _b5.train_lgbm_from_splits(
            splits_dir=splits_dir,
            duckdb_runtime=args.duckdb_runtime,
            objective_min_precision=float(args.objective.min_precision),
            random_seed=int(args.random_seed),
            step5=args.step5,
            output_dir=step5_out_dir,
            feature_columns=feat_cols,
        )
    except ValueError as exc:
        msg = str(exc)
        if "Step 5 schema gate failed" in msg or "requires split parquet" in msg:
            raise ValueError(f"{msg} (feature_candidate_registry={snap.path})") from exc
        raise
    if metrics is not None:
        metrics.update(result.report)
        metrics["candidate_registry"] = reg_echo
    return result


def write_artifacts(args: HighTierTrainArgs, *, step5_result: _b5.Step5Result | None = None) -> None:
    """Log persisted Step 5 artifacts (model written during ``fit_model``)."""
    if step5_result is not None:
        logger.info("[Step 6] Step 5 model at %s", step5_result.model_path.resolve())


def _run_training_execute_steps(args: HighTierTrainArgs, metrics: dict[str, Any]) -> _b5.Step5Result | None:
    """Run preprocess → dataset → split → Step 5; mutate ``metrics``; return Step 5 result if any."""

    if args.start_from_features:
        _maybe_run_step4(args, metrics=metrics)
    else:
        t_prep = time.perf_counter()
        prepare_training_frame(args, metrics=metrics)
        metrics["prepare_training_frame_seconds"] = round(time.perf_counter() - t_prep, 3)
        t_step3 = time.perf_counter()
        _maybe_build_training_dataset(args, metrics=metrics)
        metrics["build_training_dataset_seconds"] = round(time.perf_counter() - t_step3, 3)
        _maybe_run_step4(args, metrics=metrics)

    step5_result = fit_model(args, metrics=metrics)
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
