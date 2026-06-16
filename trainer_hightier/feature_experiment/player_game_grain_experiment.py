"""Wave 2 offline experiment: bet-level top3_mean baseline vs native player-game model."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from trainer_hightier.config import Step5TrainConfig, configs_from_run_profile, get_run_profile, txn_lite_feature_columns
from trainer_hightier.feature_experiment.candidate_registry_loader import (
    baseline_features_for_main_trainer,
    load_candidate_registry,
)
from trainer_hightier.feature_experiment.equal_capacity_eval import pg_top_k_metrics
from trainer_hightier.feature_experiment.materialize_txn_lite import enrich_player_game_splits_with_txn_pg
from trainer_hightier.player_game_grain import (
    enrich_player_game_splits_with_baseline_bet_features,
    materialize_player_game_splits,
    player_game_composition_features,
    prepare_bet_splits_for_player_game_materialize,
    train_player_game_lgbm_from_splits,
)

logger = logging.getLogger(__name__)

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")


def _feature_columns_present_in_splits(splits_dir: Path, columns: tuple[str, ...]) -> tuple[str, ...]:
    """Drop registry columns absent from split parquet schema."""

    names = frozenset(pq.read_schema(Path(splits_dir) / "train.parquet").names)
    present = tuple(c for c in columns if c in names)
    missing = [c for c in columns if c not in names]
    if missing:
        logger.warning("[PG-W2] Baseline columns absent from splits (dropped): %s", missing)
    return present


def _audit_dict(audits: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Serialize materialize audit dataclasses to JSON-friendly dicts."""

    return {
        split: {
            "input_bet_rows": int(a.input_bet_rows),
            "output_player_games": int(a.output_player_games),
            "excluded_bet_rows": int(a.excluded_bet_rows),
            "excluded_player_games": int(a.excluded_player_games),
            "dq_pcd_span_violations": int(a.dq_pcd_span_violations),
            "dq_pv_span_violations": int(a.dq_pv_span_violations),
        }
        for split, a in audits.items()
    }


def _decision_gate(
    baseline_report: dict[str, Any],
    pg_model_dir: Path,
    pg_val_df_path: Path,
    *,
    baseline_k: int,
) -> dict[str, Any]:
    """Decide whether offline gate passes for serving migration."""

    import pickle

    import pandas as pd

    from trainer_hightier.feature_experiment.run_pipeline import (
        predicted_positive_class_probability,
        prepare_matrix_from_val_split,
    )

    val_df = pd.read_parquet(pg_val_df_path)
    y = (
        pd.to_numeric(val_df["player_game_label"], errors="coerce")
        .fillna(0)
        .astype("int8")
        .to_numpy()
    )
    with open(pg_model_dir / "model.pkl", "rb") as handle:
        pkt = pickle.load(handle)
    xf = prepare_matrix_from_val_split(val_df, pkt)
    scores = predicted_positive_class_probability(pkt["model"], xf)
    pg_at_k = pg_top_k_metrics(y, scores, int(baseline_k))
    base_prec = float(baseline_report.get("val_precision", 0.0))
    base_rec = float(baseline_report.get("val_recall", 0.0))
    tie_tol = 1e-9
    recall_ok = float(pg_at_k["recall"]) + tie_tol >= base_rec
    prec_ok = float(pg_at_k["precision"]) + tie_tol >= base_prec
    proceed = bool(recall_ok and prec_ok)
    reason = (
        "player_game recall/precision at baseline K meet or beat baseline"
        if proceed
        else f"player_game at K={baseline_k}: recall_ok={recall_ok} prec_ok={prec_ok}"
    )
    return {
        "proceed_to_serving_migration": proceed,
        "reason": reason,
        "baseline_val_precision": base_prec,
        "baseline_val_recall": base_rec,
        "baseline_alert_budget_k": int(baseline_k),
        "player_game_precision_at_k": float(pg_at_k["precision"]),
        "player_game_recall_at_k": float(pg_at_k["recall"]),
        "delta_recall_at_k": float(pg_at_k["recall"]) - base_rec,
        "delta_precision_at_k": float(pg_at_k["precision"]) - base_prec,
        "player_game_threshold": float(pkt["threshold"]),
    }


def _load_baseline_report(path: Path | None) -> dict[str, Any] | None:
    """Load an existing Step-5 training metrics JSON for baseline comparison."""

    if path is None:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"baseline report must be a JSON object; got {type(payload)!r}")
    return payload


def run_player_game_grain_experiment(
    *,
    splits_dir: Path,
    out_dir: Path,
    objective_min_precision: float,
    random_seed: int,
    skip_baseline: bool = False,
    baseline_report_json: Path | None = None,
    pg_splits_dir: Path | None = None,
) -> dict[str, Any]:
    """Materialize player-game splits, train arms, and emit decision report."""

    splits_dir = Path(splits_dir).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    duck, _, _ = configs_from_run_profile(get_run_profile("default"))
    step5 = Step5TrainConfig(run_step5=True, skip_optuna=True)
    timing: dict[str, float] = {}
    registry_snap = load_candidate_registry()
    baseline_feature_target = baseline_features_for_main_trainer(registry_snap)
    imported_baseline = _load_baseline_report(baseline_report_json)

    materialize_input_dir = out_dir / "materialize_input"
    if pg_splits_dir is not None:
        pg_splits_dir = Path(pg_splits_dir).resolve()
        mat_audits: dict[str, Any] = {}
        timing["prepare_materialize_input_sec"] = 0.0
        timing["materialize_sec"] = 0.0
    else:
        pg_splits_dir = out_dir / "player_game_splits"
        t0 = time.perf_counter()
        prepare_bet_splits_for_player_game_materialize(
            splits_dir,
            materialize_input_dir,
            duckdb_runtime=duck,
        )
        timing["prepare_materialize_input_sec"] = round(time.perf_counter() - t0, 3)
        t1 = time.perf_counter()
        mat = materialize_player_game_splits(materialize_input_dir, pg_splits_dir, duckdb_runtime=duck)
        timing["materialize_sec"] = round(time.perf_counter() - t1, 3)
        mat_audits = _audit_dict(mat.audits)

    pg_splits_txn_dir = out_dir / "player_game_splits_txn_pg"
    t_txn = time.perf_counter()
    txn_pg_meta = enrich_player_game_splits_with_txn_pg(
        pg_splits_dir,
        pg_splits_txn_dir,
        duckdb_runtime=duck,
    )
    timing["enrich_txn_pg_sec"] = round(time.perf_counter() - t_txn, 3)

    baseline_cols = _feature_columns_present_in_splits(splits_dir, baseline_feature_target)
    if not baseline_cols:
        raise RuntimeError("No baseline feature columns remain in bet splits.")
    pg_splits_baseline_dir = out_dir / "player_game_splits_baseline_parity"
    t_b1 = time.perf_counter()
    baseline_parity_meta = enrich_player_game_splits_with_baseline_bet_features(
        pg_splits_txn_dir,
        splits_dir,
        pg_splits_baseline_dir,
        feature_columns=baseline_cols,
        duckdb_runtime=duck,
    )
    timing["enrich_baseline_parity_sec"] = round(time.perf_counter() - t_b1, 3)
    baseline_parity_feats = _feature_columns_present_in_splits(
        pg_splits_baseline_dir,
        baseline_feature_target,
    )

    schema_names = frozenset(pq.read_schema(pg_splits_dir / "train.parquet").names)
    txn_schema_names = frozenset(pq.read_schema(pg_splits_txn_dir / "train.parquet").names)
    pg_feats = player_game_composition_features(include_settlement=False)
    pg_settle_feats = player_game_composition_features(
        include_settlement=True,
        frame_columns=schema_names,
    )
    txn_feats = tuple(c for c in txn_lite_feature_columns() if c in txn_schema_names)
    pg_txn_feats = pg_feats + txn_feats

    baseline_report: dict[str, Any] | None = imported_baseline
    baseline_dir = out_dir / "baseline_top3_mean"
    if imported_baseline is not None:
        logger.info("[PG-W2] Using imported baseline report from %s", baseline_report_json)
    elif not skip_baseline:
        baseline_dir.mkdir(parents=True, exist_ok=True)
        t1 = time.perf_counter()
        res_base = _b5.train_lgbm_from_splits(
            splits_dir=splits_dir,
            duckdb_runtime=duck,
            objective_min_precision=float(objective_min_precision),
            random_seed=int(random_seed),
            step5=step5,
            output_dir=baseline_dir,
            feature_columns=baseline_cols,
        )
        timing["train_baseline_sec"] = round(time.perf_counter() - t1, 3)
        baseline_report = res_base.report

    if baseline_report is None and skip_baseline:
        logger.warning("[PG-W2] No baseline report available; decision gate will be omitted.")

    arms: dict[str, dict[str, Any]] = {}
    arm_specs = [
        ("player_game_composition", pg_feats, pg_splits_dir, True),
        ("player_game_with_settlement", pg_settle_feats, pg_splits_dir, "pg__casino_win_sum" in schema_names),
        ("player_game_composition_txn_pg", pg_txn_feats, pg_splits_txn_dir, bool(txn_feats)),
        (
            "player_game_baseline_parity",
            baseline_parity_feats,
            pg_splits_baseline_dir,
            bool(baseline_parity_feats),
        ),
    ]
    for arm_id, feat_cols, train_splits_dir, enabled in arm_specs:
        if not enabled:
            arms[arm_id] = {"skipped": True, "reason": "settlement column absent or no txn features"}
            continue
        arm_dir = out_dir / arm_id
        arm_dir.mkdir(parents=True, exist_ok=True)
        t_arm = time.perf_counter()
        res_pg = train_player_game_lgbm_from_splits(
            pg_splits_dir=Path(train_splits_dir),
            duckdb_runtime=duck,
            objective_min_precision=float(objective_min_precision),
            random_seed=int(random_seed),
            step5=step5,
            output_dir=arm_dir,
            feature_columns=feat_cols,
        )
        timing[f"train_{arm_id}_sec"] = round(time.perf_counter() - t_arm, 3)
        arms[arm_id] = {
            "skipped": False,
            "model_dir": str(arm_dir),
            "report": res_pg.report,
            "threshold": float(res_pg.threshold),
        }

    decision: dict[str, Any] | None = None
    decision_txn_pg: dict[str, Any] | None = None
    decision_baseline_parity: dict[str, Any] | None = None
    if baseline_report is not None:
        baseline_k = int(baseline_report.get("val_alerts", 0))
        if arms.get("player_game_composition", {}).get("skipped") is False:
            decision = _decision_gate(
                baseline_report,
                out_dir / "player_game_composition",
                pg_splits_dir / "val.parquet",
                baseline_k=baseline_k,
            )
        if arms.get("player_game_composition_txn_pg", {}).get("skipped") is False:
            decision_txn_pg = _decision_gate(
                baseline_report,
                out_dir / "player_game_composition_txn_pg",
                pg_splits_txn_dir / "val.parquet",
                baseline_k=baseline_k,
            )
        if arms.get("player_game_baseline_parity", {}).get("skipped") is False:
            decision_baseline_parity = _decision_gate(
                baseline_report,
                out_dir / "player_game_baseline_parity",
                pg_splits_baseline_dir / "val.parquet",
                baseline_k=baseline_k,
            )

    return {
        "experiment_kind": "player_game_grain_w2_v1_b1",
        "splits_dir": str(splits_dir),
        "out_dir": str(out_dir),
        "materialize_input_dir": str(materialize_input_dir),
        "player_game_splits_dir": str(pg_splits_dir),
        "player_game_splits_txn_pg_dir": str(pg_splits_txn_dir),
        "player_game_splits_baseline_parity_dir": str(pg_splits_baseline_dir),
        "txn_pg_enrich": txn_pg_meta,
        "baseline_parity_enrich": baseline_parity_meta,
        "baseline_parity_feature_count": len(baseline_parity_feats),
        "materialize_audit": mat_audits,
        "timing_sec": timing,
        "baseline_top3_mean": baseline_report,
        "baseline_source": (
            str(Path(baseline_report_json).resolve())
            if imported_baseline is not None
            else ("retrained" if not skip_baseline else None)
        ),
        "player_game_arms": arms,
        "decision": decision,
        "decision_txn_pg": decision_txn_pg,
        "decision_baseline_parity": decision_baseline_parity,
        "method_note": (
            "Baseline: bet-level LightGBM + top3_mean player-game aggregation. "
            "Player-game arms: native one-row-per-player-game model. "
            "txn_pg: txn__* at player_id + player_game_ready_ts PIT (not bet pcd). "
            "baseline_parity: same 42 MVP features as baseline — non-txn from representative "
            "bet, txn__* from player_game_ready_ts PIT."
        ),
    }


def main() -> None:
    """CLI entry for Wave 2 player-game grain offline experiment."""

    parser = argparse.ArgumentParser(description="Player-game grain Wave 2 offline experiment")
    parser.add_argument("--splits-dir", type=Path, required=True, help="Step 4 bet split directory")
    parser.add_argument("--out-dir", type=Path, required=True, help="Experiment output directory")
    parser.add_argument(
        "--min-precision",
        type=float,
        default=0.5,
        help="Validation precision floor for threshold pick",
    )
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument(
        "--baseline-report-json",
        type=Path,
        default=None,
        help="Existing Step-5 training_metrics.json for top3_mean baseline (skip retrain)",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Skip bet-level baseline training when no imported baseline report is provided",
    )
    parser.add_argument(
        "--pg-splits-dir",
        type=Path,
        default=None,
        help="Reuse existing player-game splits (skip bet materialize)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Decision report JSON (default: <out-dir>/player_game_grain_decision_report.json)",
    )
    ns = parser.parse_args()
    out_dir = Path(ns.out_dir).resolve()
    report = run_player_game_grain_experiment(
        splits_dir=Path(ns.splits_dir),
        out_dir=out_dir,
        objective_min_precision=float(ns.min_precision),
        random_seed=int(ns.random_seed),
        skip_baseline=bool(ns.skip_baseline),
        baseline_report_json=Path(ns.baseline_report_json) if ns.baseline_report_json else None,
        pg_splits_dir=Path(ns.pg_splits_dir) if ns.pg_splits_dir else None,
    )
    out_json = Path(ns.output).resolve() if ns.output else out_dir / "player_game_grain_decision_report.json"
    out_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote player-game grain experiment report → %s", out_json)
    if report.get("decision") is not None:
        proceed = report["decision"]["proceed_to_serving_migration"]
        logger.info("Offline gate (composition) proceed_to_serving_migration=%s", proceed)
    if report.get("decision_txn_pg") is not None:
        proceed_txn = report["decision_txn_pg"]["proceed_to_serving_migration"]
        logger.info("Offline gate (composition+txn_pg) proceed_to_serving_migration=%s", proceed_txn)
    if report.get("decision_baseline_parity") is not None:
        proceed_b1 = report["decision_baseline_parity"]["proceed_to_serving_migration"]
        logger.info("Offline gate (baseline_parity) proceed_to_serving_migration=%s", proceed_b1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
