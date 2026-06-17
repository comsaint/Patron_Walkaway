"""Time-CV ablation runner: expanding-window folds + LOO baseline pruning."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    FeatureSelectionTimeCvConfig,
    HighTierObjectiveConfig,
    Step5TrainConfig,
    configs_from_run_profile,
    get_run_profile,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
from trainer_hightier.feature_experiment.candidate_registry_loader import (
    baseline_features_for_main_trainer,
    load_candidate_registry,
)
from trainer_hightier.feature_experiment.time_cv.fold_definitions import (
    GAMING_DAY_COLUMN,
    TimeFold,
    generate_expanding_folds,
    unique_gaming_days_from_parquet,
)
from trainer_hightier.feature_experiment.time_cv.metrics import delta_p1hr_pp, fold_metrics_from_report
from trainer_hightier.feature_experiment.time_cv.report import (
    aggregate_arm_decision,
    arm_decision_to_dict,
    feature_pruning_decision_from_loo,
    should_early_stop_strong_drop,
)

logger = logging.getLogger(__name__)
_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")
_HIGHTIER = Path(__file__).resolve().parents[2]
_DEFAULT_SPLITS = _HIGHTIER / "artifacts" / "training_data" / "splits"
_DEFAULT_ENRICHED = _HIGHTIER / "artifacts" / "training_data" / "training_set_fe_enriched.parquet"


def _feature_columns_present(parquet_path: Path, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return registry columns that exist in ``parquet_path``."""

    names = frozenset(pq.read_schema(parquet_path).names)
    present = tuple(c for c in columns if c in names)
    missing = [c for c in columns if c not in names]
    if missing:
        logger.warning("[time_cv] columns absent from %s: %s", parquet_path.name, missing)
    return present


def _cv_pool_parquet_paths(
    *,
    enriched_parquet: Path | None,
    splits_dir: Path,
    train_split: str = "train",
) -> tuple[Path, ...]:
    """Resolve train+val parquet paths for fold day discovery and row filtering."""

    if enriched_parquet is not None and enriched_parquet.is_file():
        return (enriched_parquet.resolve(),)
    train_name = "train.parquet" if train_split == "train" else "train_sampled.parquet"
    train_p = splits_dir / train_name
    val_p = splits_dir / "val.parquet"
    for p in (train_p, val_p):
        if not p.is_file():
            raise FileNotFoundError(f"time_cv requires train/val split or enriched parquet; missing {p}")
    return (train_p.resolve(), val_p.resolve())


def _write_fold_splits(
    *,
    pool_paths: tuple[Path, ...],
    test_parquet: Path,
    fold: TimeFold,
    out_dir: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> None:
    """Write ``train.parquet`` / ``val.parquet`` for one fold; copy held-out test."""

    out_dir.mkdir(parents=True, exist_ok=True)
    quoted = ", ".join(f"'{str(p).replace(chr(39), chr(39) * 2)}'" for p in pool_paths)
    train_out = str((out_dir / "train.parquet").resolve()).replace("'", "''")
    val_out = str((out_dir / "val.parquet").resolve()).replace("'", "''")
    train_start = fold.train_start.isoformat()
    train_end = fold.train_end.isoformat()
    val_start = fold.val_start.isoformat()
    val_end = fold.val_end.isoformat()

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(
            f"COPY (SELECT * FROM read_parquet([{quoted}]) "
            f"WHERE CAST({GAMING_DAY_COLUMN} AS DATE) BETWEEN DATE '{train_start}' AND DATE '{train_end}') "
            f"TO '{train_out}' (FORMAT PARQUET)",
        )
        con.execute(
            f"COPY (SELECT * FROM read_parquet([{quoted}]) "
            f"WHERE CAST({GAMING_DAY_COLUMN} AS DATE) BETWEEN DATE '{val_start}' AND DATE '{val_end}') "
            f"TO '{val_out}' (FORMAT PARQUET)",
        )
    finally:
        con.close()

    shutil.copy2(test_parquet.resolve(), out_dir / "test.parquet")


def _train_arm(
    *,
    splits_dir: Path,
    output_dir: Path,
    feature_columns: tuple[str, ...],
    duckdb_runtime: DuckDbRuntimeConfig,
    random_seed: int,
    min_precision: float,
) -> dict[str, Any]:
    """Train one LightGBM arm with fixed hyperparameters (no Optuna)."""

    step5 = Step5TrainConfig(run_step5=True, skip_optuna=True)
    objective = HighTierObjectiveConfig(
        selection_policy="alert_band_precision",
        deployment_target_alerts_per_hour=1.0,
        min_precision=float(min_precision),
    )
    result = _b5.train_lgbm_from_splits(
        splits_dir=splits_dir,
        duckdb_runtime=duckdb_runtime,
        objective_min_precision=min_precision,
        random_seed=random_seed,
        step5=step5,
        output_dir=output_dir,
        feature_columns=feature_columns,
        objective=objective,
    )
    return result.report


def _run_one_arm_across_folds(
    *,
    arm_id: str,
    arm_columns: tuple[str, ...],
    baseline_columns: tuple[str, ...],
    folds: tuple[TimeFold, ...],
    pool_paths: tuple[Path, ...],
    test_parquet: Path,
    out_dir: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    random_seed: int,
    min_precision: float,
    cfg: FeatureSelectionTimeCvConfig,
    wall_deadline: float,
    shared_baseline_reports: dict[int, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Run baseline + arm for each fold; return per-fold metrics and decision."""

    fold_rows: list[dict[str, Any]] = []
    fold_deltas: list[float] = []
    early_stopped = False

    for fold in folds:
        if time.perf_counter() >= wall_deadline:
            logger.warning("[time_cv] wall time limit hit for arm=%s fold=%d", arm_id, fold.fold_idx)
            break

        fold_dir = out_dir / f"fold_{fold.fold_idx:02d}"
        split_dir = fold_dir / "splits"
        _write_fold_splits(
            pool_paths=pool_paths,
            test_parquet=test_parquet,
            fold=fold,
            out_dir=split_dir,
            duckdb_runtime=duckdb_runtime,
        )

        base_report: dict[str, Any]
        if shared_baseline_reports is not None and fold.fold_idx in shared_baseline_reports:
            base_report = shared_baseline_reports[fold.fold_idx]
        else:
            base_dir = fold_dir / "baseline"
            logger.info(
                "[time_cv] fold=%d training baseline (%d cols)",
                fold.fold_idx,
                len(baseline_columns),
            )
            base_report = _train_arm(
                splits_dir=split_dir,
                output_dir=base_dir,
                feature_columns=baseline_columns,
                duckdb_runtime=duckdb_runtime,
                random_seed=random_seed,
                min_precision=min_precision,
            )
            if shared_baseline_reports is not None:
                shared_baseline_reports[fold.fold_idx] = base_report

        arm_dir = fold_dir / "arm"
        logger.info("[time_cv] fold=%d training arm=%s (%d cols)", fold.fold_idx, arm_id, len(arm_columns))
        arm_report = _train_arm(
            splits_dir=split_dir,
            output_dir=arm_dir,
            feature_columns=arm_columns,
            duckdb_runtime=duckdb_runtime,
            random_seed=random_seed,
            min_precision=min_precision,
        )

        delta = delta_p1hr_pp(base_report, arm_report)
        fold_rows.append(
            {
                "fold_idx": fold.fold_idx,
                "train_n_days": fold.train_n_days,
                "val_n_days": fold.val_n_days,
                "train_start": fold.train_start.isoformat(),
                "train_end": fold.train_end.isoformat(),
                "val_start": fold.val_start.isoformat(),
                "val_end": fold.val_end.isoformat(),
                "delta_p1hr_pp": delta,
                "baseline": fold_metrics_from_report(
                    base_report,
                    fold_idx=fold.fold_idx,
                    arm_id="baseline",
                ).__dict__,
                "arm": fold_metrics_from_report(
                    arm_report,
                    fold_idx=fold.fold_idx,
                    arm_id=arm_id,
                ).__dict__,
            },
        )
        if delta is not None:
            fold_deltas.append(float(delta))
            if should_early_stop_strong_drop(
                tuple(fold_deltas),
                early_stop_folds=int(cfg.early_stop_folds),
            ):
                logger.info("[time_cv] early stop STRONG_DROP arm=%s after %d folds", arm_id, len(fold_deltas))
                early_stopped = True
                break

    decision = aggregate_arm_decision(
        tuple(fold_deltas),
        arm_id=arm_id,
        cfg=cfg,
        early_stopped=early_stopped,
    )
    return {
        "arm_id": arm_id,
        "arm_columns": list(arm_columns),
        "arm_mode": "leave_one_out",
        "decision": arm_decision_to_dict(decision),
        "feature_pruning_decision": feature_pruning_decision_from_loo(decision),
        "folds": fold_rows,
    }


def run_time_cv_ablation(
    *,
    output_dir: Path,
    splits_dir: Path | None = None,
    enriched_parquet: Path | None = None,
    train_split: str = "train",
    loo_features: tuple[str, ...] | None = None,
    n_folds: int | None = None,
    run_profile: str = "default",
    random_seed: int = 42,
    min_precision: float = 0.6,
    time_cv_cfg: FeatureSelectionTimeCvConfig | None = None,
) -> dict[str, Any]:
    """Run expanding-window Time-CV LOO ablation on baseline features."""

    t0 = time.perf_counter()
    cfg = time_cv_cfg or FeatureSelectionTimeCvConfig()
    k_folds = int(n_folds if n_folds is not None else cfg.prototype_n_folds)
    out_dir = output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    splits = (splits_dir or _DEFAULT_SPLITS).resolve()
    test_parquet = splits / "test.parquet"
    if not test_parquet.is_file():
        raise FileNotFoundError(f"time_cv requires held-out test split at {test_parquet}")

    pool_paths = _cv_pool_parquet_paths(
        enriched_parquet=enriched_parquet,
        splits_dir=splits,
        train_split=train_split,
    )
    duckdb_runtime, _, _ = configs_from_run_profile(get_run_profile(run_profile))
    gaming_days = unique_gaming_days_from_parquet(pool_paths, duckdb_runtime=duckdb_runtime)
    folds = generate_expanding_folds(
        gaming_days,
        n_folds=k_folds,
        val_window_days=int(cfg.val_window_days),
        min_train_days=int(cfg.min_train_days),
    )

    schema_path = pool_paths[0]
    registry = load_candidate_registry(None)
    baseline_cols = _feature_columns_present(
        schema_path,
        baseline_features_for_main_trainer(registry),
    )
    if not baseline_cols:
        raise ValueError("time_cv: no baseline feature columns present in CV pool parquet.")

    if loo_features is None:
        loo_features = baseline_cols[: min(3, len(baseline_cols))]

    arms: list[dict[str, Any]] = []
    shared_baseline: dict[int, dict[str, Any]] = {}
    wall_deadline = t0 + float(cfg.wall_time_limit_sec)

    for feat in loo_features:
        if feat not in baseline_cols:
            logger.warning("[time_cv] skip LOO arm %s (not in baseline columns)", feat)
            continue
        arm_cols = tuple(c for c in baseline_cols if c != feat)
        if not arm_cols:
            logger.warning("[time_cv] skip LOO arm %s (would empty feature set)", feat)
            continue
        arm_result = _run_one_arm_across_folds(
            arm_id=f"loo__{feat}",
            arm_columns=arm_cols,
            baseline_columns=baseline_cols,
            folds=folds,
            pool_paths=pool_paths,
            test_parquet=test_parquet,
            out_dir=out_dir / f"arm_{feat}",
            duckdb_runtime=duckdb_runtime,
            random_seed=random_seed,
            min_precision=min_precision,
            cfg=cfg,
            wall_deadline=wall_deadline,
            shared_baseline_reports=shared_baseline,
        )
        arms.append(arm_result)

    report: dict[str, Any] = {
        "experiment_kind": "time_cv_ablation_v0",
        "arm_mode": "leave_one_out",
        "n_folds_requested": k_folds,
        "n_folds_used": len(folds),
        "fold_manifest": [
            {
                "fold_idx": f.fold_idx,
                "train_start": f.train_start.isoformat(),
                "train_end": f.train_end.isoformat(),
                "val_start": f.val_start.isoformat(),
                "val_end": f.val_end.isoformat(),
                "train_n_days": f.train_n_days,
                "val_n_days": f.val_n_days,
            }
            for f in folds
        ],
        "cv_pool_parquet_paths": [str(p) for p in pool_paths],
        "train_split": train_split,
        "test_parquet": str(test_parquet),
        "baseline_feature_columns": list(baseline_cols),
        "loo_features_requested": list(loo_features),
        "time_cv_config": {
            "n_folds": cfg.n_folds,
            "prototype_n_folds": cfg.prototype_n_folds,
            "val_window_days": cfg.val_window_days,
            "min_train_days": cfg.min_train_days,
            "early_stop_folds": cfg.early_stop_folds,
            "mean_delta_p1hr_pp": cfg.mean_delta_p1hr_pp,
            "max_cv_ratio": cfg.max_cv_ratio,
            "drop_threshold_pp": cfg.drop_threshold_pp,
            "marginal_low_pp": cfg.marginal_low_pp,
            "wall_time_limit_sec": cfg.wall_time_limit_sec,
        },
        "arms": arms,
        "elapsed_sec": round(time.perf_counter() - t0, 3),
    }
    out_path = out_dir / "time_cv_ablation_report.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("[time_cv] wrote %s", out_path)
    return report


def main() -> None:
    """CLI entry for Time-CV LOO prototype."""

    parser = argparse.ArgumentParser(description="Time-CV leave-one-out baseline feature ablation.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_HIGHTIER / "artifacts" / "feature_experiment" / "time_cv_latest",
    )
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS)
    parser.add_argument(
        "--enriched-parquet",
        type=Path,
        default=None,
        help="Optional single enriched parquet for CV pool (default: train+val splits).",
    )
    parser.add_argument(
        "--train-split",
        choices=("train", "train_sampled"),
        default="train",
        help="Train parquet under splits-dir for CV pool (default: full train).",
    )
    parser.add_argument(
        "--loo-features",
        nargs="*",
        default=None,
        help="Baseline columns to leave out (default: first 3 baseline columns).",
    )
    parser.add_argument("--n-folds", type=int, default=None, help="Override fold count (prototype default K=3).")
    parser.add_argument("--min-train-days", type=int, default=None, help="Override min train gaming days per fold.")
    parser.add_argument("--val-window-days", type=int, default=None, help="Override validation window (gaming days).")
    parser.add_argument("--wall-time-limit-sec", type=float, default=None, help="Wall clock budget for all arms.")
    parser.add_argument("--run-profile", default="default")
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--min-precision", type=float, default=0.6)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    loo = tuple(args.loo_features) if args.loo_features else None
    enriched = args.enriched_parquet.resolve() if args.enriched_parquet is not None else None
    base_cfg = FeatureSelectionTimeCvConfig()
    cfg = FeatureSelectionTimeCvConfig(
        n_folds=base_cfg.n_folds,
        val_window_days=int(args.val_window_days if args.val_window_days is not None else base_cfg.val_window_days),
        min_train_days=int(args.min_train_days if args.min_train_days is not None else base_cfg.min_train_days),
        early_stop_folds=base_cfg.early_stop_folds,
        mean_delta_p1hr_pp=base_cfg.mean_delta_p1hr_pp,
        max_cv_ratio=base_cfg.max_cv_ratio,
        drop_threshold_pp=base_cfg.drop_threshold_pp,
        marginal_low_pp=base_cfg.marginal_low_pp,
        wall_time_limit_sec=float(
            args.wall_time_limit_sec if args.wall_time_limit_sec is not None else base_cfg.wall_time_limit_sec
        ),
        prototype_n_folds=base_cfg.prototype_n_folds,
        deployment_target_alerts_per_hour=base_cfg.deployment_target_alerts_per_hour,
    )
    report = run_time_cv_ablation(
        output_dir=args.output_dir.resolve(),
        splits_dir=args.splits_dir.resolve(),
        enriched_parquet=enriched,
        train_split=args.train_split,
        loo_features=loo,
        n_folds=args.n_folds,
        run_profile=args.run_profile,
        random_seed=args.random_seed,
        min_precision=args.min_precision,
        time_cv_cfg=cfg,
    )
    print(json.dumps({"arms": [a["decision"] for a in report["arms"]]}, indent=2))


if __name__ == "__main__":
    main()
