from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from bisect import bisect_left, bisect_right

import pandas as pd
# zoneinfo is available in Python 3.9+. If not present, try backports.zoneinfo for older Pythons.
# If both are unavailable, fall back to python-dateutil's gettz so the validator can still run
# without requiring backports (useful in constrained environments).
try:
    from zoneinfo import ZoneInfo
except Exception:
    try:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    except Exception:
        try:
            from dateutil.tz import gettz as ZoneInfo  # type: ignore
        except Exception:
            raise ImportError(
                "ZoneInfo not available: install Python 3.9+ or backports.zoneinfo "
                "or ensure python-dateutil is installed"
            )

import config
from db_conn import get_clickhouse_client

HK_TZ = ZoneInfo(config.HK_TZ)
BASE_DIR = Path(__file__).parent
STATE_DB_PATH = BASE_DIR / "local_state" / "state.db"
OUT_DIR = BASE_DIR / "out_validator"
OUT_DIR.mkdir(exist_ok=True)
RESULTS_PATH = OUT_DIR / "validation_results.csv"  # legacy only (read-backfill); DB is source of truth

IGNORED_REASONS = {"gap_started_before_alert", "missing_player_id"}

VALIDATION_COLUMNS = [
    "alert_ts",
    "validated_at",
    "player_id",
    "table_id",
    "position_idx",
    "session_id",
    "bet_id",
    "score",
    "result",
    "gap_start",
    "gap_minutes",
    "reason",
    "bet_ts",
]


def fetch_bets_for_players(player_ids: List[int], start: datetime, end: datetime) -> Dict[int, List[datetime]]:
    if not player_ids:
        return {}
    client = get_clickhouse_client()
    params = {"players": tuple(player_ids), "start": start, "end": end}
    query = f"""
        SELECT player_id, payout_complete_dtm
        FROM {config.SOURCE_DB}.{config.TBET}
        WHERE player_id IN %(players)s
          AND payout_complete_dtm >= %(start)s
          AND payout_complete_dtm <= %(end)s
                    AND wager > 0
        ORDER BY player_id, payout_complete_dtm
    """
    df = client.query_df(query, parameters=params)
    if df.empty:
        return {}
    # Ensure tz-aware in HK
    df["payout_complete_dtm"] = pd.to_datetime(df["payout_complete_dtm"])
    if df["payout_complete_dtm"].dt.tz is None:
        df["payout_complete_dtm"] = df["payout_complete_dtm"].dt.tz_localize(HK_TZ)
    else:
        df["payout_complete_dtm"] = df["payout_complete_dtm"].dt.tz_convert(HK_TZ)

    cache: Dict[int, List[datetime]] = {}
    for pid, grp in df.groupby("player_id"):
        cache[int(pid)] = list(grp["payout_complete_dtm"].sort_values())
    return cache


def fetch_sessions_for_players(player_ids: List[int], start: datetime, end: datetime) -> Dict[int, List[Dict]]:
    if not player_ids:
        return {}
    client = get_clickhouse_client()
    params = {"players": tuple(player_ids), "start": start, "end": end}
    query = f"""
        SELECT player_id, session_id, session_start_dtm, session_end_dtm
        FROM {config.SOURCE_DB}.{config.TSESSION}
        WHERE player_id IN %(players)s
          AND session_start_dtm >= %(start)s - INTERVAL 1 DAY
          AND session_start_dtm <= %(end)s + INTERVAL 1 DAY
        ORDER BY player_id, session_start_dtm, session_end_dtm
    """
    df = client.query_df(query, parameters=params)
    if df.empty:
        return {}
    for col in ["session_start_dtm", "session_end_dtm"]:
        df[col] = pd.to_datetime(df[col])
        if df[col].dt.tz is None:
            df[col] = df[col].dt.tz_localize(HK_TZ)
        else:
            df[col] = df[col].dt.tz_convert(HK_TZ)

    cache: Dict[int, List[Dict]] = {}
    for pid, grp in df.groupby("player_id"):
        grp_sorted = grp.sort_values(["session_start_dtm", "session_end_dtm"])
        sessions = []
        starts = grp_sorted["session_start_dtm"].to_list()
        ends = grp_sorted["session_end_dtm"].to_list()
        ids = grp_sorted["session_id"].to_list()
        for i, (sid, st, en) in enumerate(zip(ids, starts, ends)):
            next_start = starts[i + 1] if i + 1 < len(starts) else None
            sessions.append(
                {
                    "session_id": int(sid) if pd.notna(sid) else None,
                    "start": st,
                    "end": en,
                    "next_start": next_start,
                }
            )
        cache[int(pid)] = sessions
    return cache


# ------------------ State helpers (SQLite) ------------------
def get_db_conn() -> sqlite3.Connection:
    STATE_DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_alerts (
            bet_id TEXT PRIMARY KEY,
            processed_ts TEXT
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_val_alert_ts ON validation_results(alert_ts)")
    return conn


def load_processed(conn: sqlite3.Connection) -> set:
    try:
        rows = conn.execute("SELECT bet_id FROM processed_alerts").fetchall()
        return {r[0] for r in rows if r[0] is not None}
    except Exception:
        return set()


def mark_processed(conn: sqlite3.Connection, bet_ids: List) -> None:
    if not bet_ids:
        return
    ts = datetime.now(HK_TZ).isoformat()
    rows = [(str(bid), ts) for bid in bet_ids if pd.notna(bid)]
    conn.executemany(
        """
        INSERT INTO processed_alerts(bet_id, processed_ts)
        VALUES (?, ?)
        ON CONFLICT(bet_id) DO UPDATE SET processed_ts=excluded.processed_ts
        """,
        rows,
    )
    conn.commit()


def prune_validator_retention(conn: sqlite3.Connection, now_hk: datetime) -> None:
    days = getattr(config, "VALIDATION_RESULTS_RETENTION_DAYS", None)
    if days is None or days <= 0:
        return
    cutoff = now_hk - timedelta(days=days)
    conn.execute("DELETE FROM validation_results WHERE alert_ts < ?", (cutoff.isoformat(),))
    conn.execute("DELETE FROM processed_alerts WHERE processed_ts < ?", (cutoff.isoformat(),))
    conn.commit()


def load_existing_results(conn: sqlite3.Connection) -> Dict:
    existing_results: Dict[str, Dict] = {}
    try:
        df_db = pd.read_sql_query("SELECT * FROM validation_results", conn)
        for _, r in df_db.iterrows():
            key = str(r["bet_id"]) if pd.notnull(r["bet_id"]) else f"{r['player_id']}_{r['alert_ts']}"
            existing_results[key] = r.to_dict()
    except Exception:
        pass

    # Legacy CSV fallback (only add entries not already in DB)
    if RESULTS_PATH.exists():
        try:
            df_old = pd.read_csv(RESULTS_PATH)
            for _, r in df_old.iterrows():
                key = str(r["bet_id"]) if pd.notnull(r["bet_id"]) else f"{r['player_id']}_{r['alert_ts']}"
                if key not in existing_results:
                    existing_results[key] = r.to_dict()
        except Exception:
            pass
    return existing_results


def save_validation_results(conn: sqlite3.Connection, final_df: pd.DataFrame) -> None:
    if final_df.empty:
        return
    rows = [
        (
            None if pd.isna(r.bet_id) else str(r.bet_id),
            r.alert_ts,
            r.validated_at,
            None if pd.isna(r.player_id) else int(r.player_id),
            r.table_id,
            r.position_idx,
            None if pd.isna(r.session_id) else str(int(r.session_id)),
            r.score,
            None if pd.isna(r.result) else int(bool(r.result)),
            r.gap_start,
            r.gap_minutes,
            r.reason,
            r.bet_ts,
        )
        for r in final_df.itertuples(index=False)
    ]
    conn.executemany(
        """
        INSERT INTO validation_results(
            bet_id, alert_ts, validated_at, player_id, table_id, position_idx, session_id,
            score, result, gap_start, gap_minutes, reason, bet_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bet_id) DO UPDATE SET
            alert_ts=excluded.alert_ts,
            validated_at=excluded.validated_at,
            player_id=excluded.player_id,
            table_id=excluded.table_id,
            position_idx=excluded.position_idx,
            session_id=excluded.session_id,
            score=excluded.score,
            result=excluded.result,
            gap_start=excluded.gap_start,
            gap_minutes=excluded.gap_minutes,
            reason=excluded.reason,
            bet_ts=excluded.bet_ts
        """,
        rows,
    )
    conn.commit()


# ------------------ Validation logic ------------------
def parse_alerts(conn: sqlite3.Connection) -> pd.DataFrame:
    retention_days = getattr(config, "VALIDATOR_ALERT_RETENTION_DAYS", None)
    try:
        df = pd.read_sql_query("SELECT * FROM alerts", conn)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
            df["bet_ts"] = pd.to_datetime(df.get("bet_ts"), errors="coerce")
            df["ts"] = df["ts"].dt.tz_localize("UTC").dt.tz_convert(HK_TZ) if df["ts"].dt.tz is None else df["ts"].dt.tz_convert(HK_TZ)
            if "bet_ts" in df.columns:
                df["bet_ts"] = df["bet_ts"].dt.tz_localize("UTC").dt.tz_convert(HK_TZ) if df["bet_ts"].dt.tz is None else df["bet_ts"].dt.tz_convert(HK_TZ)
            if retention_days is not None and retention_days > 0:
                cutoff = datetime.now(HK_TZ) - timedelta(days=retention_days)
                df = df[df["ts"] >= cutoff]
            return df
    except Exception:
        pass

    return pd.DataFrame()


def find_gap_within_window(alert_ts: datetime, bet_times: List[datetime], base_start: Optional[datetime] = None) -> Tuple[bool, Optional[datetime], float]:
    """Return (is_true, gap_start, gap_minutes). Gap must start within 15m of alert and last >=30m."""
    horizon_end = alert_ts + timedelta(minutes=45)
    bet_times = [t for t in bet_times if t >= alert_ts and t <= horizon_end]
    bet_times.sort()

    current_start = base_start or alert_ts
    for bt in bet_times:
        gap_minutes = (bt - current_start).total_seconds() / 60.0
        if gap_minutes >= 30 and (current_start - alert_ts).total_seconds() / 60.0 <= 15:
            return True, current_start, gap_minutes
        current_start = bt
    # Tail gap to horizon end
    gap_minutes = (horizon_end - current_start).total_seconds() / 60.0
    if gap_minutes >= 30 and (current_start - alert_ts).total_seconds() / 60.0 <= 15:
        return True, current_start, gap_minutes
    return False, None, 0.0


def validate_alert_row(row: pd.Series, bet_cache: Dict[int, List[datetime]], session_cache: Dict[int, List[Dict]], force_finalize: bool = False) -> Dict:
    score_ts = pd.to_datetime(row["ts"]) 
    if score_ts.tzinfo is None:
        score_ts = score_ts.tz_localize(HK_TZ)
    else:
        score_ts = score_ts.tz_convert(HK_TZ)
    bet_ts = row.get("bet_ts")
    if pd.isna(bet_ts):
        bet_ts = score_ts
    else:
        bet_ts = pd.to_datetime(bet_ts)
        if bet_ts.tzinfo is None:
            bet_ts = bet_ts.tz_localize(HK_TZ)
        else:
            bet_ts = bet_ts.tz_convert(HK_TZ)
    player_id = row.get("player_id")
    bet_id = row.get("bet_id")

    now_hk = datetime.now(HK_TZ)

    # Template for result with all columns
    res_base = {col: None for col in VALIDATION_COLUMNS}
    res_base.update(
        {
            "alert_ts": score_ts.isoformat(),
            "validated_at": now_hk.isoformat(),
            "bet_ts": bet_ts.isoformat(),
            "bet_id": bet_id,
            "score": row.get("score"),
            "player_id": int(player_id) if pd.notna(player_id) else None,
            "table_id": row.get("table_id"),
            "position_idx": row.get("position_idx"),
            "session_id": row.get("session_id"),
        }
    )

    if pd.isna(player_id):
        res_base.update({"result": False, "reason": "missing_player_id"})
        return res_base

    # Only validate after the bet has aged past the alert horizon plus a small buffer
    freshness_buffer_min = getattr(config, "VALIDATOR_FRESHNESS_BUFFER_MINUTES", 2)
    wait_minutes = 45 + max(0, freshness_buffer_min)
    if bet_ts > now_hk - timedelta(minutes=wait_minutes):
        return {"result": None}  # too recent; special key to signal skip

    # Use cached bets for this player
    bet_list = bet_cache.get(int(player_id), [])
    # Find last bet before bet_ts
    idx = bisect_left(bet_list, bet_ts)
    last_bet_before = bet_list[idx - 1] if idx > 0 else None
    if last_bet_before is not None and (bet_ts - last_bet_before) > timedelta(minutes=15):
        res_base.update(
            {
                "result": False,
                "gap_start": last_bet_before.isoformat(),
                "gap_minutes": (bet_ts - last_bet_before).total_seconds() / 60.0,
                "reason": "gap_started_before_alert",
            }
        )
        return res_base

    base_start = last_bet_before or bet_ts

    # Bets within 45-minute horizon after bet_ts
    horizon_end = bet_ts + timedelta(minutes=45)
    right_idx = bisect_right(bet_list, horizon_end)
    bet_times = bet_list[idx:right_idx]

    # Session-based check (aligned with trainer/scorer)
    sessions = session_cache.get(int(player_id), [])
    session_id = row.get("session_id")
    matched_session = None
    if pd.notna(session_id):
        for sess in sessions:
            if sess.get("session_id") == int(session_id):
                matched_session = sess
                break
    if matched_session is None:
        # fallback: find session containing bet_ts
        for sess in sessions:
            if sess["start"] <= bet_ts <= sess["end"]:
                matched_session = sess
                break

    if matched_session:
        session_end = matched_session["end"]
        next_start = matched_session.get("next_start")
        gap_to_next = (next_start - session_end).total_seconds() / 60.0 if next_start is not None else 1e9
        minutes_to_end = (session_end - bet_ts).total_seconds() / 60.0
        
        # Exact match within 15 min window -> candidate MATCH
        # Per policy, we treat this as a candidate and defer finalization
        # until the extended wait window passes (to allow late arrivals to show up).
        if gap_to_next >= 30 and 0 <= minutes_to_end <= 15:
            res_base.update(
                {
                    "result": None,  # signal to re-check later
                    "gap_start": session_end.isoformat(),
                    "gap_minutes": gap_to_next,
                    "reason": "PENDING",
                }
            )
            # do not return yet; let the later logic decide after the extended wait
        
        # Eventual walkaway (merged as MISS per requirement)
        elif gap_to_next >= 30 and minutes_to_end > 15:
            res_base.update(
                {
                    "result": False,
                    "gap_start": session_end.isoformat(),
                    "gap_minutes": gap_to_next,
                    "reason": "MISS",
                }
            )
            return res_base

    # Fallback to bet-gap check
    is_true, gap_start, gap_minutes = find_gap_within_window(bet_ts, bet_times, base_start=base_start)
    
    # If a gap was found via bets, we verify if it was within 15m or late (merged to MISS)
    if is_true:
        minutes_to_gap = (gap_start - bet_ts).total_seconds() / 60.0
        # A detected gap is a candidate MATCH, but per policy we allow a short
        # extended wait window for late-arriving data (arrivals with timestamps
        # in the (15m,45m] interval) before finalizing MATCH.
        res_base.update({
            "result": None,
            "gap_start": gap_start.isoformat(),
            "gap_minutes": gap_minutes,
            "reason": "PENDING"
        })
        # If we're past the extended wait window, finalize now by checking whether any
        # late arrival appeared whose timestamp falls within the 15-45m window.
        extended_wait = getattr(config, 'VALIDATOR_EXTENDED_WAIT_MINUTES', 15)
        late_threshold = bet_ts + timedelta(minutes=15)
        horizon_end = bet_ts + timedelta(minutes=45)
        extended_end = bet_ts + timedelta(minutes=45 + extended_wait)

        if now_hk >= extended_end or force_finalize:
            any_late_bet_in_window = any((bt > late_threshold and bt <= horizon_end) for bt in bet_list)
            any_late_session_in_window = any(
                (sess.get("start") is not None and sess["start"] > late_threshold and sess["start"] <= horizon_end)
                for sess in sessions
            )
            if any_late_bet_in_window or any_late_session_in_window:
                res_base.update({
                    "result": False,
                    "gap_start": gap_start.isoformat(),
                    "gap_minutes": gap_minutes,
                    "reason": "MISS"
                })
                print(f"[validator] Finalizing candidate as MISS (late arrival in 15-45m window or forced) player={player_id} bet_id={bet_id}")
            else:
                res_base.update({
                    "result": True,
                    "gap_start": gap_start.isoformat(),
                    "gap_minutes": gap_minutes,
                    "reason": "MATCH"
                })
                print(f"[validator] Finalizing candidate as MATCH (no late arrivals in 15-45m window or forced) player={player_id} bet_id={bet_id}")
    else:
        # No gap found within the horizon.
        # Policy:
        #  - If any bet/session start exists after bet_ts + 15m and within the 45m horizon,
        #    we can immediately conclude MISS (final at horizon).
        #  - Otherwise, if VALIDATOR_FINALIZE_ON_HORIZON is enabled, wait an extra
        #    VALIDATOR_EXTENDED_WAIT_MINUTES before finalizing; during this period we
        #    return a special {'result': None} to indicate re-check later.
        extended_wait = getattr(config, 'VALIDATOR_EXTENDED_WAIT_MINUTES', 15)
        late_threshold = bet_ts + timedelta(minutes=15)
        horizon_end = bet_ts + timedelta(minutes=45)
        extended_end = bet_ts + timedelta(minutes=45 + extended_wait)

        # Check for any bets after the 15m threshold up to the 45m horizon -> immediate MISS
        any_late_bet_within_horizon = any((bt > late_threshold and bt <= horizon_end) for bt in bet_list)
        # Check for any session start after the 15m threshold up to the 45m horizon -> immediate MISS
        any_late_session_within_horizon = any(
            (sess.get("start") is not None and sess["start"] > late_threshold and sess["start"] <= horizon_end)
            for sess in sessions
        )

        if any_late_bet_within_horizon or any_late_session_within_horizon:
            res_base.update({
                "result": False,
                "gap_start": None,
                "gap_minutes": 0,
                "reason": "MISS"
            })
            print(f"[validator] Finalizing alert as MISS (evidence within 45m) player={player_id} bet_id={bet_id}")
        else:
            if getattr(config, 'VALIDATOR_FINALIZE_ON_HORIZON', False):
                # Still within extended wait window -> skip (to be re-checked later)
                if now_hk < extended_end and not force_finalize:
                    return {"result": None}

                # Either extended window passed or force_finalize requested; check for any late arrivals whose timestamps
                # fall within the 15-45m window after bet_ts (arrivals beyond 45m do not change verdict).
                any_late_bet_in_extended = any((bt > late_threshold and bt <= horizon_end) for bt in bet_list)
                any_late_session_in_extended = any(
                    (sess.get("start") is not None and sess["start"] > late_threshold and sess["start"] <= horizon_end)
                    for sess in sessions
                )

                if any_late_bet_in_extended or any_late_session_in_extended:
                    res_base.update({
                        "result": False,
                        "gap_start": None,
                        "gap_minutes": 0,
                        "reason": "MISS"
                    })
                    print(f"[validator] Finalizing alert as MISS (late arrival in 15-45m window) player={player_id} bet_id={bet_id}")
                else:
                    # No late arrivals in the 15-45m window -> confirm MATCH
                    res_base.update({
                        "result": True,
                        "gap_start": None,
                        "gap_minutes": 0,
                        "reason": "MATCH"
                    })
                    print(f"[validator] Finalizing alert as MATCH (no late arrivals in 15-45m window) player={player_id} bet_id={bet_id}")
            else:
                res_base.update({
                    "result": False,
                    "gap_start": None,
                    "gap_minutes": 0,
                    "reason": "PENDING"
                })
    return res_base


# ------------------ Main loop ------------------
def validate_once(conn: sqlite3.Connection, force_finalize: bool = False) -> None:
    now_hk = datetime.now(HK_TZ)
    prune_validator_retention(conn, now_hk)

    alerts = parse_alerts(conn)
    if alerts.empty:
        print("[validator] No alerts to validate")
        return

    processed = {str(bid) for bid in load_processed(conn)}
    alerts["bet_id_str"] = alerts["bet_id"].astype(str)
    pending_all = alerts[~alerts["bet_id_str"].isin(processed)].copy()
    if pending_all.empty:
        print(f"[validator] Alerts: {len(alerts)}, Pending: 0 (all processed)")
        return

    freshness_buffer_min = getattr(config, "VALIDATOR_FRESHNESS_BUFFER_MINUTES", 2)
    wait_minutes = 45 + max(0, freshness_buffer_min)
    cutoff = now_hk - timedelta(minutes=wait_minutes)
    finality_cutoff = now_hk - timedelta(hours=getattr(config, 'VALIDATOR_FINALITY_HOURS', 1))

    effective_ts = pd.to_datetime(pending_all["bet_ts"].fillna(pending_all["ts"]))
    if effective_ts.dt.tz is None:
        effective_ts = effective_ts.dt.tz_localize(HK_TZ)
    else:
        effective_ts = effective_ts.dt.tz_convert(HK_TZ)

    pending = pending_all[effective_ts <= cutoff].copy()
    if pending.empty:
        print(f"[validator] {len(pending_all)} pending, but all are too recent (<{wait_minutes}m)")
        return

    if force_finalize:
        print("[validator] WARNING: running with --force-finalize; PENDING candidates will be finalized now")

    print(f"[validator] Processing {len(pending)} alerts (including re-checks)...")

    existing_results = load_existing_results(conn)

    player_ids = (
        pending.loc[pending["player_id"].notna(), "player_id"]
        .astype(int)
        .unique()
        .tolist()
    )
    try:
        processed_players = (
            alerts[alerts["bet_id_str"].isin(processed)]["player_id"]
            .dropna()
            .astype(int)
            .unique()
            .tolist()
        )
        for pid in processed_players:
            if pid not in player_ids:
                player_ids.append(pid)
    except Exception:
        pass

    bet_cache = {}
    session_cache = {}
    if player_ids:
        fetch_start = effective_ts[pending.index].min() - timedelta(hours=1)
        fetch_end = now_hk
        bet_cache = fetch_bets_for_players(player_ids, fetch_start, fetch_end)
        session_cache = fetch_sessions_for_players(player_ids, fetch_start, fetch_end)

    new_processed_ids: List = []
    updated_count = 0

    for bid in list(processed):
        key = str(bid)
        if key not in existing_results:
            try:
                match = alerts[alerts["bet_id_str"] == key]
                if not match.empty:
                    r = validate_alert_row(match.iloc[0], bet_cache, session_cache, force_finalize=force_finalize)
                    if r.get("result") is not None:
                        existing_results[key] = r
            except Exception:
                continue

    for key, saved_row in list(existing_results.items()):
        try:
            if saved_row.get("reason") == "PENDING":
                bid = saved_row.get("bet_id")
                match = alerts[alerts["bet_id_str"] == str(bid)] if pd.notna(bid) else pd.DataFrame()
                if not match.empty:
                    newr = validate_alert_row(match.iloc[0], bet_cache, session_cache, force_finalize=force_finalize)
                    if newr.get("result") is not None and (newr.get("reason") != "PENDING"):
                        existing_results[key] = newr
                        updated_count += 1
        except Exception:
            continue

    for _, row in pending.iterrows():
        res = validate_alert_row(row, bet_cache, session_cache, force_finalize=force_finalize)
        if res.get("result") is None:
            continue

        bid = str(row["bet_id"])
        key = bid if bid != "nan" else f"{row['player_id']}_{row['ts']}"

        is_new = key not in existing_results
        is_upgrade = not is_new and not existing_results[key]["result"] and res["result"]
        was_pending = not is_new and existing_results[key].get("reason") == "PENDING"
        is_finalize = was_pending and res.get("reason") == "MISS"

        if res.get("reason") in IGNORED_REASONS:
            existing_results[key] = res
            processed.add(row["bet_id"])
            new_processed_ids.append(row["bet_id"])
            continue

        if is_new or is_upgrade or is_finalize:
            existing_results[key] = res
            if is_upgrade or is_finalize:
                updated_count += 1

        alert_dt = pd.to_datetime(row["bet_ts"] if pd.notnull(row["bet_ts"]) else row["ts"])
        if alert_dt.tzinfo is None:
            alert_dt = alert_dt.replace(tzinfo=HK_TZ)

        if res["result"] == True or alert_dt <= finality_cutoff:
            if res["result"] == False:
                res["reason"] = "MISS"
                existing_results[key] = res

            processed.add(row["bet_id"])
            new_processed_ids.append(row["bet_id"])

    if existing_results:
        final_df = pd.DataFrame(list(existing_results.values()))[VALIDATION_COLUMNS]

        kpi_df = final_df[~final_df["reason"].isin(IGNORED_REASONS)]
        finalized_or_old = kpi_df[kpi_df["reason"] != "PENDING"]
        total = len(finalized_or_old)
        matches = finalized_or_old["reason"].eq("MATCH").sum()
        precision = (matches / total) if total > 0 else 0
        print(f"[validator] Cumulative Precision (15m window): {precision:.2%} ({matches}/{total})")

        final_df["alert_ts_dt"] = pd.to_datetime(final_df["alert_ts"])
        final_df = final_df.sort_values("alert_ts_dt").drop(columns=["alert_ts_dt"])
        save_validation_results(conn, final_df)
        print(f"[validator] Saved {len(final_df)} total validations to SQLite (Updated {updated_count}, Finalized {len(new_processed_ids)})")

    mark_processed(conn, new_processed_ids)
    return


def main():
    parser = argparse.ArgumentParser(description="Validate alerts against realized walkaways")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run a single validation pass and exit")
    parser.add_argument("--force-finalize", action="store_true", help="Force-finalize PENDING candidates immediately (for manual runs)")
    args = parser.parse_args()

    conn = get_db_conn()
    interval = args.interval

    while True:
        start_time = time.time()
        try:
            validate_once(conn, force_finalize=args.force_finalize)
        except Exception as exc:
            print(f"[validator] ERROR: {exc}")
        if args.once:
            break
        # Sleep to next tick (preventing overlap)
        elapsed = time.time() - start_time
        sleep_time = max(0, interval - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
