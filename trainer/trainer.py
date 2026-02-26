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
            toDate(b.payout_complete_dtm) as bet_date,
            count(*) as bet_count
        FROM {config.SOURCE_DB}.{config.TBET} b
        INNER JOIN {config.SOURCE_DB}.{config.TSESSION} s
            ON b.session_id = s.session_id
        WHERE b.payout_complete_dtm >= %(start)s
          AND b.payout_complete_dtm <= %(end)s
          AND s.casino_player_id IS NOT NULL
          AND s.casino_player_id NOT IN ('[NULL]', 'null', '')
          AND toInt64OrNull(s.casino_player_id) IS NOT NULL
        GROUP BY bet_date
        ORDER BY bet_date
    """
    diag = client.query_df(diag_query, parameters=params)
    print("[diagnostic] Bets per day in window:")
    print(diag)

    bets_query = f"""
        SELECT
            b.bet_id,
            b.is_back_bet,
            b.base_ha,
            b.bet_type,
            b.payout_complete_dtm,
            b.session_id,
            s.casino_player_id,
            b.table_id,
            b.position_idx,
            b.wager,
            b.payout_odds,
            b.status
        FROM {config.SOURCE_DB}.{config.TBET} b
        INNER JOIN {config.SOURCE_DB}.{config.TSESSION} s
            ON b.session_id = s.session_id
                WHERE b.payout_complete_dtm >= %(start)s
                    AND b.payout_complete_dtm <= %(end)s
                    AND s.casino_player_id IS NOT NULL
                    AND s.casino_player_id NOT IN ('[NULL]', 'null', '')
                    AND toInt64OrNull(s.casino_player_id) IS NOT NULL
    """

    session_query = f"""
        SELECT
            session_id,
            table_id,
                        casino_player_id,
                        session_start_dtm,
                        session_end_dtm
        FROM {config.SOURCE_DB}.{config.TSESSION}
        WHERE session_start_dtm >= %(start)s - INTERVAL 1 DAY
          AND session_start_dtm <= %(end)s + INTERVAL 1 DAY
                    AND casino_player_id IS NOT NULL
                    AND casino_player_id NOT IN ('[NULL]', 'null', '')
                    AND toInt64OrNull(casino_player_id) IS NOT NULL
    """

    bets = client.query_df(bets_query, parameters=params)
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

    # Normalize types; retain back bets and seat_id 0/NaN for modeling
    bets_df["is_back_bet"] = pd.to_numeric(bets_df.get("is_back_bet"), errors="coerce").fillna(0)
    bets_df["position_idx"] = pd.to_numeric(bets_df.get("position_idx"), errors="coerce")

    # --- New engineered features ---
    rolling_needed_cols = [
        "bet_5m",
        "bet_15m",
        "wager_5m",
        "wager_15m",
        "loss_count_5m",
        "loss_count_15m",
        "loss_amount_5m",
        "loss_amount_15m",
        "wager_std_5m",
        "wager_std_15m",
        "wager_cv_5m",
        "wager_cv_15m",
    ]

    use_cached_roll = False
    if rolling_cache_exists():
        cached = load_rolling_cache()
        missing_roll = [c for c in rolling_needed_cols if c not in cached.columns]
        if missing_roll:
            print(f"[trainer] Rolling cache missing columns {missing_roll}; recomputing...", flush=True)
        else:
            bets_df = cached
            use_cached_roll = True
            print("[diagnostic] Columns in rolling cache after load:", list(bets_df.columns), flush=True)

    if not use_cached_roll:
        # Rolling aggregates over 5/15 mins (count, wager, loss count/amount, wager volatility)
        print("[trainer] computing rolling aggregates (5/15m)...", flush=True)
        status_series = bets_df.get("status")
        if status_series is None:
            status_series = pd.Series([""], index=bets_df.index)
        status_series = status_series.astype(str).str.upper()
        bets_df["is_loss"] = status_series.eq("LOSE")
        bets_df["loss_wager"] = bets_df["wager"].where(bets_df["is_loss"], 0)

        # Single pass per window using vectorized groupby.rolling (faster than per-session apply)
        sorted_idx = bets_df.sort_values(["session_id", "payout_complete_dtm", "bet_id"]).index
        indexed = bets_df.loc[sorted_idx]
        by_sess = indexed.set_index("payout_complete_dtm")
        for window in [5, 15]:
            print(f"[trainer]   ...rolling bet/wager/loss for {window}m window...", flush=True)
            roll = (
                by_sess
                .groupby("session_id", group_keys=False)
                .rolling(f"{window}min")
                .agg({
                    "bet_id": "count",
                    "wager": ["sum", "std"],
                    "is_loss": "sum",
                    "loss_wager": "sum",
                })
            )
            # Flatten multiindex columns
            roll.columns = ["_".join([c for c in col if c]) for col in roll.columns.to_flat_index()]
            roll = roll.rename(
                columns={
                    "bet_id_count": f"bet_{window}m",
                    "wager_sum": f"wager_{window}m",
                    "wager_std": f"wager_std_{window}m",
                    "is_loss_sum": f"loss_count_{window}m",
                    "loss_wager_sum": f"loss_amount_{window}m",
                }
            ).reset_index(level=0, drop=True)

            # Compute coefficient of variation for wager (std / mean) safely
            mean_wager = roll[f"wager_{window}m"] / roll[f"bet_{window}m"].replace({0: np.nan})
            roll[f"wager_cv_{window}m"] = (roll[f"wager_std_{window}m"] / mean_wager).replace([np.inf, -np.inf], np.nan)

            # Assign back to the original frame using sorted index alignment
            bets_df.loc[sorted_idx, f"bet_{window}m"] = roll[f"bet_{window}m"].to_numpy()
            bets_df.loc[sorted_idx, f"wager_{window}m"] = roll[f"wager_{window}m"].to_numpy()
            bets_df.loc[sorted_idx, f"wager_std_{window}m"] = roll[f"wager_std_{window}m"].to_numpy()
            bets_df.loc[sorted_idx, f"wager_cv_{window}m"] = roll[f"wager_cv_{window}m"].to_numpy()
            bets_df.loc[sorted_idx, f"loss_count_{window}m"] = roll[f"loss_count_{window}m"].to_numpy()
            bets_df.loc[sorted_idx, f"loss_amount_{window}m"] = roll[f"loss_amount_{window}m"].to_numpy()
        # Fill NaNs introduced by std/cv with 0
        bets_df[rolling_needed_cols] = bets_df[rolling_needed_cols].fillna(0)
        save_rolling_cache(bets_df)

    print("[trainer] merging session info and computing session-based features...", flush=True)
    print("[trainer] feature engineering complete.", flush=True)

    # Coerce identifiers to numeric (except casino_player_id which remains string) and drop missing session ids
    for col in ["session_id", "table_id", "bet_id"]:
        bets_df[col] = pd.to_numeric(bets_df.get(col), errors="coerce")
    for col in ["session_id", "table_id"]:
        sessions_df[col] = pd.to_numeric(sessions_df.get(col), errors="coerce")

    # Normalize casino_player_id as string key
    bets_df["casino_player_id"] = bets_df.get("casino_player_id").astype(str)
    sessions_df["casino_player_id"] = sessions_df.get("casino_player_id").astype(str)

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
                "casino_player_id": "first",
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

    # Compute next session per player to derive visit gap
    sessions_df = sessions_df.sort_values(["casino_player_id", "session_start_dtm"])
    sessions_df["next_session_start"] = sessions_df.groupby("casino_player_id")[
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
                "casino_player_id",
                "table_id",
                "session_start_dtm",
                "session_end_dtm",
                "gap_to_next_min",
            ]
        ],
        on="session_id",
        how="left",
        validate="many_to_one",
        suffixes=("_bet", "_session"),
    )
    print("[diagnostic] Columns after merge:", list(merged.columns), flush=True)
    # Canonicalize identifiers
    merged["table_id"] = merged["table_id_session"].combine_first(merged["table_id_bet"])
    merged["casino_player_id"] = merged["casino_player_id_session"].combine_first(
        merged["casino_player_id_bet"]
    )
    if merged["table_id"].isna().all():
        raise SystemExit("table_id missing after merge; cannot proceed")
    # Drop suffixed duplicates to avoid confusion
    merged = merged.drop(
        columns=[
            "table_id_bet",
            "table_id_session",
            "casino_player_id_bet",
            "casino_player_id_session",
        ],
        errors="ignore",
    )

    # Session duration so far (now that session_start_dtm is available)
    merged["session_duration_min"] = (
        merged["payout_complete_dtm"] - merged["session_start_dtm"]
    ).dt.total_seconds() / 60.0

    merged["minutes_to_session_end"] = (
        merged["session_end_dtm"] - merged["payout_complete_dtm"]
    ).dt.total_seconds() / 60.0

    # Time-based features (no look-ahead)
    merged["minutes_since_session_start"] = (
        merged["payout_complete_dtm"] - merged["session_start_dtm"]
    ).dt.total_seconds() / 60.0

    # Label: gap >= 30 mins and alert window 15 mins before walkaway
    merged["gap_to_next_min"] = merged["gap_to_next_min"].fillna(1e9)
    merged["minutes_to_session_end"] = merged["minutes_to_session_end"].fillna(1e9)
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
    merged["loss_wager"] = pd.to_numeric(merged.get("loss_wager"), errors="coerce").fillna(0)

    # Clip heavy tails on wagers/odds to reduce split noise
    for col in ["wager", "payout_odds"]:
        low, high = merged[col].quantile([0.01, 0.99])
        merged[col] = merged[col].clip(lower=low, upper=high)

    merged["cum_bets"] = merged.groupby("session_id").cumcount() + 1
    merged["cum_wager"] = merged.groupby("session_id")["wager"].cumsum()
    merged["cum_loss"] = merged.groupby("session_id")["loss_wager"].cumsum()
    merged["avg_wager_sofar"] = merged["cum_wager"] / merged["cum_bets"]
    merged["bets_per_minute"] = merged["cum_bets"] / (merged["session_duration_min"] + 1e-3)

    # Time-of-day cyclical encoding
    minute_of_day = (
        merged["payout_complete_dtm"].dt.hour * 60
        + merged["payout_complete_dtm"].dt.minute
        + merged["payout_complete_dtm"].dt.second / 60.0
    )
    merged["time_of_day_sin"] = np.sin(2 * np.pi * minute_of_day / (24 * 60))
    merged["time_of_day_cos"] = np.cos(2 * np.pi * minute_of_day / (24 * 60))

    # Acceleration features based on 5m vs 15m windows (slope per minute)
    merged["bet_acceleration"] = (merged["bet_15m"] - merged["bet_5m"]) / 10.0
    merged["wager_acceleration"] = (merged["wager_15m"] - merged["wager_5m"]) / 10.0
    merged["loss_count_acceleration"] = (merged["loss_count_15m"] - merged["loss_count_5m"]) / 10.0
    merged["loss_amount_acceleration"] = (merged["loss_amount_15m"] - merged["loss_amount_5m"]) / 10.0

    # Pace change: recent 5m bets/min minus prior 10m average pace
    prev10_rate = (merged["bet_15m"] - merged["bet_5m"]) / 10.0
    merged["pace_delta_5_vs_prev10"] = (merged["bet_5m"] / 5.0) - prev10_rate

    # Loss ratios to highlight tilt
    merged["loss_rate_5m"] = merged["loss_amount_5m"] / (merged["wager_5m"] + 1e-3)
    merged["loss_rate_15m"] = merged["loss_amount_15m"] / (merged["wager_15m"] + 1e-3)
    merged["loss_share_session"] = merged["cum_loss"] / (merged["cum_wager"] + 1e-3)

    # Table headcount at bet time (>=1)
    if "table_id" in sessions_df.columns and not sessions_df["table_id"].isna().all():
        sessions_for_hc = sessions_df[["table_id", "session_start_dtm", "session_end_dtm"]].copy()
        sessions_for_hc["session_end_dtm"] = sessions_for_hc["session_end_dtm"].fillna(
            sessions_for_hc["session_start_dtm"]
        )
        events_start = sessions_for_hc.rename(columns={"session_start_dtm": "ts"})[["table_id", "ts"]]
        events_start["delta"] = 1
        events_end = sessions_for_hc.rename(columns={"session_end_dtm": "ts"})[["table_id", "ts"]]
        events_end["ts"] = events_end["ts"] + pd.Timedelta(seconds=1)
        events_end["delta"] = -1
        events = pd.concat([events_start, events_end], ignore_index=True)
        events = events.sort_values(["table_id", "ts"])
        events["headcount"] = events.groupby("table_id")["delta"].cumsum()
        timeline = events.drop_duplicates(subset=["table_id", "ts"], keep="last")[["table_id", "ts", "headcount"]]

        # Ensure both DataFrames are strictly sorted by table_id and time for merge_asof
        merged = merged.dropna(subset=["table_id", "payout_complete_dtm"])
        timeline = timeline.dropna(subset=["table_id", "ts"])
        # Sort primarily by timestamp to satisfy merge_asof's monotonic left_key requirement;
        # secondary sort by table_id keeps per-table grouping stable.
        merged = merged.sort_values(["payout_complete_dtm", "table_id"], kind="mergesort").reset_index(drop=True)
        timeline = timeline.sort_values(["ts", "table_id"], kind="mergesort").reset_index(drop=True)
        # Drop duplicate timestamps within table_id groups (keep first in sorted order)
        merged = merged.drop_duplicates(subset=["table_id", "payout_complete_dtm"], keep="first")
        timeline = timeline.drop_duplicates(subset=["table_id", "ts"], keep="first")
        # Final stable sort (time-first) before merge_asof
        merged = merged.sort_values(["payout_complete_dtm", "table_id"], kind="mergesort").reset_index(drop=True)
        timeline = timeline.sort_values(["ts", "table_id"], kind="mergesort").reset_index(drop=True)

        # Explicit monotonicity checks to catch any remaining out-of-order keys early
        if not merged["payout_complete_dtm"].is_monotonic_increasing:
            raise SystemExit("payout_complete_dtm not sorted after final sort; cannot merge_asof")
        if not timeline["ts"].is_monotonic_increasing:
            raise SystemExit("timeline ts not sorted after final sort; cannot merge_asof")

        # Diagnostic print for sorted keys
        print("[diagnostic] Sorted merged head for merge_asof:", merged[["table_id", "payout_complete_dtm"]].head(), flush=True)
        print("[diagnostic] Sorted timeline head for merge_asof:", timeline[["table_id", "ts"]].head(), flush=True)
        merged = pd.merge_asof(
            merged,
            timeline,
            left_on="payout_complete_dtm",
            right_on="ts",
            by="table_id",
            direction="backward",
        )
        merged["table_hc"] = merged["headcount"].fillna(1).clip(lower=1).astype(int)
        merged = merged.drop(columns=["ts", "headcount"], errors="ignore")
    else:
        raise SystemExit("table_id missing in session data; refetch sessions before training")

    # Lose streak (consecutive losses looking backward within session)
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
        # New features
        "bet_5m",
        "bet_15m",
        "wager_5m",
        "wager_15m",
        "loss_count_5m",
        "loss_count_15m",
        "loss_amount_5m",
        "loss_amount_15m",
        "wager_std_5m",
        "wager_std_15m",
        "wager_cv_5m",
        "wager_cv_15m",
        "bets_per_minute",
        "time_of_day_sin",
        "time_of_day_cos",
        "table_hc",
        "bet_acceleration",
        "wager_acceleration",
        "loss_count_acceleration",
        "loss_amount_acceleration",
        "pace_delta_5_vs_prev10",
        "loss_rate_5m",
        "loss_rate_15m",
        "loss_share_session",
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
    precision_min_recall = getattr(config, "PRECISION_MIN_RECALL", 0.0)
    precision_min_alerts = getattr(config, "PRECISION_MIN_ALERTS", 1)
    precision_tie_break = getattr(config, "PRECISION_TIE_BREAK", "recall")  # "recall" or "alerts"
    topk_candidates = getattr(config, "PRECISION_TOPK_CANDIDATES", [3, 5, 10, 20, 50])

    # Time-based split to avoid leakage: oldest 80% train, most recent 20% validation
    df_sorted = df.sort_values("payout_complete_dtm")
    cutoff_idx = int(len(df_sorted) * 0.8)
    train_df = df_sorted.iloc[:cutoff_idx]
    val_df = df_sorted.iloc[cutoff_idx:]
    X_train, y_train = train_df[feature_cols], train_df["label"]
    X_val, y_val = val_df[feature_cols], val_df["label"]

    models = {}

    base_lgb_params = {
        "n_estimators": 200,
        "class_weight": "balanced",
        "random_state": 42,
        "n_jobs": -1,
    }
    default_lgb_grid = [
        {"num_leaves": 31, "learning_rate": 0.05, "min_child_samples": 20, "subsample": 0.8, "colsample_bytree": 0.8},
        {"num_leaves": 25, "learning_rate": 0.05, "min_child_samples": 40, "subsample": 0.8, "colsample_bytree": 0.7},
        {"num_leaves": 20, "learning_rate": 0.03, "min_child_samples": 60, "subsample": 0.8, "colsample_bytree": 0.6},
        {"num_leaves": 15, "learning_rate": 0.03, "min_child_samples": 80, "subsample": 0.8, "colsample_bytree": 0.5},
        {"num_leaves": 10, "learning_rate": 0.02, "min_child_samples": 120, "subsample": 0.8, "colsample_bytree": 0.5},
        # {"num_leaves": 63, "learning_rate": 0.05, "min_child_samples": 20, "subsample": 0.8, "colsample_bytree": 0.8},
        # {"num_leaves": 63, "learning_rate": 0.1, "min_child_samples": 40, "subsample": 0.9, "colsample_bytree": 0.9},
    ]
    lgb_grid = getattr(config, "LGBM_PARAM_GRID", default_lgb_grid)
    for idx, params in enumerate(lgb_grid):
        name = f"LightGBM_grid{idx+1}"
        models[name] = lgb.LGBMClassifier(**base_lgb_params, **params)

    print(f"[trainer] Model configs: {list(models.keys())}", flush=True)

    best_metrics = None
    best_model = None
    best_name = None

    def is_better(candidate, current):
        if candidate is None:
            return False
        if current is None:
            return True
        if candidate["precision"] > current["precision"]:
            return True
        if candidate["precision"] < current["precision"]:
            return False
        # Tie-breaker: prefer higher recall or fewer alerts depending on config
        if precision_tie_break == "alerts":
            if candidate["alerts"] < current["alerts"]:
                return True
            if candidate["alerts"] > current["alerts"]:
                return False
        # Default tie-breaker: recall, then fewer alerts
        if candidate["recall"] > current["recall"]:
            return True
        if candidate["recall"] < current["recall"]:
            return False
        return candidate.get("alerts", 0) < current.get("alerts", 0)

    for idx, (name, model) in enumerate(models.items(), start=1):
        print(f"\n=== {name} ({idx}/{len(models)}) ===", flush=True)
        print(f"Training {name}...", flush=True)
        model.fit(X_train, y_train)
        val_scores = model.predict_proba(X_val)[:, 1]
        thresholds = np.append(np.sort(np.unique(val_scores)), 1.0)
        positives = (y_val == 1).sum()
        best = None
        # Threshold sweep with precision-first guardrails
        for t in thresholds:
            preds = val_scores >= t
            alerts = int(preds.sum())
            if alerts < precision_min_alerts:
                continue
            tp = int(((preds) & (y_val == 1)).sum())
            prec = tp / alerts
            rec = tp / positives if positives > 0 else 0.0
            if rec < precision_min_recall:
                continue
            candidate = {
                "mode": "threshold",
                "threshold": float(t),
                "precision": float(prec),
                "recall": float(rec),
                "alerts": alerts,
                "tp": tp,
            }
            if is_better(candidate, best):
                best = candidate

        # Top-N precision search
        order = np.argsort(-val_scores)
        sorted_scores = val_scores[order]
        sorted_labels = y_val.to_numpy()[order]
        for k in topk_candidates:
            if k > len(sorted_scores):
                continue
            top_labels = sorted_labels[:k]
            tp = int((top_labels == 1).sum())
            prec = tp / k if k > 0 else 0.0
            rec = tp / positives if positives > 0 else 0.0
            thresh_k = float(sorted_scores[k - 1])
            if rec < precision_min_recall or k < precision_min_alerts:
                continue
            candidate = {
                "mode": "topk",
                "k": k,
                "threshold": thresh_k,
                "precision": float(prec),
                "recall": float(rec),
                "alerts": k,
                "tp": tp,
            }
            if is_better(candidate, best):
                best = candidate

        if best is None:
            best = {"mode": "threshold", "threshold": 0.5, "precision": 0.0, "recall": 0.0, "alerts": 0, "tp": 0}
        metrics = {
            "validation_precision": best["precision"],
            "validation_recall": best["recall"],
            "threshold": best["threshold"],
            "positive_rate": float(df_sorted["label"].mean()),
            "val_samples": int(len(y_val)),
            "val_alerts": int(best.get("alerts", 0)),
            "val_true_alerts": int(best.get("tp", 0)),
            "model_type": name,
            "selection_mode": best.get("mode", "threshold"),
            "selection_k": int(best.get("k", 0)),
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
    bets = sessions = None

    if local_data_exists():
        bets, sessions = load_local_data()
        missing_bet_cols = [c for c in ["status"] if c not in bets.columns]
        if missing_bet_cols:
            print(f"[trainer] Local bets cache missing columns {missing_bet_cols}; reloading from ClickHouse...", flush=True)
            bets = sessions = None  # force refetch below
        else:
            # Auto-adjust time window to fit local data
            min_dt = bets["payout_complete_dtm"].min()
            max_dt = bets["payout_complete_dtm"].max()
            print(f"[trainer] Using local data window: {min_dt} to {max_dt}", flush=True)
            start, end = min_dt, max_dt

    if bets is None or sessions is None:
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
