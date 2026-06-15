"""Clock-context add-one smoke: baseline vs +is_late_night / +is_weekend / +hour_of_day.

Uses existing Step-4 splits (current registry baseline) and derives clock columns from
``payout_complete_dtm`` (HK wall-clock), matching ``materialize_fe_derived`` semantics.
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from pathlib import Path
from typing import Any, Final

import duckdb

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    Step5TrainConfig,
    configs_from_run_profile,
    get_run_profile,
)
from trainer_hightier.feature_experiment.ablation import compute_gate1_vs_baseline
from trainer_hightier.feature_experiment.feature_registry import MODEL_FEATURE_COLUMNS

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SPLITS = _REPO_ROOT / "trainer_hightier/artifacts/training_data/splits"
_DEFAULT_OUT = _REPO_ROOT / "trainer_hightier/artifacts/feature_experiment/clock_context_smoke"
_CAPACITY_ALERTS_HR: Final[float] = 120.0

_CLOCK_COLS: Final[tuple[str, ...]] = (
    "fe__clock__hour_of_day",
    "fe__clock__is_weekend",
    "fe__clock__is_late_night",
)

_CLOCK_ABLATION_ARMS: Final[dict[str, tuple[str, ...]]] = {
    "add_fe__clock__is_late_night": ("fe__clock__is_late_night",),
    "add_fe__clock__is_weekend": ("fe__clock__is_weekend",),
    "add_fe__clock__hour_of_day": ("fe__clock__hour_of_day",),
}

_SMOKE_NEG_SAMPLE_FRAC: Final[float] = 0.05
_SMOKE_NEG_SAMPLE_SEED: Final[int] = 42


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def materialize_clock_splits(*, splits_dir: Path, out_splits_dir: Path) -> None:
    """Write train/val/test parquets with HK clock columns derived from payout_complete_dtm."""

    src = Path(splits_dir).resolve()
    dst = Path(out_splits_dir).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    for split in ("train", "val", "test"):
        in_p = src / f"{split}.parquet"
        out_p = dst / f"{split}.parquet"
        if not in_p.is_file():
            raise FileNotFoundError(f"missing split parquet: {in_p}")
        if out_p.is_file() and out_p.stat().st_size > 0:
            continue
        sql = f"""
        COPY (
          SELECT
            b.*,
            CAST(EXTRACT(hour FROM b.payout_complete_dtm AT TIME ZONE 'Asia/Hong_Kong') AS DOUBLE)
              AS fe__clock__hour_of_day,
            CASE
              WHEN EXTRACT(isodow FROM b.payout_complete_dtm AT TIME ZONE 'Asia/Hong_Kong') IN (6, 7)
              THEN 1.0 ELSE 0.0
            END AS fe__clock__is_weekend,
            CASE
              WHEN EXTRACT(hour FROM b.payout_complete_dtm AT TIME ZONE 'Asia/Hong_Kong') BETWEEN 0 AND 5
              THEN 1.0 ELSE 0.0
            END AS fe__clock__is_late_night
          FROM read_parquet('{_path_esc(in_p)}') b
        ) TO '{_path_esc(out_p)}' (FORMAT PARQUET, COMPRESSION SNAPPY)
        """
        con = duckdb.connect(database=":memory:")
        try:
            con.execute(sql)
        finally:
            con.close()
    sampled_src = src / "train_sampled.parquet"
    if sampled_src.is_file():
        out_s = dst / "train_sampled.parquet"
        if not (out_s.is_file() and out_s.stat().st_size > 0):
            sql_s = f"""
            COPY (
              SELECT
                b.*,
                CAST(EXTRACT(hour FROM b.payout_complete_dtm AT TIME ZONE 'Asia/Hong_Kong') AS DOUBLE)
                  AS fe__clock__hour_of_day,
                CASE
                  WHEN EXTRACT(isodow FROM b.payout_complete_dtm AT TIME ZONE 'Asia/Hong_Kong') IN (6, 7)
                  THEN 1.0 ELSE 0.0
                END AS fe__clock__is_weekend,
                CASE
                  WHEN EXTRACT(hour FROM b.payout_complete_dtm AT TIME ZONE 'Asia/Hong_Kong') BETWEEN 0 AND 5
                  THEN 1.0 ELSE 0.0
                END AS fe__clock__is_late_night
              FROM read_parquet('{_path_esc(sampled_src)}') b
            ) TO '{_path_esc(out_s)}' (FORMAT PARQUET, COMPRESSION SNAPPY)
            """
            con = duckdb.connect(database=":memory:")
            try:
                con.execute(sql_s)
            finally:
                con.close()


def materialize_smoke_train_sample(*, clock_splits_dir: Path) -> Path:
    """Downsample train negatives for smoke runs to avoid RAM blowups on full splits."""

    train_p = Path(clock_splits_dir).resolve() / "train.parquet"
    out_p = Path(clock_splits_dir).resolve() / "train_sampled.parquet"
    if not train_p.is_file():
        raise FileNotFoundError(f"missing train split for smoke sampling: {train_p}")
    frac = float(_SMOKE_NEG_SAMPLE_FRAC)
    seed = int(_SMOKE_NEG_SAMPLE_SEED)
    sql = f"""
    COPY (
      SELECT *
      FROM read_parquet('{_path_esc(train_p)}')
      WHERE COALESCE(CAST(walkaway_label AS DOUBLE), 0.0) >= 0.5
      UNION ALL
      SELECT *
      FROM read_parquet('{_path_esc(train_p)}')
      WHERE COALESCE(CAST(walkaway_label AS DOUBLE), 0.0) < 0.5
        AND mod(abs(hash(CAST(bet_id AS VARCHAR) || '|{seed}')), 10000)
            < CAST(round({frac} * 10000) AS BIGINT)
    ) TO '{_path_esc(out_p)}' (FORMAT PARQUET, COMPRESSION SNAPPY)
    """
    con = duckdb.connect(database=":memory:")
    try:
        con.execute(sql)
    finally:
        con.close()
    return out_p


def _train_arm(
    *,
    splits_dir: Path,
    feature_columns: tuple[str, ...],
    output_dir: Path,
    duck: DuckDbRuntimeConfig,
    min_prec: float,
    train_parquet: Path | None,
) -> dict[str, Any]:
    """Train one arm with Step 5 defaults (no Optuna)."""

    step5 = Step5TrainConfig(run_step5=True, skip_optuna=True)
    res = _b5.train_lgbm_from_splits(
        splits_dir=splits_dir,
        duckdb_runtime=duck,
        objective_min_precision=min_prec,
        random_seed=42,
        step5=step5,
        output_dir=output_dir,
        feature_columns=feature_columns,
        train_parquet=train_parquet,
    )
    return dict(res.report)


def _metric_slice(report: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "val_ap",
        "val_recall",
        "val_alerts_per_hour",
        "test_ap",
        "test_recall",
        "test_alerts_per_hour",
        "test_operational_simulated_precision",
        "test_operational_simulated_alerts_per_hour",
        "step5_val_pick_feasible",
        "step5_threshold",
    )
    return {k: report.get(k) for k in keys}


def run_smoke(
    *,
    splits_dir: Path,
    out_dir: Path,
    arm_ids: tuple[str, ...] | None = None,
    skip_materialize: bool = False,
) -> dict[str, Any]:
    """Run baseline vs three clock add-one arms; write JSON report."""

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    clock_splits = out_dir / "splits_clock"
    if not skip_materialize:
        materialize_clock_splits(splits_dir=splits_dir, out_splits_dir=clock_splits)

    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    min_prec = HighTierObjectiveConfig().min_precision
    baseline_cols = tuple(MODEL_FEATURE_COLUMNS)
    train_sample: Path | None = None
    if not skip_materialize:
        train_sample = materialize_smoke_train_sample(clock_splits_dir=clock_splits)
    else:
        sampled_train = clock_splits / "train_sampled.parquet"
        if sampled_train.is_file():
            import pyarrow.parquet as pq

            sampled_names = frozenset(pq.read_schema(sampled_train).names)
            if all(c in sampled_names for c in baseline_cols):
                train_sample = sampled_train
        if train_sample is None:
            train_sample = materialize_smoke_train_sample(clock_splits_dir=clock_splits)

    selected_arms = _CLOCK_ABLATION_ARMS
    if arm_ids is not None:
        missing = [a for a in arm_ids if a not in _CLOCK_ABLATION_ARMS]
        if missing:
            raise ValueError(f"unknown arm_ids={missing}; expected subset of {list(_CLOCK_ABLATION_ARMS)}")
        selected_arms = {k: v for k, v in _CLOCK_ABLATION_ARMS.items() if k in arm_ids}

    t0 = time.perf_counter()
    baseline_metrics_path = out_dir / "baseline" / "training_metrics.json"
    if baseline_metrics_path.is_file():
        baseline_report = json.loads(baseline_metrics_path.read_text(encoding="utf-8"))
    else:
        baseline_report = _train_arm(
            splits_dir=clock_splits,
            feature_columns=baseline_cols,
            output_dir=out_dir / "baseline",
            duck=duck,
            min_prec=min_prec,
            train_parquet=train_sample,
        )

    arms_block: dict[str, Any] = {}
    for arm_id, clock_cols in selected_arms.items():
        arm_cols = tuple(dict.fromkeys(baseline_cols + clock_cols))
        arm_report = _train_arm(
            splits_dir=clock_splits,
            feature_columns=arm_cols,
            output_dir=out_dir / arm_id,
            duck=duck,
            min_prec=min_prec,
            train_parquet=train_sample,
        )
        gate1 = compute_gate1_vs_baseline(
            baseline_report,
            arm_report,
            capacity_alerts_per_hour_cap=_CAPACITY_ALERTS_HR,
            arm_side_key_prefix="arm",
        )
        arms_block[arm_id] = {
            "experiment_kind": "add_one_clock_feature",
            "clock_feature_columns": list(clock_cols),
            "feature_columns": list(arm_cols),
            "metrics": _metric_slice(arm_report),
            "gate1_vs_baseline": gate1,
            "pass_gate1": bool(gate1.get("pass_v0_thresholds")),
        }

    report_path = out_dir / "clock_feature_ablation_report.json"
    prior_arms: dict[str, Any] = {}
    if report_path.is_file():
        prior = json.loads(report_path.read_text(encoding="utf-8"))
        prior_arms = dict(prior.get("arms") or {})
    prior_arms.update(arms_block)

    summary: dict[str, Any] = {
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "splits_source": str(Path(splits_dir).resolve()),
        "clock_splits": str(clock_splits.resolve()),
        "baseline_feature_count": len(baseline_cols),
        "clock_columns_materialized": list(_CLOCK_COLS),
        "baseline": _metric_slice(baseline_report),
        "arms": prior_arms,
        "note": (
            "Add-one vs current registry baseline (42 cols). Gate1: delta_val_ap>=0.003, "
            "delta_val_recall>0, val pick feasible, val_alerts_per_hour<=120."
        ),
    }
    report_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> None:
    """CLI entry for clock-context add-one smoke."""

    parser = argparse.ArgumentParser(
        description="Clock feature add-one smoke (baseline vs +is_late_night / +is_weekend / +hour_of_day)",
    )
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT)
    parser.add_argument(
        "--arm",
        action="append",
        dest="arms",
        choices=tuple(_CLOCK_ABLATION_ARMS.keys()),
        help="Run only selected add-one arm(s); baseline metrics are reused if present.",
    )
    parser.add_argument(
        "--skip-materialize",
        action="store_true",
        help="Reuse existing splits_clock outputs when present.",
    )
    args = parser.parse_args()
    rep = run_smoke(
        splits_dir=args.splits_dir,
        out_dir=args.out_dir,
        arm_ids=tuple(args.arms) if args.arms else None,
        skip_materialize=bool(args.skip_materialize),
    )
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
