"""
Backtester for walkaway alert precision.
Runs the trained model over historical bets and reports alert precision.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from typing import List, Tuple

import pandas as pd
import joblib
from pathlib import Path

import trainer as trainer_mod

from trainer import (
    HK_TZ,
    MODEL_DIR,
    load_clickhouse_data,
    build_labels_and_features,
    _to_hk,
)

BASE_DIR = Path(__file__).parent
BACKTEST_OUT = BASE_DIR / "out_backtest"
BACKTEST_OUT.mkdir(exist_ok=True)


def load_artifacts():
    model_path = MODEL_DIR / "walkaway_model.pkl"
    bundle = joblib.load(model_path)
    return bundle["model"], bundle["features"], float(bundle["threshold"])


def default_window() -> Tuple[datetime, datetime]:
    import config
    hours = getattr(config, "BACKTEST_HOURS", 3)
    offset = getattr(config, "BACKTEST_OFFSET_HOURS", 1)
    now = datetime.now(HK_TZ)
    return now - timedelta(hours=hours), now - timedelta(hours=offset)


def parse_window(args) -> Tuple[datetime, datetime]:
    if args.start or args.end:
        start = _to_hk(pd.to_datetime(args.start).to_pydatetime()) if args.start else None
        end = _to_hk(pd.to_datetime(args.end).to_pydatetime()) if args.end else None
        if start is None or end is None:
            raise ValueError("Both --start and --end must be provided when overriding window")
        return start.to_pydatetime(), end.to_pydatetime()
    return default_window()


def backtest(bets: pd.DataFrame, sessions: pd.DataFrame, feature_cols: List[str], model, threshold: float,
             start: datetime, end: datetime) -> dict:
    # Point trainer caches to backtest-only folder and clear them for this run
    trainer_mod.ROLLING_PATH = BACKTEST_OUT / "rolling_bets.csv"
    trainer_mod.FEATURES_PATH = BACKTEST_OUT / "features_buffer.csv"
    trainer_mod.FEATURE_COLS_PATH = BACKTEST_OUT / "feature_cols.json"
    for p in [trainer_mod.ROLLING_PATH, trainer_mod.FEATURES_PATH]:
        if Path(p).exists():
            Path(p).unlink()

    labeled_df, _ = build_labels_and_features(bets, sessions)
    labeled_df = labeled_df.dropna(subset=["session_start_dtm", "session_end_dtm", "payout_complete_dtm"])

    windowed = labeled_df[
        (labeled_df["payout_complete_dtm"] >= start)
        & (labeled_df["payout_complete_dtm"] <= end)
    ].copy()

    if windowed.empty:
        return {
            "alerts": 0,
            "true_alerts": 0,
            "precision": None,
            "notes": "No bets in the requested window",
        }

    X = windowed[feature_cols]  # preserve feature names for model
    proba = model.predict_proba(X)[:, 1]
    windowed["score"] = proba
    windowed["is_alert"] = windowed["score"] >= threshold

    positives_window = int((windowed["label"] == 1).sum())
    max_score = float(windowed["score"].max()) if not windowed.empty else None

    alerts_df = windowed[windowed["is_alert"]].copy()
    true_alerts = int(alerts_df[alerts_df["label"] == 1].shape[0])
    alert_count = int(alerts_df.shape[0])
    precision = float(true_alerts / alert_count) if alert_count > 0 else None

    # Save outputs
    predictions_path = BACKTEST_OUT / "backtest_predictions.csv"
    alerts_path = BACKTEST_OUT / "backtest_alerts.csv"
    windowed.to_csv(predictions_path, index=False)
    alerts_df.to_csv(alerts_path, index=False)

    return {
        "alerts": alert_count,
        "true_alerts": true_alerts,
        "precision": precision,
        "threshold": threshold,
        "predictions_path": str(predictions_path),
        "alerts_path": str(alerts_path),
        "window_rows": int(windowed.shape[0]),
        "window_positives": positives_window,
        "max_score": max_score,
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest walkaway alerts")
    parser.add_argument("--start", help="Window start datetime (HK), e.g. 2024-07-04 18:00", default=None)
    parser.add_argument("--end", help="Window end datetime (HK)", default=None)
    parser.add_argument("--threshold", type=float, help="Override model threshold", default=None)
    args = parser.parse_args()

    model, feature_cols, threshold = load_artifacts()
    if args.threshold is not None:
        threshold = float(args.threshold)
    start, end = parse_window(args)

    bets, sessions = load_clickhouse_data(start, end)
    if bets.empty:
        raise SystemExit("No bets returned from ClickHouse for the requested window")
    if sessions.empty:
        raise SystemExit("No sessions returned from ClickHouse for the requested window")

    result = backtest(bets, sessions, feature_cols, model, threshold, start, end)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
