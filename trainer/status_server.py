import json
import math
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from zoneinfo import ZoneInfo

import config
from db_conn import get_clickhouse_client

HK_TZ = ZoneInfo(config.HK_TZ)
BASE_DIR = Path(__file__).parent
STATE_DB_PATH = BASE_DIR / "local_state" / "state.db"
STATUS_PATH = BASE_DIR / "out_status" / "table_status.json"  # legacy seed only
HC_PATH = BASE_DIR / "out_status" / "table_hc.csv"  # legacy seed only
REFRESH_SECONDS = getattr(config, "TABLE_STATUS_REFRESH_SECONDS", 45)
LOOKBACK_HOURS = getattr(config, "TABLE_STATUS_LOOKBACK_HOURS", 12)
STATUS_RETENTION_HOURS = getattr(config, "TABLE_STATUS_RETENTION_HOURS", 24)
HC_RETENTION_DAYS = getattr(config, "TABLE_STATUS_HC_RETENTION_DAYS", 30)
TARGET_ASPECT = 37 / 7  # width/height (matches requested 7:37 h:w layout)
EMPTY_SEED = 42  # deterministic empties placement
SEAT_KEYS = ["1", "2", "3", "5", "6", "0"]  # include 0 as other/unknown
LAST_OCCUPIED: List[dict] = []


def get_db_conn() -> sqlite3.Connection:
    STATE_DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS status_layout (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            layout_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS status_snapshots (
            updated_at TEXT PRIMARY KEY,
            layout_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hc_history (
            ts TEXT PRIMARY KEY,
            tables INTEGER,
            seats INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status_updated ON status_snapshots(updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hc_ts ON hc_history(ts)")
    return conn


def load_recent_alert_stats(conn: sqlite3.Connection) -> dict:
    """Return latest alert stats keyed by (table_id, position_idx)."""
    stats = {}
    try:
        rows = conn.execute(
            """
            SELECT table_id, position_idx, ts,
                   loss_streak, bets_last_5m, bets_last_15m, bets_last_30m,
                   wager_last_10m, wager_last_30m, cum_bets, cum_wager,
                   avg_wager_sofar, session_duration_min, bets_per_minute
            FROM alerts
            WHERE table_id IS NOT NULL AND position_idx IS NOT NULL
            ORDER BY ts DESC
            """
        ).fetchall()
        for r in rows:
            table_str = str(r["table_id"])
            # Normalize seat/position to avoid "2.0" vs "2" mismatches
            pos_val = r["position_idx"]
            if pos_val is None:
                continue
            try:
                pos_str = str(int(float(pos_val)))
            except Exception:
                pos_str = str(pos_val)
            key = (table_str, pos_str)
            if key in stats:
                continue  # already took the latest
            stats[key] = {
                "loss_streak": r["loss_streak"],
                "bets_last_5m": r["bets_last_5m"],
                "bets_last_15m": r["bets_last_15m"],
                "bets_last_30m": r["bets_last_30m"],
                "wager_last_10m": r["wager_last_10m"],
                "wager_last_30m": r["wager_last_30m"],
                "cum_bets": r["cum_bets"],
                "cum_wager": r["cum_wager"],
                "avg_wager_sofar": r["avg_wager_sofar"],
                "session_duration_min": r["session_duration_min"],
                "bets_per_minute": r["bets_per_minute"],
            }
    except Exception as e:
        print(f"[status_server] load_recent_alert_stats failed: {e}")
    return stats


def prune_status(conn: sqlite3.Connection, now_hk: pd.Timestamp):
    try:
        cutoff = now_hk - pd.Timedelta(hours=STATUS_RETENTION_HOURS)
        conn.execute("DELETE FROM status_snapshots WHERE updated_at < ?", (cutoff.isoformat(),))
        if HC_RETENTION_DAYS and HC_RETENTION_DAYS > 0:
            hc_cutoff = now_hk - pd.Timedelta(days=HC_RETENTION_DAYS)
            conn.execute("DELETE FROM hc_history WHERE ts < ?", (hc_cutoff.isoformat(),))
        conn.commit()
    except Exception as e:
        print(f"[status_server] prune error: {e}")


def fetch_table_ids() -> pd.Series:
    client = get_clickhouse_client()
    query = f"""
        SELECT DISTINCT table_id
        FROM {config.SOURCE_DB}.{config.TSESSION}
        WHERE table_id IS NOT NULL
        SETTINGS max_execution_time = 15, max_result_rows = 500000, max_result_bytes = 536870912
    """
    df = client.query_df(query)
    if df.empty:
        return pd.Series(dtype=str)
    return df["table_id"].dropna().astype(str).drop_duplicates().sort_values().reset_index(drop=True)


def fetch_bet_summaries(session_ids: List[str]) -> dict:
    """Return {session_id: (sum_wager, bet_count)} using wager>0 only."""
    if not session_ids:
        return {}
    client = get_clickhouse_client()
    summaries = {}
    chunk = 500
    for i in range(0, len(session_ids), chunk):
        chunk_ids = session_ids[i : i + chunk]
        params = {"sids": tuple(chunk_ids)}
        query = f"""
            SELECT session_id, sum(wager) AS sum_wager, count() AS bet_count
            FROM {config.SOURCE_DB}.{config.TBET}
            WHERE session_id IN %(sids)s
              AND wager > 0
            GROUP BY session_id
        """
        try:
            df = client.query_df(query, parameters=params)
            for _, row in df.iterrows():
                sid = str(row.get("session_id"))
                summaries[sid] = (
                    float(row.get("sum_wager")) if row.get("sum_wager") is not None else 0.0,
                    int(row.get("bet_count")) if row.get("bet_count") is not None else 0,
                )
        except Exception as e:
            print(f"[status_server] bet summaries chunk error: {e}")
    return summaries


def choose_grid(n: int, aspect: float = TARGET_ASPECT) -> Tuple[int, int]:
    # Want cols/rows ~ aspect, minimal area covering n
    rows = max(1, math.ceil(math.sqrt(n / aspect)))
    cols = max(1, math.ceil(aspect * rows))
    while rows * cols < n:
        rows += 1
        cols = max(cols, math.ceil(aspect * rows))
    return rows, cols


def build_layout(table_ids: pd.Series, aspect: float = TARGET_ASPECT, seed: int = EMPTY_SEED):
    ids = table_ids.tolist()
    if not ids:
        return []

    rows, cols = choose_grid(len(ids), aspect)
    capacity = rows * cols
    empties = capacity - len(ids)

    rng = random.Random(seed)
    empty_slots = set(rng.sample(range(capacity), empties)) if empties > 0 else set()

    step_x = 1 / (cols - 1) if cols > 1 else 0
    step_y = 1 / (rows - 1) if rows > 1 else 0

    layout = []
    idx_tid = 0
    for pos in range(capacity):
        if pos in empty_slots:
            continue
        if idx_tid >= len(ids):
            break
        tid = ids[idx_tid]
        idx_tid += 1
        r = pos // cols
        c = pos % cols
        x = round(c * step_x, 4)
        y = round(r * step_y, 4)
        layout.append({"table_id": tid, "x": x, "y": y})
    return layout, rows, cols, capacity, len(empty_slots)


def load_base_layout(conn: sqlite3.Connection) -> list:
    try:
        row = conn.execute("SELECT layout_json FROM status_layout WHERE id=1").fetchone()
        if row and row["layout_json"]:
            return json.loads(row["layout_json"])
    except Exception:
        pass
    # fallback to existing JSON if present (one-time seed)
    if STATUS_PATH.exists():
        try:
            base = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            layout = base.get("layout", [])
            conn.execute(
                """
                INSERT INTO status_layout(id, layout_json) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET layout_json=excluded.layout_json
                """,
                (json.dumps(layout),),
            )
            conn.commit()
            return layout
        except Exception:
            return []
    return []


def fetch_open_sessions_ch() -> pd.DataFrame:
    client = get_clickhouse_client()
    cutoff = pd.Timestamp.now(tz=HK_TZ) - pd.Timedelta(hours=LOOKBACK_HOURS)
    query = f"""
        SELECT DISTINCT
            session_id,
            table_id,
            position_label AS seat_id,
            seat_label,
            player_id,
            avg_bet,
            player_win,
            turnover,
            num_bets,
            adjusted_turnover,
            avg_cash_bet,
            session_start_dtm,
            session_end_dtm,
            status,
            is_canceled,
            is_deleted,
            table_name,
            game_type,
            gaming_area,
            pit_name
        FROM {config.SOURCE_DB}.{config.TSESSION}
        WHERE (session_end_dtm IS NULL)
          AND (session_start_dtm >= %(cutoff)s)
          AND (is_canceled = 0 OR is_canceled IS NULL)
          AND (is_deleted = 0 OR is_deleted IS NULL)
          AND (status IS NULL OR status NOT IN ('closed','ended','completed','canceled','cancelled'))
        SETTINGS
          max_execution_time = 2,
          max_result_rows = 50000,
          max_result_bytes = 10485760
    """
    return client.query_df(query, parameters={"cutoff": cutoff})


def resolve_open_sessions() -> List[dict]:
    global LAST_OCCUPIED
    # Try ClickHouse
    df = pd.DataFrame()
    try:
        df = fetch_open_sessions_ch()
    except Exception:
        print("[status_server] ClickHouse fetch failed; no occupancy data")
        return []

    # Apply lookback if session_start_dtm exists
    if "session_start_dtm" in df.columns:
        try:
            df["session_start_dtm"] = pd.to_datetime(df["session_start_dtm"], errors="coerce")
            cutoff = pd.Timestamp.now(tz=HK_TZ) - pd.Timedelta(hours=LOOKBACK_HOURS)
            df = df[df["session_start_dtm"] >= cutoff]
        except Exception:
            pass

    if "session_end_dtm" in df.columns:
        open_mask = df["session_end_dtm"].isnull() | (df["session_end_dtm"] == "")
    else:
        open_mask = pd.Series([True] * len(df))
    if "status" in df.columns:
        open_mask = open_mask & (~df["status"].astype(str).str.lower().isin(["closed", "ended", "completed", "canceled", "cancelled"]))
    if "is_canceled" in df.columns:
        open_mask = open_mask & (~df["is_canceled"].fillna(0).astype(int).astype(bool))
    if "is_deleted" in df.columns:
        open_mask = open_mask & (~df["is_deleted"].fillna(0).astype(int).astype(bool))

    open_sessions = df[open_mask]

    if "seat_id" in open_sessions.columns:
        seat_col = "seat_id"
    elif "position_label" in open_sessions.columns:
        seat_col = "position_label"
    else:
        seat_col = None
    if seat_col is None or "table_id" not in open_sessions.columns:
        return []

    open_sessions = open_sessions.rename(columns={seat_col: "seat_id"})

    def to_ts(val):
        if pd.isna(val):
            return None
        try:
            return pd.to_datetime(val).isoformat()
        except Exception:
            return str(val)

    records = []
    seen_pairs = set()
    for _, row in open_sessions.iterrows():
        tid = row.get("table_id")
        seat = row.get("seat_id")
        if pd.isna(tid) or pd.isna(seat):
            continue
        tid_str = str(tid)
        seat_str = str(seat)
        pair = (tid_str, seat_str)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        rec = {
            "table_id": tid_str,
            "seat_id": seat_str,
            "session_id": None if pd.isna(row.get("session_id")) else str(row.get("session_id")),
            "player_id": None if pd.isna(row.get("player_id")) else str(row.get("player_id")),
            "avg_bet": None if pd.isna(row.get("avg_bet")) else float(row.get("avg_bet")),
            "turnover": None if pd.isna(row.get("turnover")) else float(row.get("turnover")),
            "adjusted_turnover": None if pd.isna(row.get("adjusted_turnover")) else float(row.get("adjusted_turnover")),
            "avg_cash_bet": None if pd.isna(row.get("avg_cash_bet")) else float(row.get("avg_cash_bet")),
            "win": None if pd.isna(row.get("player_win")) else float(row.get("player_win")),
            "num_bets": None if pd.isna(row.get("num_bets")) else int(row.get("num_bets")),
            "session_start": to_ts(row.get("session_start_dtm")),
            "table_name": None if pd.isna(row.get("table_name")) else str(row.get("table_name")),
            "game_type": None if pd.isna(row.get("game_type")) else str(row.get("game_type")),
            "pit_name": None if pd.isna(row.get("pit_name")) else str(row.get("pit_name")),
            "gaming_area": None if pd.isna(row.get("gaming_area")) else str(row.get("gaming_area")),
            "seat_label": None if pd.isna(row.get("seat_label")) else str(row.get("seat_label")),
            "status_flag": None if pd.isna(row.get("status")) else str(row.get("status")),
        }
        # Derive avg_bet if missing using available fields
        if rec["avg_bet"] is None or rec["avg_bet"] == 0:
            try:
                if rec["adjusted_turnover"] is not None and rec["num_bets"]:
                    rec["avg_bet"] = float(rec["adjusted_turnover"]) / float(rec["num_bets"])
                elif rec["turnover"] is not None and rec["num_bets"]:
                    rec["avg_bet"] = float(rec["turnover"]) / float(rec["num_bets"])
                elif rec["avg_cash_bet"] is not None and rec["avg_cash_bet"] > 0:
                    rec["avg_bet"] = rec["avg_cash_bet"]
            except Exception:
                pass
        records.append(rec)

    # Backfill avg_bet/turnover/num_bets using wager>0 bets when missing or zero
    missing_sessions = [r["session_id"] for r in records if r.get("session_id") and ((r.get("avg_bet") in (None, 0)) or (r.get("num_bets") in (None, 0)))]
    if missing_sessions:
        summaries = fetch_bet_summaries(missing_sessions)
        for rec in records:
            sid = rec.get("session_id")
            if not sid or sid not in summaries:
                continue
            sum_wager, bet_count = summaries[sid]
            if bet_count and sum_wager:
                rec["avg_bet"] = sum_wager / bet_count
                rec["turnover"] = rec["turnover"] if rec.get("turnover") not in (None, 0) else sum_wager
                rec["num_bets"] = bet_count

    if not records and LAST_OCCUPIED:
        print("[status_server] No occupancy rows found after filtering; reusing last known occupancy")
        return LAST_OCCUPIED
    LAST_OCCUPIED = records
    return records


def fetch_table_ids_for_layout() -> List[str]:
    """One-time ClickHouse lookup to seed layout if nothing exists."""
    try:
        df = fetch_table_ids()
        if df.empty:
            raise ValueError("no table_id rows from ClickHouse")
        return df.tolist()
    except Exception as e:
        print(f"[status_server] failed to fetch table ids for layout seed: {e}")
    return []


def write_status(conn: sqlite3.Connection, occupied: List[dict]) -> None:
    alert_stats = load_recent_alert_stats(conn)
    # Build occupancy map: table_id -> seat_id -> rich seat info
    occ_map: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in occupied:
        tid = str(row.get("table_id")) if row.get("table_id") is not None else None
        seat = str(row.get("seat_id")) if row.get("seat_id") is not None else None
        if tid is None or seat is None:
            continue
        seat_info = {
            "seat_id": seat,
            "session_id": row.get("session_id"),
            "player_id": row.get("player_id"),
            "avg_bet": row.get("avg_bet"),
            "turnover": row.get("turnover"),
            "adjusted_turnover": row.get("adjusted_turnover"),
            "avg_cash_bet": row.get("avg_cash_bet"),
            "win": row.get("win"),
            "num_bets": row.get("num_bets"),
            "session_start": row.get("session_start"),
            "table_name": row.get("table_name"),
            "game_type": row.get("game_type"),
            "pit_name": row.get("pit_name"),
            "gaming_area": row.get("gaming_area"),
            "seat_label": row.get("seat_label"),
            "status_flag": row.get("status_flag"),
        }
        # Merge latest alert feature stats for this seat if present
        key = (tid, seat)
        if key in alert_stats:
            seat_info.update(alert_stats[key])
        occ_map.setdefault(tid, {})[seat] = seat_info

    # Load base layout from DB (or seed from legacy file once)
    layout = load_base_layout(conn)

    # If no layout exists, build layout via generate_table_layout logic (ground truth) and persist
    if not layout:
        table_ids = list(occ_map.keys())
        if not table_ids:
            table_ids = fetch_table_ids_for_layout()
        try:
            layout_df = pd.Series(table_ids, dtype=str)
            layout, _, _, _, _ = build_layout(layout_df, aspect=TARGET_ASPECT, seed=EMPTY_SEED)
        except Exception as e:
            print(f"[status_server] failed to build layout via generator: {e}")
            layout = []
        if layout:
            try:
                conn.execute(
                    """
                    INSERT INTO status_layout(id, layout_json)
                    VALUES (1, ?)
                    ON CONFLICT(id) DO UPDATE SET layout_json=excluded.layout_json
                    """,
                    (json.dumps(layout),),
                )
                conn.commit()
                print(f"[status_server] seeded fallback layout with {len(layout)} tables")
            except Exception as e:
                print(f"[status_server] failed to seed fallback layout: {e}")

    # Update layout entries with status dict
    for entry in layout:
        tid = str(entry.get("table_id")) if entry.get("table_id") is not None else None
        if tid is None:
            continue
        # Defaulted table metadata for tooltip consumption
        entry.setdefault("dealer_id", "123456789")
        entry.setdefault("table_status", "Open")
        entry.setdefault("min_bet", 2000)
        entry.setdefault("max_bet", 2_000_000)
        status: Dict[str, int] = {k: 0 for k in SEAT_KEYS}
        table_seat_info: Dict[str, Dict[str, Any]] = {}
        for seat, info in occ_map.get(tid, {}).items():
            if seat in status:
                status[seat] = 1
            else:
                status["0"] = 1  # unexpected seat id
            table_seat_info[seat] = info
        entry["status"] = status
        if table_seat_info:
            entry["seat_info"] = table_seat_info

        # Roll up simple table metrics for tooltip consumption (independent of alerts/validation)
        try:
            avg_bets = [
                float(v.get("avg_bet"))  # type: ignore[arg-type, union-attr]
                for v in table_seat_info.values()
                if v.get("avg_bet") is not None
            ]
            turnover_sum = sum(
                float(v.get("turnover"))  # type: ignore[arg-type, misc, union-attr]
                for v in table_seat_info.values()
                if v.get("turnover") is not None
            )
            win_sum = sum(
                float(v.get("win"))  # type: ignore[arg-type, misc, union-attr]
                for v in table_seat_info.values()
                if v.get("win") is not None
            )
            push_sum = sum(
                float(v.get("push"))  # type: ignore[arg-type, misc, union-attr]
                for v in table_seat_info.values()
                if v.get("push") is not None
            )
            entry["table_metrics"] = {
                "avg_bet": (sum(avg_bets) / len(avg_bets)) if avg_bets else None,
                "turnover": turnover_sum if turnover_sum != 0 else None,
                "win": win_sum if win_sum != 0 else None,
                "push": push_sum if push_sum != 0 else None,
                "active_seats": sum(1 for v in status.values() if v == 1),
                "session_count": len(table_seat_info),
            }
            # Propagate commonly used metadata (first non-empty)
            meta_fields = ["table_name", "game_type", "pit_name", "gaming_area"]
            for field in meta_fields:
                val = next(
                    (v.get(field) for v in table_seat_info.values() if v.get(field) not in (None, "")),
                    None,
                )
                if val is not None:
                    entry[field] = val
        except Exception:
            pass

    updated_at = pd.Timestamp.now(tz=HK_TZ)

    # Persist layout (static) and latest snapshot
    try:
        conn.execute(
            """
            INSERT INTO status_layout(id, layout_json)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET layout_json=excluded.layout_json
            """,
            (json.dumps(layout),),
        )
        conn.execute(
            """
            INSERT INTO status_snapshots(updated_at, layout_json)
            VALUES (?, ?)
            ON CONFLICT(updated_at) DO UPDATE SET layout_json=excluded.layout_json
            """,
            (updated_at.isoformat(), json.dumps({"updated_at": updated_at.isoformat(), "layout": layout})),
        )
        # Headcount rollup
        occ_tables = 0
        occ_seats = 0
        for entry in layout:
            if entry.get("status"):
                seats = sum(1 for v in entry["status"].values() if v == 1)
                if seats > 0:
                    occ_tables += 1
                    occ_seats += seats
        conn.execute(
            """
            INSERT INTO hc_history(ts, tables, seats) VALUES (?, ?, ?)
            ON CONFLICT(ts) DO UPDATE SET tables=excluded.tables, seats=excluded.seats
            """,
            (updated_at.isoformat(), occ_tables, occ_seats),
        )
        conn.commit()
        prune_status(conn, updated_at)
    except Exception as e:
        print(f"[status_server] DB write error: {e}")


def main():
    print(f"[status_server] starting; refresh every {REFRESH_SECONDS}s")
    conn = get_db_conn()
    while True:
        try:
            occupied = resolve_open_sessions()
            write_status(conn, occupied)
            print(f"[status_server] wrote {len(occupied)} occupied seats to SQLite")
        except Exception as e:
            print(f"[status_server] error: {e}")
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    main()
