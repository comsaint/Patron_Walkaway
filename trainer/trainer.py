from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import precision_recall_curve
from sklearn.model_selection import train_test_split
import joblib

from zoneinfo import ZoneInfo

import config
from db_conn import get_clickhouse_client

HK_TZ = ZoneInfo(config.HK_TZ)
BASE_DIR = Path(__file__).parent
OUT_DIR = BASE_DIR / "out_trainer"
MODEL_DIR = BASE_DIR / "models"
OUT_DIR.mkdir(exist_ok=True)
MODEL_DIR.mkdir(exist_ok=True)

# ------------------ Local data buffer helpers ------------------
def get_local_data_paths():
    bets_path = OUT_DIR / "bets_buffer.csv"
    sessions_path = OUT_DIR / "sessions_buffer.csv"
    return bets_path, sessions_path

def local_data_exists():
    bets_path, sessions_path = get_local_data_paths()
    return bets_path.exists() and sessions_path.exists()

def load_local_data():
    bets_path, sessions_path = get_local_data_paths()
    print(f"[trainer] Loading local bets from {bets_path}", flush=True)
    print(f"[trainer] Loading local sessions from {sessions_path}", flush=True)
    bets = pd.read_csv(bets_path, parse_dates=["payout_complete_dtm"])
    # Guard: drop zero/blank wagers even if cached locally
    before = len(bets)
    bets = bets[bets["wager"].fillna(0) > 0].copy()
    if len(bets) != before:
        print(f"[trainer] filtered zero-wager rows from local bets: {before}->{len(bets)}", flush=True)
    sessions = pd.read_csv(sessions_path, parse_dates=["session_start_dtm", "session_end_dtm"])
    return bets, sessions

def save_local_data(bets, sessions):
    bets_path, sessions_path = get_local_data_paths()
    print(f"[trainer] Saving bets to {bets_path}", flush=True)
    bets.to_csv(bets_path, index=False)
    print(f"[trainer] Saving sessions to {sessions_path}", flush=True)
    sessions.to_csv(sessions_path, index=False)

# ------------------ Feature cache helpers ------------------
FEATURES_PATH = OUT_DIR / "features_buffer.csv"
FEATURE_COLS_PATH = OUT_DIR / "feature_cols.json"
ROLLING_PATH = OUT_DIR / "rolling_bets.csv"

def feature_cache_exists():
    return FEATURES_PATH.exists() and FEATURE_COLS_PATH.exists()

def load_feature_cache():
    print(f"[trainer] Loading cached features from {FEATURES_PATH}", flush=True)
    df = pd.read_csv(FEATURES_PATH, parse_dates=["payout_complete_dtm"])
    with FEATURE_COLS_PATH.open("r", encoding="utf-8") as f:
        cols = json.load(f)
    return df, cols

def save_feature_cache(df: pd.DataFrame, feature_cols: List[str]):
    print(f"[trainer] Saving engineered features to {FEATURES_PATH}", flush=True)
    df.to_csv(FEATURES_PATH, index=False)
    with FEATURE_COLS_PATH.open("w", encoding="utf-8") as f:
        json.dump(feature_cols, f, indent=2)

def rolling_cache_exists():
    return ROLLING_PATH.exists()

def load_rolling_cache():
    print(f"[trainer] Loading cached rolling features from {ROLLING_PATH}", flush=True)
    return pd.read_csv(ROLLING_PATH, parse_dates=["payout_complete_dtm"])

def save_rolling_cache(bets_df: pd.DataFrame):
    print(f"[trainer] Saving rolling feature frame to {ROLLING_PATH}", flush=True)
    bets_df.to_csv(ROLLING_PATH, index=False)
"""
Trainer for baccarat walkaway (churn) prediction using live ClickHouse data.
Focus: maximize precision on 15-minute-ahead walkaway alerts.
"""


def load_clickhouse_data(start: datetime, end: datetime) -> Tuple[pd.DataFrame, pd.DataFrame]:
    print(f"[trainer] Pulling data from ClickHouse for {start} to {end}", flush=True)
    print("[trainer] fetching bets and sessions from ClickHouse...", flush=True)

    client = get_clickhouse_client()
    params = {"start": start, "end": end}

    # Diagnostic: print bet counts per day in the window
    diag_query = f"""
        SELECT
            toDate(payout_complete_dtm) as bet_date,
            count(*) as bet_count
        FROM {config.SOURCE_DB}.{config.TBET}
        WHERE payout_complete_dtm >= %(start)s
          AND payout_complete_dtm <= %(end)s
          AND wager > 0
        GROUP BY bet_date
        ORDER BY bet_date
    """
    diag = client.query_df(diag_query, parameters=params)
    print("[diagnostic] Bets per day in window:")
    print(diag)

    bets_query = f"""
        SELECT
            bet_id,
            is_back_bet,
            base_ha,
            bet_type,
            payout_complete_dtm,
            session_id,
            player_id,
            table_id,
            position_idx,
            wager,
            payout_odds,
            status
        FROM {config.SOURCE_DB}.{config.TBET}
        WHERE payout_complete_dtm >= %(start)s
          AND payout_complete_dtm <= %(end)s
          AND wager > 0
    """

    session_query = f"""
        SELECT
            session_id,
            table_id,
            player_id,
            session_start_dtm,
            session_end_dtm
        FROM {config.SOURCE_DB}.{config.TSESSION}
        WHERE session_start_dtm >= %(start)s - INTERVAL 1 DAY
          AND session_start_dtm <= %(end)s + INTERVAL 1 DAY
    """

    bets = client.query_df(bets_query, parameters=params)
    before = len(bets)
    bets = bets[bets["wager"].fillna(0) > 0].copy()
    if len(bets) != before:
        print(f"[trainer] filtered zero-wager bets from ClickHouse: {before}->{len(bets)}", flush=True)
    sessions = client.query_df(session_query, parameters=params)
    return bets, sessions


# ------------------ Feature engineering ------------------
def build_labels_and_features(
    bets: pd.DataFrame, sessions: pd.DataFrame
) -> Tuple[pd.DataFrame, List[str]]:
    print("[trainer] starting feature engineering...", flush=True)
    # Normalize column names
    bets_df = bets.copy()
    sessions_df = sessions.copy()

    # Ensure required numeric columns exist
    for col in ["position_idx", "payout_odds", "base_ha", "is_back_bet", "wager", "bet_id"]:
        if col not in bets_df.columns:
            bets_df[col] = 0
    if "status" not in bets_df.columns:
        bets_df["status"] = None

    # Drop zero/blank wagers upfront
    before_initial = len(bets_df)
    bets_df = bets_df[bets_df.get("wager", 0).fillna(0) > 0].copy()
    if len(bets_df) != before_initial:
        print(f"[trainer] filtered zero-wager rows (source): {before_initial}->{len(bets_df)}", flush=True)

    # --- New engineered features ---
    if rolling_cache_exists():
        bets_df = load_rolling_cache()
        before_cache = len(bets_df)
        bets_df = bets_df[bets_df.get("wager", 0).fillna(0) > 0].copy()
        if len(bets_df) != before_cache:
            print(f"[trainer] filtered zero-wager rows (cached rolling): {before_cache}->{len(bets_df)}", flush=True)
    else:
        # Bet frequency in last 5/15/30 minutes (per session)
        print("[trainer] computing rolling bet counts (5/15/30m)...", flush=True)
        bets_df = bets_df.sort_values(["session_id", "payout_complete_dtm"]).reset_index(drop=True)
        bets_df['orig_idx'] = bets_df.index
        for window in [5, 15, 30]:
            print(f"[trainer]   ...rolling bet count for {window}m window...", flush=True)
            colname = f"bets_last_{window}m"
            session_ids = bets_df['session_id'].unique()
            total_sessions = len(session_ids)
            batch_size = 10000
            for i in range(0, total_sessions, batch_size):
                batch_ids = session_ids[i:i+batch_size]
                batch = bets_df[bets_df['session_id'].isin(batch_ids)]
                # Sort within batch to satisfy rolling monotonic requirement
                batch_sorted = batch.sort_values(["session_id", "payout_complete_dtm", "bet_id"])
                # Compute rolling counts per session; result aligns with batch_sorted index
                rolled_vals = (
                    batch_sorted
                    .groupby("session_id", group_keys=False)
                    .apply(
                        lambda g: g.set_index("payout_complete_dtm")["bet_id"].rolling(f"{window}min").count().to_numpy(),
                        include_groups=False,
                    )
                )
                # Because apply returns a Series of arrays, flatten preserving order
                rolled_vals = np.concatenate(rolled_vals.values)
                bets_df.loc[batch_sorted.index, colname] = rolled_vals
                print(f"[trainer]     ...{min(i+batch_size, total_sessions)}/{total_sessions} sessions done for {window}m window...", flush=True)
        # (session_duration_min will be calculated after merging session info)
        # Rolling sum of wager in last 10/30 mins
        print("[trainer] computing rolling wager sums (10/30m)...", flush=True)
        for window in [10, 30]:
            print(f"[trainer]   ...rolling wager sum for {window}m window...", flush=True)
            colname = f"wager_last_{window}m"
            session_ids = bets_df['session_id'].unique()
            total_sessions = len(session_ids)
            batch_size = 10000
            for i in range(0, total_sessions, batch_size):
                batch_ids = session_ids[i:i+batch_size]
                batch = bets_df[bets_df['session_id'].isin(batch_ids)]
                batch_sorted = batch.sort_values(["session_id", "payout_complete_dtm", "bet_id"])
                rolled_vals = (
                    batch_sorted
                    .groupby("session_id", group_keys=False)
                    .apply(
                        lambda g: g.set_index("payout_complete_dtm")["wager"].rolling(f"{window}min").sum().to_numpy(),
                        include_groups=False,
                    )
                )
                rolled_vals = np.concatenate(rolled_vals.values)
                bets_df.loc[batch_sorted.index, colname] = rolled_vals
                print(f"[trainer]     ...{min(i+batch_size, total_sessions)}/{total_sessions} sessions done for {window}m window...", flush=True)
            # Save cache after computations (already filtered upstream)
        save_rolling_cache(bets_df)

    print("[trainer] merging session info and computing session-based features...", flush=True)
    print("[trainer] feature engineering complete.", flush=True)

    # Coerce identifiers to numeric and drop missing session ids
    for col in ["session_id", "player_id", "table_id", "bet_id"]:
        bets_df[col] = pd.to_numeric(bets_df.get(col), errors="coerce")
    for col in ["session_id", "player_id", "table_id"]:
        sessions_df[col] = pd.to_numeric(sessions_df.get(col), errors="coerce")

    # Drop rows lacking core identifiers, then enforce integer storage
    bets_df = bets_df.dropna(subset=["session_id", "bet_id"])
    sessions_df = sessions_df.dropna(subset=["session_id"])
    bets_df["session_id"] = bets_df["session_id"].astype("Int64")
    bets_df["bet_id"] = bets_df["bet_id"].astype("Int64")
    sessions_df["session_id"] = sessions_df["session_id"].astype("Int64")

    # Deduplicate sessions with deterministic aggregation per session_id
    sessions_df = sessions_df.sort_values(["session_id", "session_end_dtm"])
    sessions_df = (
        sessions_df.groupby("session_id", as_index=False)
        .agg(
            {
                "player_id": "first",
                "table_id": "first",
                "session_start_dtm": "min",
                "session_end_dtm": "max",
            }
        )
    )

    # Ensure datetime tz aware
    for col in ["payout_complete_dtm"]:
        if bets_df[col].dt.tz is None:
            bets_df[col] = bets_df[col].dt.tz_localize(HK_TZ)
        else:
            bets_df[col] = bets_df[col].dt.tz_convert(HK_TZ)
    for col in ["session_start_dtm", "session_end_dtm"]:
        if sessions_df[col].dt.tz is None:
            sessions_df[col] = sessions_df[col].dt.tz_localize(HK_TZ)
        else:
            sessions_df[col] = sessions_df[col].dt.tz_convert(HK_TZ)

    # Deduplicate sessions to keep a single record per session_id (choose latest end)
    sessions_df = sessions_df.sort_values(["session_id", "session_end_dtm"])
    sessions_df = sessions_df.drop_duplicates(subset=["session_id"], keep="last")

    print("[trainer] computing table headcount (per-table occupancy)...", flush=True)
    # Table headcount at bet time (number of concurrent sessions on the same table)
    try:
        sess_occ = sessions_df.dropna(subset=["table_id"]).copy()
        sess_occ["session_start_dtm"] = pd.to_datetime(sess_occ["session_start_dtm"], errors="coerce")
        sess_occ["session_end_dtm"] = pd.to_datetime(sess_occ["session_end_dtm"], errors="coerce")
        sess_occ["session_end_dtm"] = sess_occ["session_end_dtm"].fillna(sess_occ["session_start_dtm"])
        sess_occ["table_id_str"] = sess_occ["table_id"].astype(str)
        sess_occ = sess_occ.dropna(subset=["session_start_dtm", "session_end_dtm"]).copy()

        bets_df["payout_complete_dtm"] = pd.to_datetime(bets_df["payout_complete_dtm"], errors="coerce")
        bets_df = bets_df.dropna(subset=["payout_complete_dtm"]).copy()
        bets_df["table_id_str"] = bets_df["table_id"].astype(str)

        def to_utc_ns(series: pd.Series) -> pd.Series:
            # Localize naive timestamps to UTC; convert aware timestamps to UTC
            if series.dt.tz is None:
                series = series.dt.tz_localize("UTC", nonexistent="NaT", ambiguous="NaT")
            else:
                series = series.dt.tz_convert("UTC")
            return series.astype("int64")

        sess_occ["ts_start_ns"] = to_utc_ns(sess_occ["session_start_dtm"])
        sess_occ["ts_end_ns"] = to_utc_ns(sess_occ["session_end_dtm"])
        bets_df["payout_ns"] = to_utc_ns(bets_df["payout_complete_dtm"])

        bets_df["table_hc"] = 0
        for tid, ev in sess_occ.groupby("table_id_str", sort=False):
            bet_idx = bets_df.index[bets_df["table_id_str"] == tid]
            if bet_idx.empty:
                continue
            bet_ns = bets_df.loc[bet_idx, "payout_ns"].to_numpy()
            ev_ns = np.concatenate([ev["ts_start_ns"].to_numpy(), ev["ts_end_ns"].to_numpy()])
            deltas = np.concatenate([np.ones(len(ev), dtype=int), -np.ones(len(ev), dtype=int)])
            order = np.argsort(ev_ns, kind="mergesort")
            ev_ns_sorted = ev_ns[order]
            occ = np.cumsum(deltas[order])
            pos = ev_ns_sorted.searchsorted(bet_ns, side="right") - 1
            vals = np.where(pos >= 0, occ[pos], 0)
            bets_df.loc[bet_idx, "table_hc"] = vals
        bets_df["table_hc"] = bets_df["table_hc"].fillna(0)
    except Exception as e:
        print(f"[trainer] table headcount computation failed; defaulting to 0: {e}", flush=True)
        bets_df["table_hc"] = 0
    print("[trainer] table headcount computation complete.", flush=True)

    # Compute next session per player to derive visit gap
    sessions_df = sessions_df.sort_values(["player_id", "session_start_dtm"])
    sessions_df["next_session_start"] = sessions_df.groupby("player_id")[
        "session_start_dtm"
    ].shift(-1)
    sessions_df["gap_to_next_min"] = (
        (sessions_df["next_session_start"] - sessions_df["session_end_dtm"])
        .dt.total_seconds()
        .div(60)
    )

    # Merge session info into bets (only once, after bet-level rolling features)
    merged = bets_df.merge(
        sessions_df[
            [
                "session_id",
                "player_id",
                "table_id",
                "session_start_dtm",
                "session_end_dtm",
                "gap_to_next_min",
            ]
        ],
        on="session_id",
        how="left",
        validate="many_to_one",
    )

    # Session duration so far (now that session_start_dtm is available)
    merged["session_duration_min"] = (
        merged["payout_complete_dtm"] - merged["session_start_dtm"]
    ).dt.total_seconds() / 60.0

    # Time-based features
    merged["minutes_since_session_start"] = (
        merged["payout_complete_dtm"] - merged["session_start_dtm"]
    ).dt.total_seconds() / 60.0

    # Minutes to session end (fallback to 0 if no end provided)
    merged["minutes_to_session_end"] = (
        merged["session_end_dtm"] - merged["payout_complete_dtm"]
    ).dt.total_seconds() / 60.0

    # Time-of-day cyclic encoding (minutes into day)
    merged["minutes_into_day"] = merged["payout_complete_dtm"].dt.hour * 60 + merged["payout_complete_dtm"].dt.minute
    merged["time_of_day_sin"] = np.sin(2 * np.pi * merged["minutes_into_day"] / 1440)
    merged["time_of_day_cos"] = np.cos(2 * np.pi * merged["minutes_into_day"] / 1440)

    # Label: gap >= 30 mins and alert window 15 mins before walkaway
    merged["gap_to_next_min"] = merged["gap_to_next_min"].fillna(1e9)
    merged["label"] = (
        (merged["gap_to_next_min"] >= 30)
        & (merged["minutes_to_session_end"] >= 0)
        & (merged["minutes_to_session_end"] <= 15)
    ).astype(int)

    # Simple rolling aggregations per session
    # Sort within session strictly by payout_complete_dtm (bet_id is non-monotonic per briefing)
    merged = merged.sort_values(["session_id", "payout_complete_dtm", "bet_id"])

    # Ensure numeric types for cumulative calcs
    merged["wager"] = pd.to_numeric(merged["wager"], errors="coerce").fillna(0)
    merged["payout_odds"] = pd.to_numeric(merged["payout_odds"], errors="coerce").fillna(0)
    merged["base_ha"] = pd.to_numeric(merged["base_ha"], errors="coerce").fillna(0)
    merged["is_back_bet"] = pd.to_numeric(merged["is_back_bet"], errors="coerce").fillna(0)
    merged["position_idx"] = pd.to_numeric(merged["position_idx"], errors="coerce").fillna(0)

    merged["cum_bets"] = merged.groupby("session_id").cumcount() + 1
    merged["cum_wager"] = merged.groupby("session_id")["wager"].cumsum()
    merged["avg_wager_sofar"] = merged["cum_wager"] / merged["cum_bets"]
    merged["bets_per_minute"] = merged["cum_bets"] / (merged["session_duration_min"] + 1e-3)

    # Loss streak per session (consecutive LOSE bets up to current bet)
    def _loss_streak(g: pd.DataFrame) -> pd.Series:
        streak = 0
        out = []
        for st in g["status"]:
            if isinstance(st, str) and st.upper() == "LOSE":
                streak += 1
            else:
                streak = 0
            out.append(streak)
        return pd.Series(out, index=g.index)

    merged = merged.sort_values(["session_id", "payout_complete_dtm", "bet_id"])
    merged["loss_streak"] = (
        merged.groupby("session_id", group_keys=False).apply(_loss_streak, include_groups=False)
    )

    feature_cols = [
        "wager",
        "payout_odds",
        "base_ha",
        "is_back_bet",
        "position_idx",
        "minutes_since_session_start",
        "cum_bets",
        "cum_wager",
        "avg_wager_sofar",
        "table_hc",
        # New features
        "bets_last_5m",
        "bets_last_15m",
        "bets_last_30m",
        "session_duration_min",
        "wager_last_10m",
        "wager_last_30m",
        "bets_per_minute",
        "time_of_day_sin",
        "time_of_day_cos",
        "loss_streak",
    ]

    merged[feature_cols] = merged[feature_cols].fillna(0)
    return merged, feature_cols


# ------------------ Time window helpers ------------------
def _to_hk(dt_like: datetime) -> datetime:
    if dt_like.tzinfo is None:
        return dt_like.replace(tzinfo=HK_TZ)
    return dt_like.astimezone(HK_TZ)


def default_training_window(days: int = None) -> Tuple[datetime, datetime]:
    import config
    if days is None:
        days = getattr(config, "TRAINER_DAYS", 7)
    now = datetime.now(HK_TZ)
    return now - timedelta(days=days), now - timedelta(minutes=30)


def parse_window(args) -> Tuple[datetime, datetime]:
    if args.start or args.end:
        if not (args.start and args.end):
            raise ValueError("Provide both --start and --end or neither")
        start = _to_hk(pd.to_datetime(args.start).to_pydatetime())
        end = _to_hk(pd.to_datetime(args.end).to_pydatetime())
        return start, end
    return default_training_window(getattr(args, "days", None))


# ------------------ Training ------------------

def train_and_select_model(df, feature_cols):
    """
    Always train and compare multiple models (RandomForest, GradientBoosting, LightGBM if available),
    selecting the best by validation precision.
    """
    # Time-based split to avoid leakage: oldest 80% train, most recent 20% validation
    df_sorted = df.sort_values("payout_complete_dtm")
    cutoff_idx = int(len(df_sorted) * 0.8)
    train_df = df_sorted.iloc[:cutoff_idx]
    val_df = df_sorted.iloc[cutoff_idx:]
    X_train, y_train = train_df[feature_cols], train_df["label"]
    X_val, y_val = val_df[feature_cols], val_df["label"]

    models = {}

    base_lgb_params = {
        "n_estimators": 400,
        "learning_rate": 0.05,
        "colsample_bytree": 0.8,
        "subsample": 0.8,
        "subsample_freq": 1,
        "max_depth": 8,
        "max_bin": 64,
        "min_data_in_bin": 5,
        "force_col_wise": True,
        "class_weight": "balanced",
        "random_state": 42,
        "n_jobs": -1,
    }
    default_lgb_grid = [
        {"num_leaves": 31, "min_child_samples": 20},
        # {"num_leaves": 63, "min_child_samples": 40},
    ]
    lgb_grid = getattr(config, "LGBM_PARAM_GRID", default_lgb_grid)
    for idx, params in enumerate(lgb_grid):
        name = f"LightGBM_grid{idx+1}"
        models[name] = lgb.LGBMClassifier(**base_lgb_params, **params)

    print(f"[trainer] Model configs: {list(models.keys())}", flush=True)

    best_metrics = None
    best_model = None
    best_name = None
    for idx, (name, model) in enumerate(models.items(), start=1):
        print(f"\n=== {name} ({idx}/{len(models)}) ===", flush=True)
        print(f"Training {name}...", flush=True)
        model.fit(
            X_train,
            y_train,
            eval_set=[(X_val, y_val)],
            eval_metric="binary_logloss",
            callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)],
        )
        best_iter = model.best_iteration_ if hasattr(model, "best_iteration_") else None
        val_scores = model.predict_proba(X_val, num_iteration=best_iter)[:, 1]
        thresholds = np.append(np.sort(np.unique(val_scores)), 1.0)
        positives = (y_val == 1).sum()
        min_recall = 0.02
        min_alerts = 5
        best = None
        for t in thresholds:
            preds = val_scores >= t
            alerts = int(preds.sum())
            if alerts < min_alerts:
                continue
            tp = int(((preds) & (y_val == 1)).sum())
            if alerts == 0:
                continue
            prec = tp / alerts
            rec = tp / positives if positives > 0 else 0.0
            if rec < min_recall:
                continue
            if best is None or prec > best["precision"] or (prec == best["precision"] and rec > best["recall"]):
                best = {"threshold": float(t), "precision": float(prec), "recall": float(rec), "alerts": alerts, "tp": tp}
        if best is None:
            fallback = None
            for t in thresholds:
                preds = val_scores >= t
                alerts = int(preds.sum())
                if alerts == 0:
                    continue
                tp = int(((preds) & (y_val == 1)).sum())
                prec = tp / alerts
                rec = tp / positives if positives > 0 else 0.0
                if fallback is None or prec > fallback["precision"] or (prec == fallback["precision"] and rec > fallback["recall"]):
                    fallback = {"threshold": float(t), "precision": float(prec), "recall": float(rec), "alerts": alerts, "tp": tp}
            best = fallback or {"threshold": 0.5, "precision": 0.0, "recall": 0.0, "alerts": 0, "tp": 0}
        metrics = {
            "validation_precision": best["precision"],
            "validation_recall": best["recall"],
            "threshold": best["threshold"],
            "positive_rate": float(df_sorted["label"].mean()),
            "val_samples": int(len(y_val)),
            "val_alerts": int(best.get("alerts", 0)),
            "val_true_alerts": int(best.get("tp", 0)),
            "model_type": name,
        }
        print(f"{name} validation precision: {metrics['validation_precision']:.4f}")
        if best_metrics is None or metrics["validation_precision"] > best_metrics["validation_precision"]:
            best_metrics = metrics
            best_model = model
            best_name = name

    print(f"Selected model: {best_name} (precision={best_metrics['validation_precision']:.4f})")
    return {"model": best_model, "metrics": best_metrics}


def save_artifacts(model, feature_cols: List[str], metrics: dict) -> None:
    model_path = MODEL_DIR / "walkaway_model.pkl"
    joblib.dump({"model": model, "features": feature_cols, "threshold": metrics["threshold"]}, model_path)

    metrics_path = OUT_DIR / "training_metrics.json"
    with metrics_path.open("w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)


# ------------------ CLI ------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Train walkaway model")
    parser.add_argument("--start", help="Training window start (HK)", default=None)
    parser.add_argument("--end", help="Training window end (HK)", default=None)
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="If start/end not provided, use last N days ending 30m ago (defaults to config.TRAINER_DAYS)",
    )
    args = parser.parse_args()


    bets_path, sessions_path = get_local_data_paths()
    if local_data_exists():
        bets, sessions = load_local_data()
        # Auto-adjust time window to fit local data
        min_dt = bets["payout_complete_dtm"].min()
        max_dt = bets["payout_complete_dtm"].max()
        print(f"[trainer] Using local data window: {min_dt} to {max_dt}", flush=True)
        start, end = min_dt, max_dt
    else:
        start, end = parse_window(args)
        print(f"[trainer] window: {start.isoformat()} to {end.isoformat()}", flush=True)
        bets, sessions = load_clickhouse_data(start, end)
        print(f"[trainer] loaded bets: {len(bets):,}, sessions: {len(sessions):,}", flush=True)
        if bets.empty:
            raise SystemExit("No bets returned from ClickHouse for the requested window")
        if sessions.empty:
            raise SystemExit("No sessions returned from ClickHouse for the requested window")
        save_local_data(bets, sessions)

    if feature_cache_exists():
        labeled_df, feature_cols = load_feature_cache()
        print(f"[trainer] rows (cached): {len(labeled_df):,}, positives: {int(labeled_df['label'].sum()):,}", flush=True)
    else:
        labeled_df, feature_cols = build_labels_and_features(bets, sessions)
        labeled_df = labeled_df.dropna(subset=["session_start_dtm", "session_end_dtm"])
        if labeled_df.empty:
            raise SystemExit("No usable rows after merging bets with sessions")
        print(
            f"[trainer] rows after merge: {len(labeled_df):,}, positives: {int(labeled_df['label'].sum()):,}",
            flush=True,
        )
        save_feature_cache(labeled_df, feature_cols)


    print("[trainer] training and comparing multiple models...", flush=True)
    artifacts = train_and_select_model(labeled_df, feature_cols)
    save_artifacts(artifacts["model"], feature_cols, artifacts["metrics"])
    print("[trainer] training complete; artifacts saved", flush=True)

    summary = {
        "metrics": artifacts["metrics"],
        "features": feature_cols,
        "model_path": str(MODEL_DIR / "walkaway_model.pkl"),
        "metrics_path": str(OUT_DIR / "training_metrics.json"),
        "samples": len(labeled_df),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
