"""P1+P2 add-one ablation: test only new chase-loss and stake-escalation features.

P1 (group_m_outcome_state additions):
- fe__outcome__consecutive_loss_streak
- fe__outcome__loss_then_double_ratio__w1h
- fe__outcome__wager_after_loss_step_ratio__w1h

P2 (group_n_stake_dynamics additions):
- fe__stake__wager_trend_slope__w1h
- fe__stake__wager_last3_vs_prior3_ratio__w1h

Clock features (promoted to baseline, not yet in splits):
- fe__clock__hour_of_day, fe__clock__day_of_week, fe__clock__is_weekend, fe__clock__is_late_night

Reads existing splits, materializes clock + P1 + P2 features in one SQL pass,
then trains add-one ablation arms against the baseline (46 features).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
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

logger = logging.getLogger(__name__)
_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SPLITS = _REPO_ROOT / "trainer_hightier/artifacts/training_data/splits"
_DEFAULT_OUT = _REPO_ROOT / "trainer_hightier/artifacts/feature_experiment/p1p2_ablation"
_CAPACITY_ALERTS_HR: Final[float] = 120.0

_P1_FEATURES: Final[tuple[str, ...]] = (
    "fe__outcome__consecutive_loss_streak",
    "fe__outcome__loss_then_double_ratio__w1h",
    "fe__outcome__wager_after_loss_step_ratio__w1h",
)

_P2_FEATURES: Final[tuple[str, ...]] = (
    "fe__stake__wager_trend_slope__w1h",
    "fe__stake__wager_last3_vs_prior3_ratio__w1h",
)

_ALL_P1P2_FEATURES: Final[tuple[str, ...]] = _P1_FEATURES + _P2_FEATURES

_P1P2_ABLATION_ARMS: Final[dict[str, tuple[str, ...]]] = {
    "add_p1_consecutive_loss_streak": ("fe__outcome__consecutive_loss_streak",),
    "add_p1_loss_then_double_ratio": ("fe__outcome__loss_then_double_ratio__w1h",),
    "add_p1_wager_after_loss_step": ("fe__outcome__wager_after_loss_step_ratio__w1h",),
    "add_p1_all": _P1_FEATURES,
    "add_p2_wager_trend_slope": ("fe__stake__wager_trend_slope__w1h",),
    "add_p2_wager_last3_vs_prior3": ("fe__stake__wager_last3_vs_prior3_ratio__w1h",),
    "add_p2_clean": ("fe__stake__wager_last3_vs_prior3_ratio__w1h",),
    "add_p2_all": _P2_FEATURES,
    "add_p1p2_all": _ALL_P1P2_FEATURES,
}

_SMOKE_NEG_SAMPLE_FRAC: Final[float] = 0.05
_SMOKE_NEG_SAMPLE_SEED: Final[int] = 42


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def _build_materialize_sql(in_p: Path) -> str:
    """SQL to compute clock + P1 + P2 features, outputting only new cols keyed by bet_id."""
    iq = _path_esc(in_p)
    return f"""
    WITH src AS (
      SELECT
        b.bet_id,
        b.canonical_id,
        b.wager,
        b.casino_win,
        CAST(b.payout_complete_dtm AS TIMESTAMP) AS pcd_ts
      FROM read_parquet('{iq}') AS b
    ),
    lagged AS (
      SELECT
        s.*,
        LAG(s.wager, 1) OVER w_canon AS lag1_wager,
        LAG(s.casino_win, 1) OVER w_canon AS lag1_casino_win
      FROM src AS s
      WINDOW w_canon AS (PARTITION BY s.canonical_id ORDER BY s.pcd_ts, s.bet_id)
    ),
    with_flags AS (
      SELECT
        lg.bet_id,
        lg.canonical_id,
        lg.wager,
        lg.casino_win,
        lg.pcd_ts,
        CASE WHEN lg.lag1_casino_win > 0 THEN 1 ELSE 0 END AS is_loss,
        CASE WHEN lg.lag1_casino_win > 0 AND lg.wager > 2.0 * lg.lag1_wager THEN 1 ELSE 0 END AS is_double_after_loss,
        CASE WHEN lg.lag1_casino_win > 0 AND lg.lag1_wager > 1e-9 THEN lg.wager / lg.lag1_wager ELSE NULL END AS wager_step_after_loss,
        lg.lag1_wager
      FROM lagged AS lg
    ),
    with_streak AS (
      SELECT
        wf.*,
        SUM(wf.is_loss) OVER (
          PARTITION BY wf.canonical_id ORDER BY wf.pcd_ts, wf.bet_id
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS _loss_streak_grp
      FROM with_flags AS wf
    ),
    ordered AS (
      SELECT
        ws.bet_id,
        ws.pcd_ts,
        CASE WHEN ws.is_loss = 1
          THEN CAST(ROW_NUMBER() OVER (
            PARTITION BY ws.canonical_id, ws._loss_streak_grp ORDER BY ws.pcd_ts, ws.bet_id
          ) AS DOUBLE)
          ELSE 0.0
        END AS fe__outcome__consecutive_loss_streak,
        CASE
          WHEN COALESCE(SUM(ws.is_loss) OVER w1h, 0) > 0
          THEN CAST(SUM(ws.is_double_after_loss) OVER w1h
                   / COALESCE(SUM(ws.is_loss) OVER w1h, 0) AS DOUBLE)
          ELSE CAST(NULL AS DOUBLE)
        END AS fe__outcome__loss_then_double_ratio__w1h,
        CAST(AVG(ws.wager_step_after_loss) OVER w1h AS DOUBLE)
          AS fe__outcome__wager_after_loss_step_ratio__w1h,
        REGR_SLOPE(ws.wager, EXTRACT(epoch FROM ws.pcd_ts)) OVER w1h_peer
          AS wager_regr_slope_w1h,
        AVG(ws.wager) OVER w1h_peer AS wager_avg_w1h_peer,
        AVG(ws.wager) OVER w_last3 AS wager_avg_last3,
        AVG(ws.wager) OVER w_prior3 AS wager_avg_prior3
      FROM with_streak AS ws
      WINDOW
        w1h AS (
          PARTITION BY ws.canonical_id ORDER BY ws.pcd_ts
          RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND CURRENT ROW
        ),
        w1h_peer AS (
          PARTITION BY ws.canonical_id ORDER BY ws.pcd_ts
          RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND CURRENT ROW
        ),
        w_last3 AS (
          PARTITION BY ws.canonical_id ORDER BY ws.pcd_ts, ws.bet_id
          ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        ),
        w_prior3 AS (
          PARTITION BY ws.canonical_id ORDER BY ws.pcd_ts, ws.bet_id
          ROWS BETWEEN 5 PRECEDING AND 3 PRECEDING
        )
    )
    SELECT
      o.bet_id,
      o.fe__outcome__consecutive_loss_streak,
      o.fe__outcome__loss_then_double_ratio__w1h,
      o.fe__outcome__wager_after_loss_step_ratio__w1h,
      CASE
        WHEN o.wager_regr_slope_w1h IS NOT NULL
           AND o.wager_avg_w1h_peer IS NOT NULL AND o.wager_avg_w1h_peer > 1e-9
        THEN CAST(o.wager_regr_slope_w1h / o.wager_avg_w1h_peer AS DOUBLE)
        ELSE CAST(NULL AS DOUBLE)
      END AS fe__stake__wager_trend_slope__w1h,
      CASE
        WHEN o.wager_avg_prior3 IS NOT NULL AND o.wager_avg_prior3 > 1e-9
           AND o.wager_avg_last3 IS NOT NULL
        THEN CAST(o.wager_avg_last3 / o.wager_avg_prior3 AS DOUBLE)
        ELSE CAST(NULL AS DOUBLE)
      END AS fe__stake__wager_last3_vs_prior3_ratio__w1h,
      CAST(EXTRACT(hour FROM o.pcd_ts AT TIME ZONE 'Asia/Hong_Kong') AS DOUBLE)
        AS fe__clock__hour_of_day,
      CAST(EXTRACT(isodow FROM o.pcd_ts AT TIME ZONE 'Asia/Hong_Kong') AS DOUBLE)
        AS fe__clock__day_of_week,
      CASE
        WHEN EXTRACT(isodow FROM o.pcd_ts AT TIME ZONE 'Asia/Hong_Kong') >= 6 THEN 1.0
        ELSE 0.0
      END AS fe__clock__is_weekend,
      CASE
        WHEN EXTRACT(hour FROM o.pcd_ts AT TIME ZONE 'Asia/Hong_Kong') BETWEEN 0 AND 5 THEN 1.0
        ELSE 0.0
      END AS fe__clock__is_late_night
    FROM ordered AS o
    """


def materialize_p1p2_splits(*, splits_dir: Path, out_splits_dir: Path) -> None:
    """Write train/val/test parquets with clock + P1 + P2 features materialized.

    Two-pass approach:
    1. Compute new features (bet_id + 9 cols) into a sidecar parquet.
    2. LEFT JOIN sidecar onto original splits to produce final splits.
    """
    src = Path(splits_dir).resolve()
    dst = Path(out_splits_dir).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    sidecar_dir = dst / "_sidecar"
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    for split in ("train", "val", "test"):
        in_p = src / f"{split}.parquet"
        out_p = dst / f"{split}.parquet"
        sidecar_p = sidecar_dir / f"{split}_new_fe.parquet"
        if not in_p.is_file():
            raise FileNotFoundError(f"missing split parquet: {in_p}")
        if out_p.is_file() and out_p.stat().st_size > 0:
            logger.info("[p1p2] reusing existing %s", out_p)
            continue

        # Step 1: compute new features (bet_id + 9 cols)
        if sidecar_p.is_file() and sidecar_p.stat().st_size > 0:
            logger.info("[p1p2] reusing sidecar %s", sidecar_p)
        else:
            logger.info("[p1p2] computing new features for %s (%.1f GB)",
                        split, in_p.stat().st_size / 1e9)
            sql_new = _build_materialize_sql(in_p)
            con = duckdb.connect(database=":memory:")
            try:
                df = con.execute(sql_new).fetchdf()
                df.to_parquet(str(sidecar_p), engine="pyarrow", compression="snappy")
            except Exception as e:
                con.close()
                raise RuntimeError(
                    f"[p1p2] new-feature materialize failed for {split}: {type(e).__name__}: {e}"
                ) from e
            con.close()
            if not sidecar_p.is_file():
                raise RuntimeError(f"[p1p2] sidecar missing after materialize: {sidecar_p}")
            logger.info("[p1p2] sidecar %s (%.1f MB)", sidecar_p, sidecar_p.stat().st_size / 1e6)

        # Step 2: join sidecar onto original splits
        logger.info("[p1p2] joining sidecar onto %s", split)
        iq = _path_esc(in_p)
        sq = _path_esc(sidecar_p)
        oq = _path_esc(out_p)
        join_sql = f"""
        SELECT b.*,
               s.fe__outcome__consecutive_loss_streak,
               s.fe__outcome__loss_then_double_ratio__w1h,
               s.fe__outcome__wager_after_loss_step_ratio__w1h,
               s.fe__stake__wager_trend_slope__w1h,
               s.fe__stake__wager_last3_vs_prior3_ratio__w1h,
               s.fe__clock__hour_of_day,
               s.fe__clock__day_of_week,
               s.fe__clock__is_weekend,
               s.fe__clock__is_late_night
        FROM read_parquet('{iq}') AS b
        LEFT JOIN read_parquet('{sq}') AS s
          ON b.bet_id = s.bet_id
        """
        con = duckdb.connect(database=":memory:")
        try:
            df = con.execute(join_sql).fetchdf()
            df.to_parquet(str(out_p), engine="pyarrow", compression="snappy")
        except Exception as e:
            con.close()
            raise RuntimeError(
                f"[p1p2] join failed for {split}: {type(e).__name__}: {e}"
            ) from e
        con.close()
        if not out_p.is_file():
            raise RuntimeError(f"[p1p2] join SQL ran but output file missing: {out_p}")
        logger.info("[p1p2] wrote %s (%.1f GB)", out_p, out_p.stat().st_size / 1e9)


def materialize_smoke_train_sample(*, p1p2_splits_dir: Path) -> Path:
    """Downsample train negatives for faster smoke runs."""
    train_p = Path(p1p2_splits_dir).resolve() / "train.parquet"
    out_p = Path(p1p2_splits_dir).resolve() / "train_sampled.parquet"
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


def run_ablation(
    *,
    splits_dir: Path,
    out_dir: Path,
    arm_ids: tuple[str, ...] | None = None,
    skip_materialize: bool = False,
    use_smoke_sample: bool = False,
) -> dict[str, Any]:
    """Run baseline vs P1+P2 add-one arms; write JSON report."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    p1p2_splits = out_dir / "splits_p1p2"

    if not skip_materialize:
        materialize_p1p2_splits(splits_dir=splits_dir, out_splits_dir=p1p2_splits)

    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    min_prec = HighTierObjectiveConfig().min_precision
    baseline_cols = tuple(MODEL_FEATURE_COLUMNS)

    train_sample: Path | None = None
    if use_smoke_sample:
        if not skip_materialize:
            train_sample = materialize_smoke_train_sample(p1p2_splits_dir=p1p2_splits)
        else:
            sampled_train = p1p2_splits / "train_sampled.parquet"
            if sampled_train.is_file():
                train_sample = sampled_train

    selected_arms = _P1P2_ABLATION_ARMS
    if arm_ids is not None:
        missing = [a for a in arm_ids if a not in _P1P2_ABLATION_ARMS]
        if missing:
            raise ValueError(f"unknown arm_ids={missing}; expected subset of {list(_P1P2_ABLATION_ARMS.keys())}")
        selected_arms = {k: v for k, v in _P1P2_ABLATION_ARMS.items() if k in arm_ids}

    t0 = time.perf_counter()
    baseline_metrics_path = out_dir / "baseline" / "training_metrics.json"
    if baseline_metrics_path.is_file():
        logger.info("[p1p2] reusing existing baseline metrics")
        baseline_report = json.loads(baseline_metrics_path.read_text(encoding="utf-8"))
    else:
        logger.info("[p1p2] training baseline (%d cols)", len(baseline_cols))
        baseline_report = _train_arm(
            splits_dir=p1p2_splits,
            feature_columns=baseline_cols,
            output_dir=out_dir / "baseline",
            duck=duck,
            min_prec=min_prec,
            train_parquet=train_sample,
        )

    arms_block: dict[str, Any] = {}
    for arm_id, p1p2_cols in selected_arms.items():
        arm_cols = tuple(dict.fromkeys(baseline_cols + p1p2_cols))
        logger.info("[p1p2] training arm %s (%d additional cols)", arm_id, len(p1p2_cols))
        arm_report = _train_arm(
            splits_dir=p1p2_splits,
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
            "experiment_kind": "add_one_p1p2_feature",
            "p1p2_feature_columns": list(p1p2_cols),
            "feature_columns": list(arm_cols),
            "metrics": _metric_slice(arm_report),
            "gate1_vs_baseline": gate1,
            "pass_gate1": bool(gate1.get("pass_v0_thresholds")),
        }

    report_path = out_dir / "p1p2_ablation_report.json"
    prior_arms: dict[str, Any] = {}
    if report_path.is_file():
        prior = json.loads(report_path.read_text(encoding="utf-8"))
        prior_arms = dict(prior.get("arms") or {})
    prior_arms.update(arms_block)

    summary: dict[str, Any] = {
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "splits_source": str(Path(splits_dir).resolve()),
        "p1p2_splits": str(p1p2_splits.resolve()),
        "baseline_feature_count": len(baseline_cols),
        "p1p2_features_materialized": list(_ALL_P1P2_FEATURES),
        "baseline": _metric_slice(baseline_report),
        "arms": prior_arms,
        "note": (
            "Add-one ablation for P1 (chase-loss) and P2 (stake-escalation) features. "
            "Baseline = 46 features (42 original + 4 clock promoted). "
            "Gate1: delta_val_ap>=0.003, delta_val_recall>0, val pick feasible, "
            "val_alerts_per_hour<=120."
        ),
    }
    report_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    logger.info("[p1p2] wrote report to %s", report_path)
    return summary


def main() -> None:
    """CLI entry for P1+P2 add-one ablation."""
    parser = argparse.ArgumentParser(
        description="P1+P2 add-one ablation (chase-loss and stake-escalation features)",
    )
    parser.add_argument("--splits-dir", type=Path, default=_DEFAULT_SPLITS)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT)
    parser.add_argument(
        "--arm",
        action="append",
        dest="arms",
        choices=tuple(_P1P2_ABLATION_ARMS.keys()),
        help="Run only selected add-one arm(s); baseline metrics are reused if present.",
    )
    parser.add_argument(
        "--skip-materialize",
        action="store_true",
        help="Reuse existing splits_p1p2 outputs when present.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Use 5%% negative sample for faster iteration.",
    )
    args = parser.parse_args()
    rep = run_ablation(
        splits_dir=args.splits_dir,
        out_dir=args.out_dir,
        arm_ids=tuple(args.arms) if args.arms else None,
        skip_materialize=bool(args.skip_materialize),
        use_smoke_sample=bool(args.smoke),
    )
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
