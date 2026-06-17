"""Formal session PIT feature ablation on existing training splits."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import time
from pathlib import Path

import pyarrow.parquet as pq

from trainer_hightier.config import (
    SESSION_PIT_FEATURE_COLUMNS,
    DuckDbRuntimeConfig,
    Step5TrainConfig,
    configs_from_run_profile,
    get_run_profile,
)
from trainer_hightier.feature_experiment.candidate_registry_loader import (
    baseline_features_for_main_trainer,
    candidate_features_for_group,
    load_candidate_registry,
)
from trainer_hightier.feature_experiment.materialize_session_pit import (
    enrich_split_parquet_with_session_pit,
    materialize_session_pit_parquet,
    write_session_pit_sidecars,
)
from trainer_hightier.utils.bet_l0_preprocess import (
    default_cleaned_bet_parquet_path,
    resolved_cleaned_bet_read_parquet_sql,
)

logger = logging.getLogger(__name__)
_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")
_HIGHTIER = Path(__file__).resolve().parents[1]
_REPO = _HIGHTIER.parent
_DEFAULT_SPLITS = _HIGHTIER / "artifacts" / "training_data" / "splits"


def _feature_columns_present_in_splits(splits_dir: Path, columns: tuple[str, ...]) -> tuple[str, ...]:
    names = frozenset(pq.read_schema(splits_dir / "train.parquet").names)
    present = tuple(c for c in columns if c in names)
    missing = [c for c in columns if c not in names]
    if missing:
        logger.warning("[session_pit_ablation] columns absent from train split: %s", missing)
    return present


def _materialize_and_enrich_splits(
    *,
    source_splits_dir: Path,
    out_splits_dir: Path,
    cleaned_bet_read: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    split_names: tuple[str, ...],
) -> dict[str, str]:
    """Materialize session PIT per split and write enriched copies."""

    out_splits_dir.mkdir(parents=True, exist_ok=True)
    session_paths: dict[str, str] = {}
    for split in split_names:
        src = source_splits_dir / f"{split}.parquet"
        sess_out = out_splits_dir / f"{split}.session_pit.parquet"
        enriched_out = out_splits_dir / f"{split}.parquet"
        meta = materialize_session_pit_parquet(
            training_parquet_for_bet_ids=src,
            out_parquet=sess_out,
            duckdb_runtime=duckdb_runtime,
            cleaned_bet_read=cleaned_bet_read,
        )
        write_session_pit_sidecars(
            run_dir=out_splits_dir,
            materialization_meta=meta,
            out_parquet=sess_out,
        )
        enrich_split_parquet_with_session_pit(
            split_parquet=src,
            session_pit_parquet=sess_out,
            out_parquet=enriched_out,
            duckdb_runtime=duckdb_runtime,
        )
        session_paths[split] = str(sess_out.resolve())
        logger.info(
            "[session_pit_ablation] enriched %s (available=%d/%d)",
            split,
            meta["session_available_row_count"],
            meta["materialized_bet_row_count"],
        )
    return session_paths


def run_ablation(
    *,
    source_splits_dir: Path,
    out_dir: Path,
    train_split: str,
    run_profile: str,
    random_seed: int,
    min_precision: float,
) -> dict:
    """Run baseline vs baseline+group_session_pit ablation."""

    t0 = time.perf_counter()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    duckdb_runtime, _, _ = configs_from_run_profile(get_run_profile(run_profile))
    cleaned_bet_read = resolved_cleaned_bet_read_parquet_sql(default_cleaned_bet_parquet_path())
    split_names = ("train", "val", "test")
    train_source_name = "train"
    if train_split != "train":
        split_names = (train_split, "val", "test")
        train_source_name = train_split
    enriched_dir = out_dir / "splits_with_session_pit"
    session_paths = _materialize_and_enrich_splits(
        source_splits_dir=source_splits_dir,
        out_splits_dir=enriched_dir,
        cleaned_bet_read=cleaned_bet_read,
        duckdb_runtime=duckdb_runtime,
        split_names=split_names,
    )
    if train_source_name != "train":
        import shutil

        sampled = enriched_dir / f"{train_source_name}.parquet"
        train_target = enriched_dir / "train.parquet"
        shutil.copy2(sampled, train_target)

    registry = load_candidate_registry(None)
    baseline_cols = _feature_columns_present_in_splits(
        enriched_dir,
        baseline_features_for_main_trainer(registry),
    )
    session_cols = candidate_features_for_group(registry, "group_session_pit", slot="ablation")
    session_present = _feature_columns_present_in_splits(enriched_dir, session_cols)
    add_one_cols = tuple(dict.fromkeys([*baseline_cols, *session_present]))
    step5 = Step5TrainConfig(run_step5=True, skip_optuna=True)

    base_dir = out_dir / "baseline"
    add_dir = out_dir / "baseline_plus_session_pit"
    base_dir.mkdir(parents=True, exist_ok=True)
    add_dir.mkdir(parents=True, exist_ok=True)

    logger.info("[session_pit_ablation] training baseline (%d cols)", len(baseline_cols))
    base_report = _b5.train_lgbm_from_splits(
        splits_dir=enriched_dir,
        duckdb_runtime=duckdb_runtime,
        objective_min_precision=min_precision,
        random_seed=random_seed,
        step5=step5,
        output_dir=base_dir,
        feature_columns=baseline_cols,
    ).report

    logger.info("[session_pit_ablation] training baseline+session (%d cols)", len(add_one_cols))
    add_report = _b5.train_lgbm_from_splits(
        splits_dir=enriched_dir,
        duckdb_runtime=duckdb_runtime,
        objective_min_precision=min_precision,
        random_seed=random_seed,
        step5=step5,
        output_dir=add_dir,
        feature_columns=add_one_cols,
    ).report

    report = {
        "experiment_kind": "session_pit_ablation_v1",
        "pit_predicate": (
            "session_end_dtm <= prediction_visible_ts_cf "
            "AND session.__etl_insert_Dtm_synthetic <= prediction_visible_ts_cf"
        ),
        "source_splits_dir": str(source_splits_dir.resolve()),
        "enriched_splits_dir": str(enriched_dir.resolve()),
        "session_materialization": session_paths,
        "baseline_feature_columns": list(baseline_cols),
        "session_feature_columns": list(session_present),
        "add_one_feature_columns": list(add_one_cols),
        "baseline_report": base_report,
        "baseline_plus_session_report": add_report,
        "delta": {
            "val_ap": float(add_report.get("val_ap", 0.0)) - float(base_report.get("val_ap", 0.0)),
            "test_ap": float(add_report.get("test_ap", 0.0)) - float(base_report.get("test_ap", 0.0)),
            "val_precision": float(add_report.get("val_precision", 0.0))
            - float(base_report.get("val_precision", 0.0)),
            "test_precision": float(add_report.get("test_precision", 0.0))
            - float(base_report.get("test_precision", 0.0)),
            "val_recall": float(add_report.get("val_recall", 0.0))
            - float(base_report.get("val_recall", 0.0)),
            "test_recall": float(add_report.get("test_recall", 0.0))
            - float(base_report.get("test_recall", 0.0)),
        },
        "elapsed_sec": round(time.perf_counter() - t0, 3),
    }
    out_path = out_dir / "session_pit_ablation_report.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("[session_pit_ablation] wrote %s", out_path)
    return report


def main() -> None:
    """CLI entry for session PIT ablation."""

    parser = argparse.ArgumentParser(description="Session PIT feature ablation on existing splits.")
    parser.add_argument(
        "--splits-dir",
        type=Path,
        default=_DEFAULT_SPLITS,
        help="Existing train/val/test split directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_HIGHTIER / "artifacts" / "feature_experiment" / "session_pit_ablation_latest",
        help="Experiment output directory.",
    )
    parser.add_argument(
        "--train-split",
        choices=("train", "train_sampled"),
        default="train_sampled",
        help="Train parquet to enrich/materialize (default train_sampled for faster iteration).",
    )
    parser.add_argument("--run-profile", default="default")
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--min-precision", type=float, default=0.6)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = run_ablation(
        source_splits_dir=args.splits_dir.resolve(),
        out_dir=args.output_dir.resolve(),
        train_split=args.train_split,
        run_profile=args.run_profile,
        random_seed=args.random_seed,
        min_precision=args.min_precision,
    )
    print(json.dumps(report["delta"], indent=2))


if __name__ == "__main__":
    main()
