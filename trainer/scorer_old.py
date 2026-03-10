from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import joblib
import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo
from db_conn import get_clickhouse_client

import config

HK_TZ = ZoneInfo(config.HK_TZ)
BASE_DIR = Path(__file__).parent
STATE_DIR = BASE_DIR / "local_state"
STATE_DIR.mkdir(exist_ok=True)
STATE_DB_PATH = STATE_DIR / "state.db"
RETENTION_HOURS = config.SCORER_STATE_RETENTION_HOURS

# ------------------ Model loader ------------------
def load_model_artifacts(model_path: Optional[Path] = None):
    if model_path is None:
        model_path = BASE_DIR / "models" / "walkaway_model.pkl"
    bundle = joblib.load(model_path)
    model = bundle["model"]
    feature_cols = bundle["features"]
    threshold = bundle.get("threshold", 0.5)
    return model, feature_cols, threshold


def fetch_recent_data(start: datetime, end: datetime) -> Tuple[pd.DataFrame, pd.DataFrame]:
    client = get_clickhouse_client()
    params = {"start": start, "end": end}
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
        print(f"[scorer] filtered zero-wager bets: {before}->{len(bets)}")
    sessions = client.query_df(session_query, parameters=params)
    return bets, sessions


# ------------------ State helpers (SQLite) ------------------
def init_state_db():
    STATE_DB_PATH.parent.mkdir(exist_ok=True)
    with sqlite3.connect(STATE_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_stats (
                session_id TEXT PRIMARY KEY,
                bet_count INTEGER NOT NULL,
                sum_wager REAL NOT NULL,
                first_ts TEXT,
                last_ts TEXT,
                player_id TEXT,
                table_id TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                bet_id TEXT PRIMARY KEY,
                ts TEXT,
                bet_ts TEXT,
                player_id TEXT,
                table_id TEXT,
                position_idx REAL,
                visit_start_ts TEXT,
                visit_end_ts TEXT,
                session_count INTEGER,
                bet_count INTEGER,
                visit_avg_bet REAL,
                historical_avg_bet REAL,
                score REAL,
                session_id TEXT,
                loss_streak INTEGER,
                bets_last_5m REAL,
                bets_last_15m REAL,
                bets_last_30m REAL,
                wager_last_10m REAL,
                wager_last_30m REAL,
                cum_bets REAL,
                cum_wager REAL,
                avg_wager_sofar REAL,
                session_duration_min REAL,
                bets_per_minute REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_player ON alerts(player_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_results (
                bet_id TEXT PRIMARY KEY,
                alert_ts TEXT,
                validated_at TEXT,
                player_id TEXT,
                table_id TEXT,
                position_idx REAL,
                session_id TEXT,
                score REAL,
                result INTEGER,
                gap_start TEXT,
                gap_minutes REAL,
                reason TEXT,
                bet_ts TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_validation_alert_ts ON validation_results(alert_ts)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_alerts (
                bet_id TEXT PRIMARY KEY,
                processed_ts TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_last_ts ON session_stats(last_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_player ON session_stats(player_id)")


def _get_last_processed_end(conn: sqlite3.Connection):
    row = conn.execute("SELECT value FROM meta WHERE key='last_processed_end'").fetchone()
    return pd.to_datetime(row[0]) if row else None


def _set_last_processed_end(conn: sqlite3.Connection, dt: datetime):
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('last_processed_end', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (dt.isoformat(),),
    )


def prune_old_state(conn: sqlite3.Connection, now_hk: datetime, retention_hours: int = RETENTION_HOURS):
    cutoff = now_hk - timedelta(hours=retention_hours)
    conn.execute("DELETE FROM session_stats WHERE last_ts < ?", (cutoff.isoformat(),))
    conn.commit()


def _upsert_session(conn: sqlite3.Connection, sid, bet_count, sum_wager, first_ts, last_ts, player_id, table_id):
    conn.execute(
        """
        INSERT INTO session_stats(session_id, bet_count, sum_wager, first_ts, last_ts, player_id, table_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            bet_count = session_stats.bet_count + excluded.bet_count,
            sum_wager = session_stats.sum_wager + excluded.sum_wager,
            first_ts = COALESCE(session_stats.first_ts, excluded.first_ts),
            last_ts = MAX(session_stats.last_ts, excluded.last_ts),
            player_id = COALESCE(session_stats.player_id, excluded.player_id),
            table_id = COALESCE(session_stats.table_id, excluded.table_id),
            updated_at = excluded.updated_at
        """,
        (str(sid), int(bet_count), float(sum_wager), first_ts.isoformat() if first_ts is not None else None, last_ts.isoformat() if last_ts is not None else None, None if pd.isna(player_id) else str(player_id), None if pd.isna(table_id) else str(table_id), datetime.now(HK_TZ).isoformat()),
    )


def update_state_with_new_bets(conn: sqlite3.Connection, bets: pd.DataFrame, window_end: datetime) -> pd.DataFrame:
    last_processed = _get_last_processed_end(conn)
    if last_processed is not None:
        new_bets = bets[bets["payout_complete_dtm"] > last_processed]
    else:
        new_bets = bets

    for sid, group in new_bets.groupby("session_id"):
        if pd.isna(sid):
            continue
        _upsert_session(
            conn,
            sid,
            len(group),
            group["wager"].sum(),
            group["payout_complete_dtm"].min(),
            group["payout_complete_dtm"].max(),
            group.get("player_id", pd.Series([None])).iloc[0],
            group.get("table_id", pd.Series([None])).iloc[0],
        )

    _set_last_processed_end(conn, window_end)
    conn.commit()
    return new_bets


def get_session_totals(conn: sqlite3.Connection, session_id):
    if pd.isna(session_id):
        return 0, 0.0, None, None
    row = conn.execute(
        "SELECT bet_count, sum_wager, first_ts, last_ts FROM session_stats WHERE session_id = ?",
        (str(session_id),),
    ).fetchone()
    if not row:
        return 0, 0.0, None, None
    first_ts = pd.to_datetime(row[2]) if row[2] else None
    last_ts = pd.to_datetime(row[3]) if row[3] else None
    return row[0], row[1], first_ts, last_ts


def get_historical_avg(conn: sqlite3.Connection, player_id) -> float:
    if pd.isna(player_id):
        return 0.0
    row = conn.execute(
        "SELECT SUM(sum_wager), SUM(bet_count) FROM session_stats WHERE player_id = ?",
        (str(player_id),),
    ).fetchone()
    if not row or row[1] is None or row[1] == 0:
        return 0.0
    return float(row[0]) / float(row[1])


def get_session_count(conn: sqlite3.Connection, player_id) -> int:
    if pd.isna(player_id):
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM session_stats WHERE player_id = ?",
        (str(player_id),),
    ).fetchone()
    return int(row[0]) if row else 0


# ------------------ Feature engineering for scoring ------------------
def _ensure_hk(dt_series: pd.Series) -> pd.Series:
    if dt_series.dt.tz is None:
        return dt_series.dt.tz_localize(HK_TZ)
    return dt_series.dt.tz_convert(HK_TZ)


def build_features(bets: pd.DataFrame, sessions: pd.DataFrame, feature_cols) -> pd.DataFrame:
    if bets.empty:
        return pd.DataFrame(columns=feature_cols + ["session_id", "player_id", "table_id", "position_idx", "session_start_dtm", "session_end_dtm", "payout_complete_dtm", "wager"])

    bets_df = bets.copy()
    sessions_df = sessions.copy()

    for col in ["position_idx", "payout_odds", "base_ha", "is_back_bet", "wager", "bet_id", "status"]:
        if col not in bets_df.columns:
            bets_df[col] = 0

    for col in ["session_id", "player_id", "table_id", "bet_id"]:
        bets_df[col] = pd.to_numeric(bets_df.get(col), errors="coerce")
    for col in ["session_id", "player_id", "table_id"]:
        sessions_df[col] = pd.to_numeric(sessions_df.get(col), errors="coerce")

    bets_df = bets_df.dropna(subset=["session_id", "bet_id"])
    sessions_df = sessions_df.dropna(subset=["session_id"])
    bets_df["session_id"] = bets_df["session_id"].astype("Int64")
    bets_df["bet_id"] = bets_df["bet_id"].astype("Int64")
    sessions_df["session_id"] = sessions_df["session_id"].astype("Int64")

    bets_df["payout_complete_dtm"] = pd.to_datetime(bets_df["payout_complete_dtm"])
    sessions_df["session_start_dtm"] = pd.to_datetime(sessions_df["session_start_dtm"])
    sessions_df["session_end_dtm"] = pd.to_datetime(sessions_df["session_end_dtm"])

    bets_df["payout_complete_dtm"] = _ensure_hk(bets_df["payout_complete_dtm"])
    sessions_df["session_start_dtm"] = _ensure_hk(sessions_df["session_start_dtm"])
    sessions_df["session_end_dtm"] = _ensure_hk(sessions_df["session_end_dtm"])

    sessions_df = sessions_df.sort_values(["session_id", "session_end_dtm"])
    sessions_df = sessions_df.drop_duplicates(subset=["session_id"], keep="last")

    # Merge only time fields to avoid duplicate player_id/table_id suffixes
    merged = bets_df.merge(
        sessions_df[["session_id", "session_start_dtm", "session_end_dtm"]],
        on="session_id",
        how="left",
        validate="many_to_one",
    )

    merged = merged.sort_values(["session_id", "payout_complete_dtm", "bet_id"])

    merged["wager"] = pd.to_numeric(merged["wager"], errors="coerce").fillna(0)
    merged["payout_odds"] = pd.to_numeric(merged["payout_odds"], errors="coerce").fillna(0)
    merged["base_ha"] = pd.to_numeric(merged["base_ha"], errors="coerce").fillna(0)
    merged["is_back_bet"] = pd.to_numeric(merged["is_back_bet"], errors="coerce").fillna(0)
    merged["position_idx"] = pd.to_numeric(merged["position_idx"], errors="coerce").fillna(0)
    merged["status"] = merged["status"].astype(str)

    merged["session_start_dtm"] = merged["session_start_dtm"].fillna(merged["payout_complete_dtm"])
    merged["session_end_dtm"] = merged["session_end_dtm"].fillna(merged["payout_complete_dtm"])

    merged["session_duration_min"] = (
        merged["payout_complete_dtm"] - merged["session_start_dtm"]
    ).dt.total_seconds() / 60.0
    merged["minutes_to_session_end"] = (
        merged["session_end_dtm"] - merged["payout_complete_dtm"]
    ).dt.total_seconds() / 60.0
    merged["minutes_since_session_start"] = (
        merged["payout_complete_dtm"] - merged["session_start_dtm"]
    ).dt.total_seconds() / 60.0

    merged["cum_bets"] = merged.groupby("session_id").cumcount() + 1
    merged["cum_wager"] = merged.groupby("session_id")["wager"].cumsum()
    merged["avg_wager_sofar"] = merged["cum_wager"] / merged["cum_bets"]
    merged["bets_per_minute"] = merged["cum_bets"] / (merged["session_duration_min"] + 1e-3)

    # Faster per-session rolling counts/sums using vectorized groupby.rolling
    # Sort and reset index so downstream rolling results align cleanly
    # Use a timezone-naive UTC timestamp for rolling to avoid tz overhead
    merged["payout_ts"] = merged["payout_complete_dtm"].dt.tz_convert("UTC").dt.tz_localize(None)
    merged["payout_ts_ns"] = merged["payout_ts"].astype("int64")
    merged = merged.sort_values(["session_id", "payout_ts_ns"]).reset_index(drop=True)

    def _session_windows(group: pd.DataFrame) -> pd.DataFrame:
        ts = group["payout_ts_ns"].to_numpy()
        wager = group["wager"].to_numpy()
        n = len(group)
        cumsum = np.concatenate([[0], np.cumsum(wager)])
        out = {}
        for window in (5, 15, 30):
            win_ns = window * 60 * 1e9
            start = 0
            counts = np.empty(n, dtype=np.float64)
            for i, t in enumerate(ts):
                while t - ts[start] > win_ns:
                    start += 1
                counts[i] = i - start + 1
            out[f"bets_last_{window}m"] = counts
        for window in (10, 30):
            win_ns = window * 60 * 1e9
            start = 0
            sums = np.empty(n, dtype=np.float64)
            for i, t in enumerate(ts):
                while t - ts[start] > win_ns:
                    start += 1
                sums[i] = cumsum[i + 1] - cumsum[start]
            out[f"wager_last_{window}m"] = sums
        return pd.DataFrame(out, index=group.index)

    windows_df = merged.groupby("session_id", group_keys=False).apply(_session_windows, include_groups=False)
    merged.update(windows_df)

    # Ensure rolling feature columns always exist
    for col in [
        "bets_last_5m",
        "bets_last_15m",
        "bets_last_30m",
        "wager_last_10m",
        "wager_last_30m",
    ]:
        if col not in merged.columns:
            merged[col] = 0.0
        else:
            merged[col] = merged[col].fillna(0.0)

    # Table headcount at bet time (number of concurrent sessions on the same table)
    try:
        sess_occ = sessions_df.dropna(subset=["table_id"]).copy()
        sess_occ["session_start_dtm"] = pd.to_datetime(sess_occ["session_start_dtm"], errors="coerce")
        sess_occ["session_end_dtm"] = pd.to_datetime(sess_occ["session_end_dtm"], errors="coerce")
        sess_occ["session_end_dtm"] = sess_occ["session_end_dtm"].fillna(sess_occ["session_start_dtm"])
        sess_occ["table_id_str"] = sess_occ["table_id"].astype(str)
        sess_occ = sess_occ.dropna(subset=["session_start_dtm", "session_end_dtm"]).copy()

        def to_utc_ns(series: pd.Series) -> pd.Series:
            if series.dt.tz is None:
                series = series.dt.tz_localize("UTC", nonexistent="NaT", ambiguous="NaT")
            else:
                series = series.dt.tz_convert("UTC")
            return series.astype("int64")

        sess_occ["ts_start_ns"] = to_utc_ns(sess_occ["session_start_dtm"])
        sess_occ["ts_end_ns"] = to_utc_ns(sess_occ["session_end_dtm"])

        merged["table_id_str"] = merged["table_id"].astype(str)
        merged["payout_ns"] = to_utc_ns(merged["payout_complete_dtm"])
        merged["table_hc"] = 0

        for tid, ev in sess_occ.groupby("table_id_str", sort=False):
            bet_idx = merged.index[merged["table_id_str"] == tid]
            if bet_idx.empty:
                continue
            bet_ns = merged.loc[bet_idx, "payout_ns"].to_numpy()
            ev_ns = np.concatenate([ev["ts_start_ns"].to_numpy(), ev["ts_end_ns"].to_numpy()])
            deltas = np.concatenate([np.ones(len(ev), dtype=int), -np.ones(len(ev), dtype=int)])
            order = np.argsort(ev_ns, kind="mergesort")
            ev_ns_sorted = ev_ns[order]
            occ = np.cumsum(deltas[order])
            pos = ev_ns_sorted.searchsorted(bet_ns, side="right") - 1
            vals = np.where(pos >= 0, occ[pos], 0)
            merged.loc[bet_idx, "table_hc"] = vals
        merged["table_hc"] = merged["table_hc"].fillna(0)
    except Exception as exc:
        print(f"[scorer] table headcount computation failed; defaulting to 0: {exc}")
        merged["table_hc"] = 0

    # Time-of-day cyclic encoding
    merged["minutes_into_day"] = merged["payout_complete_dtm"].dt.hour * 60 + merged["payout_complete_dtm"].dt.minute
    merged["time_of_day_sin"] = np.sin(2 * np.pi * merged["minutes_into_day"] / 1440)
    merged["time_of_day_cos"] = np.cos(2 * np.pi * merged["minutes_into_day"] / 1440)

    # Loss streak per session (consecutive LOSE bets up to current bet)
    merged = merged.sort_values(["session_id", "payout_complete_dtm", "bet_id"])

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

    merged["loss_streak"] = (
        merged.groupby("session_id", group_keys=False).apply(_loss_streak, include_groups=False)
    )

    merged[feature_cols] = merged[feature_cols].fillna(0)
    return merged


# ------------------ Alert helpers ------------------
def load_alert_history(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute("SELECT bet_id FROM alerts").fetchall()
        if rows:
            return {str(r[0]) for r in rows if r[0] is not None}
    except Exception:
        pass
    return set()


def refresh_alert_history(alert_history: set, now_hk: datetime, conn: sqlite3.Connection) -> set:
    retention_days = getattr(config, "SCORER_ALERT_RETENTION_DAYS", None)
    if retention_days is not None and retention_days > 0:
        cutoff = now_hk - timedelta(days=retention_days)
        conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff.isoformat(),))
        conn.commit()
    try:
        rows = conn.execute("SELECT bet_id FROM alerts").fetchall()
        alert_history.clear()
        alert_history.update({str(r[0]) for r in rows if r[0] is not None})
    except Exception:
        alert_history.clear()
    return alert_history


def append_alerts(conn: sqlite3.Connection, alerts_df: pd.DataFrame):
    # DB write (upsert)
    rows = [
        (
            None if pd.isna(r.bet_id) else str(r.bet_id),
            pd.to_datetime(r.ts).isoformat() if pd.notna(r.ts) else None,
            pd.to_datetime(r.bet_ts).isoformat() if pd.notna(r.bet_ts) else None,
            None if pd.isna(r.player_id) else str(int(r.player_id)),
            None if pd.isna(r.table_id) else str(r.table_id),
            None if pd.isna(r.position_idx) else float(r.position_idx),
            pd.to_datetime(r.visit_start_ts).isoformat() if pd.notna(r.visit_start_ts) else None,
            pd.to_datetime(r.visit_end_ts).isoformat() if pd.notna(r.visit_end_ts) else None,
            None if pd.isna(r.session_count) else int(r.session_count),
            None if pd.isna(r.bet_count) else int(r.bet_count),
            None if pd.isna(r.visit_avg_bet) else float(r.visit_avg_bet),
            None if pd.isna(r.historical_avg_bet) else float(r.historical_avg_bet),
            None if pd.isna(r.score) else float(r.score),
            None if pd.isna(r.session_id) else str(int(r.session_id)),
            None if pd.isna(r.loss_streak) else int(r.loss_streak),
            None if pd.isna(r.bets_last_5m) else float(r.bets_last_5m),
            None if pd.isna(r.bets_last_15m) else float(r.bets_last_15m),
            None if pd.isna(r.bets_last_30m) else float(r.bets_last_30m),
            None if pd.isna(r.wager_last_10m) else float(r.wager_last_10m),
            None if pd.isna(r.wager_last_30m) else float(r.wager_last_30m),
            None if pd.isna(r.cum_bets) else float(r.cum_bets),
            None if pd.isna(r.cum_wager) else float(r.cum_wager),
            None if pd.isna(r.avg_wager_sofar) else float(r.avg_wager_sofar),
            None if pd.isna(r.session_duration_min) else float(r.session_duration_min),
            None if pd.isna(r.bets_per_minute) else float(r.bets_per_minute),
        )
        for r in alerts_df.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO alerts(
            bet_id, ts, bet_ts, player_id, table_id, position_idx, visit_start_ts, visit_end_ts,
            session_count, bet_count, visit_avg_bet, historical_avg_bet, score, session_id,
            loss_streak, bets_last_5m, bets_last_15m, bets_last_30m, wager_last_10m, wager_last_30m,
            cum_bets, cum_wager, avg_wager_sofar, session_duration_min, bets_per_minute
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bet_id) DO UPDATE SET
            ts=excluded.ts,
            bet_ts=excluded.bet_ts,
            player_id=excluded.player_id,
            table_id=excluded.table_id,
            position_idx=excluded.position_idx,
            visit_start_ts=excluded.visit_start_ts,
            visit_end_ts=excluded.visit_end_ts,
            session_count=excluded.session_count,
            bet_count=excluded.bet_count,
            visit_avg_bet=excluded.visit_avg_bet,
            historical_avg_bet=excluded.historical_avg_bet,
            score=excluded.score,
            session_id=excluded.session_id,
            loss_streak=excluded.loss_streak,
            bets_last_5m=excluded.bets_last_5m,
            bets_last_15m=excluded.bets_last_15m,
            bets_last_30m=excluded.bets_last_30m,
            wager_last_10m=excluded.wager_last_10m,
            wager_last_30m=excluded.wager_last_30m,
            cum_bets=excluded.cum_bets,
            cum_wager=excluded.cum_wager,
            avg_wager_sofar=excluded.avg_wager_sofar,
            session_duration_min=excluded.session_duration_min,
            bets_per_minute=excluded.bets_per_minute
        """,
        rows,
    )


# ------------------ Main scoring loop ------------------
def score_once(model, feature_cols, threshold, lookback_hours: int, alert_history: set, conn: sqlite3.Connection, retention_hours: int = RETENTION_HOURS):
    now_hk = datetime.now(HK_TZ)
    refresh_alert_history(alert_history, now_hk, conn)
    start = now_hk - timedelta(hours=lookback_hours)
    print(f"[scorer] Window: {start.isoformat()} to {now_hk.isoformat()}")
    bets, sessions = fetch_recent_data(start, now_hk)
    print(f"[scorer] Fetched bets: {len(bets):,}, sessions: {len(sessions):,}")
    if bets.empty:
        print("[scorer] No bets in window; sleeping")
        return

    prune_old_state(conn, now_hk, retention_hours)

    # Update state with truly new bets (beyond last processed) so session-level totals persist
    new_bets = update_state_with_new_bets(conn, bets, now_hk)
    print(f"[scorer] New bets since last tick: {len(new_bets):,}")
    if new_bets.empty:
        print("[scorer] No new bets to score; sleeping")
        return

    # Engineer features on the full window to preserve rolling history, then filter to the new bets for scoring
    features_all = build_features(bets, sessions, feature_cols)
    print(f"[scorer] Engineered rows (full window): {len(features_all):,}")

    new_ids = set(new_bets["bet_id"].astype(str))
    features_df = features_all[features_all["bet_id"].astype(str).isin(new_ids)].copy()
    print(f"[scorer] Rows to score (new bets): {len(features_df):,}")
    if features_df.empty:
        print("[scorer] No usable rows after feature engineering; sleeping")
        return

    X = features_df[feature_cols]
    scores = model.predict_proba(X)[:, 1]
    features_df["score"] = scores

    alert_candidates = features_df[features_df["score"] >= threshold].copy()
    print(f"[scorer] Above-threshold rows: {len(alert_candidates):,}")
    if alert_candidates.empty:
        print("[scorer] No alerts this cycle")
        return

    # Precompute per-session wager stats from the current window to avoid 0 avg_bet in alerts when state is sparse
    session_agg = (
        features_df.groupby("session_id")["wager"]
        .agg(bet_count="count", sum_wager="sum")
        .to_dict("index")
    )
    unique_sids = [sid for sid in alert_candidates["session_id"].dropna().unique().tolist()]
    session_totals_cache = {sid: get_session_totals(conn, sid) for sid in unique_sids}

    def _session_totals(sid):
        return session_totals_cache.get(sid, (0, 0.0, None, None))

    def _fallback_avg(sid):
        agg = session_agg.get(sid)
        if not agg:
            return 0.0
        bc = int(agg.get("bet_count", 0) or 0)
        sw = float(agg.get("sum_wager", 0.0) or 0.0)
        return (sw / bc) if bc > 0 else 0.0

    alert_candidates["ts"] = now_hk
    # Session-level persistent stats
    alert_candidates["bet_count"] = alert_candidates["session_id"].apply(
        lambda sid: max(_session_totals(sid)[0], int(session_agg.get(sid, {}).get("bet_count", 0) or 0))
    )
    alert_candidates["visit_avg_bet"] = alert_candidates["session_id"].apply(
        lambda sid: (
            lambda bc, sw: (sw / bc) if bc > 0 else _fallback_avg(sid)
        )(*_session_totals(sid)[:2])
    )
    alert_candidates["visit_start_ts"] = alert_candidates["session_id"].apply(lambda sid: _session_totals(sid)[2])
    alert_candidates["visit_end_ts"] = alert_candidates["session_id"].apply(lambda sid: _session_totals(sid)[3])
    # Player-level session count to date
    alert_candidates["session_count"] = alert_candidates["player_id"].apply(
        lambda pid: get_session_count(conn, pid)
    )
    alert_candidates["historical_avg_bet"] = alert_candidates["player_id"].apply(lambda pid: get_historical_avg(conn, pid))

    alert_candidates["bet_ts"] = alert_candidates["payout_complete_dtm"]
    alert_candidates = alert_candidates[
        [
            "ts",
            "bet_ts",
            "player_id",
            "table_id",
            "position_idx",
            "visit_start_ts",
            "visit_end_ts",
            "session_count",
            "bet_count",
            "visit_avg_bet",
            "historical_avg_bet",
            "score",
            "bet_id",
            "session_id",
            "loss_streak",
            "bets_last_5m",
            "bets_last_15m",
            "bets_last_30m",
            "wager_last_10m",
            "wager_last_30m",
            "cum_bets",
            "cum_wager",
            "avg_wager_sofar",
            "session_duration_min",
            "bets_per_minute",
        ]
    ]

    if alert_history:
        alert_candidates["bet_id_str"] = alert_candidates["bet_id"].astype(str)
        before = len(alert_candidates)
        alert_candidates = alert_candidates[~alert_candidates["bet_id_str"].isin(alert_history)]
        suppressed = before - len(alert_candidates)
        if suppressed > 0:
            print(f"[scorer] Suppressed {suppressed} duplicate alerts")

    if alert_candidates.empty:
        print("[scorer] Alerts suppressed (already sent)")
        return

    append_alerts(conn, alert_candidates)
    alert_history.update(alert_candidates["bet_id"].astype(str).tolist())
    print(f"[scorer] Emitted {len(alert_candidates)} alerts")

    conn.commit()
    return


def main():
    parser = argparse.ArgumentParser(description="Near real-time scorer for walkaway alerts")
    parser.add_argument("--interval", type=int, default=45, help="Polling interval in seconds (includes run time)")
    parser.add_argument("--lookback-hours", type=int, default=8, help="Hours of history to pull each cycle")
    parser.add_argument("--once", action="store_true", help="Run a single scoring cycle and exit")
    args = parser.parse_args()

    model, feature_cols, threshold = load_model_artifacts()
    print(f"[scorer] Loaded model with threshold {threshold}")

    conn = sqlite3.connect(STATE_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    init_state_db()
    alert_history = load_alert_history(conn)

    while True:
        t_start = time.time()
        try:
            score_once(model, feature_cols, threshold, args.lookback_hours, alert_history, conn, RETENTION_HOURS)
        except Exception as exc:
            print(f"[scorer] ERROR: {exc}")
        elapsed = time.time() - t_start
        sleep_for = max(0, args.interval - elapsed)
        if args.once:
            break
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
