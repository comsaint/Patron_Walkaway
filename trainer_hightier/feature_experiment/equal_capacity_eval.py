"""Re-score existing Step-5 pickles at fixed alert budgets (equal-capacity comparison)."""

from __future__ import annotations

import argparse
import importlib
import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

from trainer_hightier.evaluation.alert_band_objective import (
    threshold_for_target_operational_alerts,
)
from trainer_hightier.evaluation.player_alert_policy import (
    ALERT_HORIZON_MIN,
    ALERT_TS_COLUMN,
    LABEL_COLUMN,
    PLAYER_ID_COLUMN,
    SCORE_COLUMN,
    TIE_BREAK_COLUMN,
    operational_simulated_metrics_block,
)
from trainer_hightier.feature_experiment.run_pipeline import (
    predicted_positive_class_probability,
    prepare_matrix_from_val_split,
)

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")
aggregate_bets_to_player_game = _b5.aggregate_bets_to_player_game

_DEFAULT_ARMS: tuple[tuple[str, str], ...] = (
    ("baseline", "baseline_lgbm"),
    ("candidate_full", "candidate_lgbm"),
    ("add_net_cash_flow", "txn_col_add_one_net_cash_flow__w1h_lgbm"),
    ("add_buyin_cash_sum", "txn_col_add_one_buyin_cash_sum__w1h_lgbm"),
    ("add_cash_out_sum", "txn_col_add_one_cash_out_sum__w1h_lgbm"),
    ("add_prize_redemption_flag", "txn_col_add_one_buyin_prize_redemption_flag__w1h_lgbm"),
    ("add_net_cash_out_flag", "txn_col_add_one_net_cash_out_flag__w1h_lgbm"),
    ("add_cash_out_cnt", "txn_col_add_one_cash_out_cnt__w1h_lgbm"),
    ("add_has_cash_out", "txn_col_add_one_has_cash_out__w15m_lgbm"),
    ("add_full_txn_group", "txn_col_add_one_full_group_lgbm"),
    ("loo_minus_prize_flag", "txn_col_loo_minus_buyin_prize_redemption_flag__w1h_lgbm"),
    ("loo_minus_net_cash_flow", "txn_col_loo_minus_net_cash_flow__w1h_lgbm"),
)


def window_hours(frame: pd.DataFrame) -> float:
    """Return validation span in hours from payout timestamps."""

    ts = pd.to_datetime(frame["payout_complete_dtm"], errors="coerce")
    if not ts.notna().any():
        return float("nan")
    span = float((ts.max() - ts.min()).total_seconds()) / 3600.0
    return span if np.isfinite(span) and span > 0 else float("nan")


def score_bundle(bundle: dict[str, Any], val_df: pd.DataFrame):
    """Score validation bets and aggregate to player-game grain."""

    xf = prepare_matrix_from_val_split(val_df, bundle)
    scores = predicted_positive_class_probability(bundle["model"], xf)
    pg = aggregate_bets_to_player_game(val_df, scores, split_name="val")
    return pg


def pg_top_k_metrics(y_true: np.ndarray, scores: np.ndarray, k: int) -> dict[str, float | int]:
    """Precision/recall when alerting top-K player-games by score."""

    y = np.asarray(y_true, dtype=np.int8).reshape(-1)
    s = np.asarray(scores, dtype=np.float64).reshape(-1)
    n_pos = int(np.sum(y == 1))
    k_eff = min(int(k), int(len(y)))
    if k_eff <= 0:
        return {"alerts": 0, "precision": 0.0, "recall": 0.0, "true_positives": 0}
    order = np.argsort(-s, kind="mergesort")
    top = order[:k_eff]
    tp = int(np.sum(y[top] == 1))
    return {
        "alerts": k_eff,
        "precision": float(tp / k_eff),
        "recall": float(tp / n_pos) if n_pos > 0 else 0.0,
        "true_positives": tp,
    }


def operational_top_k_metrics(
    candidates: pd.DataFrame,
    target_alerts: int,
    *,
    window_h: float,
    cooldown_min: int = ALERT_HORIZON_MIN,
) -> dict[str, Any]:
    """Greedy top-score operational alerts with player cooldown."""

    if candidates.empty or target_alerts <= 0:
        return {
            "target_alerts": int(target_alerts),
            "alerts": 0,
            "precision": 0.0,
            "recall": 0.0,
            "alerts_per_hour": 0.0,
            "true_positives": 0,
        }
    work = candidates.copy()
    work[ALERT_TS_COLUMN] = pd.to_datetime(work[ALERT_TS_COLUMN], errors="coerce")
    work["_score_sort"] = pd.to_numeric(work[SCORE_COLUMN], errors="coerce")
    work["_tie"] = pd.to_numeric(work[TIE_BREAK_COLUMN], errors="coerce").fillna(-1)
    work = work.sort_values(
        by=["_score_sort", ALERT_TS_COLUMN, "_tie"],
        ascending=[False, True, True],
        kind="mergesort",
    )
    labels_all = (
        pd.to_numeric(work[LABEL_COLUMN], errors="coerce").fillna(0).astype(np.int8).to_numpy()
    )
    n_pos_all = int(np.sum(labels_all == 1))
    cooldown_delta = pd.Timedelta(minutes=int(cooldown_min))
    last_raised: dict[Any, pd.Timestamp] = {}
    raised_mask = np.zeros(len(work), dtype=bool)
    raised = 0
    for i, row in enumerate(work.itertuples(index=False)):
        if raised >= int(target_alerts):
            break
        pid = getattr(row, PLAYER_ID_COLUMN)
        ts = pd.Timestamp(getattr(row, ALERT_TS_COLUMN))
        prev = last_raised.get(pid)
        if prev is not None and (ts - prev) < cooldown_delta:
            continue
        raised_mask[i] = True
        last_raised[pid] = ts
        raised += 1
    y_raised = labels_all[raised_mask]
    tp = int(np.sum(y_raised == 1))
    alerts = int(np.sum(raised_mask))
    prec = tp / alerts if alerts > 0 else 0.0
    rec = tp / n_pos_all if n_pos_all > 0 else 0.0
    ahr = alerts / window_h if np.isfinite(window_h) and window_h > 0 else float("nan")
    return {
        "target_alerts": int(target_alerts),
        "alerts": alerts,
        "precision": float(prec),
        "recall": float(rec),
        "alerts_per_hour": float(ahr),
        "true_positives": tp,
        "positives": n_pos_all,
    }


def threshold_for_operational_alerts(
    candidates: pd.DataFrame,
    target_alerts: int,
    *,
    window_h: float,
    cooldown_min: int = ALERT_HORIZON_MIN,
) -> dict[str, Any]:
    """Binary-search threshold so operational alerts approximate ``target_alerts``."""

    requested_rate = (
        float(target_alerts) / float(window_h)
        if np.isfinite(window_h) and window_h > 0
        else float("nan")
    )
    pt = threshold_for_target_operational_alerts(
        candidates,
        int(target_alerts),
        window_hours=window_h,
        requested_alerts_per_hour=requested_rate,
        cooldown_min=cooldown_min,
        split_prefix="val",
    )
    return {
        "target_alerts": int(target_alerts),
        "threshold": float(pt.threshold),
        "alerts": int(pt.alerts),
        "precision": float(pt.precision),
        "recall": float(pt.recall),
        "alerts_per_hour": pt.alerts_per_hour,
        "true_positives": int(pt.true_positives),
    }


def run_equal_capacity_eval(
    run_dir: Path,
    *,
    arms: tuple[tuple[str, str], ...] = _DEFAULT_ARMS,
) -> dict[str, Any]:
    """Score all arms at baseline-fixed alert budgets; return report blob."""

    run_dir = Path(run_dir).resolve()
    splits = run_dir / "splits"
    val_df = pd.read_parquet(splits / "val.parquet")
    wh = window_hours(val_df)

    base_pkt = pickle.loads((run_dir / "baseline_lgbm" / "model.pkl").read_bytes())
    base_pg = score_bundle(base_pkt, val_df)
    base_thr = float(base_pkt["threshold"])
    base_alerts_pg = int(np.sum(base_pg.scores >= base_thr))
    base_op = operational_simulated_metrics_block(
        "val",
        base_pg.candidates,
        base_thr,
        window_hours=wh,
    )
    base_op_alerts = int(base_op["val_operational_simulated_alerts"])
    target_ks = {
        "K_baseline_pg_picker": base_alerts_pg,
        "K_baseline_op_picker": base_op_alerts,
    }

    results: dict[str, Any] = {}
    for arm_id, subdir in arms:
        pkt = pickle.loads((run_dir / subdir / "model.pkl").read_bytes())
        pg = score_bundle(pkt, val_df)
        ap = float(average_precision_score(pg.y_true, pg.scores)) if pg.y_true.size else 0.0
        pick_thr = float(pkt["threshold"])
        at_own_pick_pg = pg_top_k_metrics(
            pg.y_true,
            pg.scores,
            int(np.sum(pg.scores >= pick_thr)),
        )
        at_own_pick_op = operational_simulated_metrics_block(
            "val",
            pg.candidates,
            pick_thr,
            window_hours=wh,
        )
        arm: dict[str, Any] = {
            "model_dir": subdir,
            "n_features": len(pkt["feature_columns"]),
            "val_ap": ap,
            "own_picker_threshold": pick_thr,
            "own_picker_player_game": at_own_pick_pg,
            "own_picker_operational": {
                "alerts": int(at_own_pick_op["val_operational_simulated_alerts"]),
                "precision": float(at_own_pick_op["val_operational_simulated_precision"]),
                "recall": float(at_own_pick_op["val_operational_simulated_recall"]),
                "alerts_per_hour": at_own_pick_op["val_operational_simulated_alerts_per_hour"],
            },
            "equal_capacity": {},
        }
        for k_name, k_val in target_ks.items():
            pg_eq = pg_top_k_metrics(pg.y_true, pg.scores, int(k_val))
            op_topk = operational_top_k_metrics(pg.candidates, int(k_val), window_h=wh)
            op_thr = threshold_for_operational_alerts(pg.candidates, int(k_val), window_h=wh)
            arm["equal_capacity"][k_name] = {
                "player_game_top_k": pg_eq,
                "operational_top_k_greedy": op_topk,
                "operational_threshold_search": op_thr,
            }
        results[arm_id] = arm

    base_pg_k = results["baseline"]["equal_capacity"]["K_baseline_pg_picker"]["player_game_top_k"]
    base_op_k = results["baseline"]["equal_capacity"]["K_baseline_op_picker"]["operational_top_k_greedy"]
    summary_rows = []
    for arm_id, arm in results.items():
        pg = arm["equal_capacity"]["K_baseline_pg_picker"]["player_game_top_k"]
        op = arm["equal_capacity"]["K_baseline_op_picker"]["operational_top_k_greedy"]
        summary_rows.append(
            {
                "arm": arm_id,
                "val_ap": arm["val_ap"],
                "pg_recall_at_baseline_K": pg["recall"],
                "pg_precision_at_baseline_K": pg["precision"],
                "delta_pg_recall_vs_baseline": float(pg["recall"]) - float(base_pg_k["recall"]),
                "op_recall_at_baseline_op_K": op["recall"],
                "op_precision_at_baseline_op_K": op["precision"],
                "delta_op_recall_vs_baseline": float(op["recall"]) - float(base_op_k["recall"]),
                "own_picker_recall_pg": arm["own_picker_player_game"]["recall"],
                "own_picker_alerts_pg": arm["own_picker_player_game"]["alerts"],
            }
        )

    return {
        "experiment_kind": "equal_capacity_eval_v0",
        "split": "val",
        "window_hours": wh,
        "reference_budgets": {
            "baseline_player_game_alerts_at_picker": base_alerts_pg,
            "baseline_operational_alerts_at_picker": base_op_alerts,
            "baseline_operational_alerts_per_hour_at_picker": base_op.get(
                "val_operational_simulated_alerts_per_hour",
            ),
        },
        "target_ks": target_ks,
        "method_note": (
            "Equal-capacity: alert budget fixed to baseline picker capacity on val. "
            "player_game_top_k ranks player-games by score; operational_top_k_greedy "
            "applies 15m player cooldown on greedy score order."
        ),
        "arms": results,
        "summary_table": sorted(summary_rows, key=lambda r: -r["delta_pg_recall_vs_baseline"]),
    }


def main() -> None:
    """CLI entry."""

    parser = argparse.ArgumentParser(description="Equal-capacity eval for feature experiment pickles")
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Feature experiment run directory containing splits/ and */model.pkl",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path (default: <run-dir>/equal_capacity_eval_report.json)",
    )
    ns = parser.parse_args()
    run_dir = Path(ns.run_dir).resolve()
    out = Path(ns.output).resolve() if ns.output else run_dir / "equal_capacity_eval_report.json"
    report = run_equal_capacity_eval(run_dir)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out}")
    for row in report["summary_table"]:
        print(
            f"{row['arm']:28s} AP={row['val_ap']:.4f} "
            f"dR_pg@K={row['delta_pg_recall_vs_baseline']:+.5f} "
            f"dR_op@K={row['delta_op_recall_vs_baseline']:+.5f} "
            f"own_pick_rec={row['own_picker_recall_pg']:.5f} "
            f"own_alerts={row['own_picker_alerts_pg']}"
        )


if __name__ == "__main__":
    main()
