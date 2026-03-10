import sqlite3
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify, send_from_directory, abort
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import config
from pathlib import Path
import json

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / "frontend"

# Point Flask's static handler to the actual frontend/static folder for Chart.js, etc.
app = Flask(
    __name__,
    static_folder=str(FRONTEND_DIR / "static"),
    static_url_path="/static",
)
HK_TZ = ZoneInfo(config.HK_TZ)
STATE_DB_PATH = BASE_DIR / "local_state" / "state.db"
STATUS_JSON_PATH = BASE_DIR / "out_status" / "table_status.json"
HC_PATH = BASE_DIR / "out_status" / "table_hc.csv"


def get_db_conn() -> sqlite3.Connection:
    STATE_DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

# --- Static frontend serving ---
@app.route("/")
@app.route("/main.html")
def index():
    """Serves the primary dashboard UI."""
    print(f"[api] Serving main.html from: {FRONTEND_DIR}")
    return send_from_directory(FRONTEND_DIR, "main.html")


@app.route("/style.css")
def style_css():
    """Serve bundled stylesheet from frontend folder."""
    return send_from_directory(FRONTEND_DIR, "style.css")


@app.route("/script.js")
def script_js():
    """Serve bundled script from frontend folder."""
    return send_from_directory(FRONTEND_DIR, "script.js")

@app.route("/<path:filename>")
def frontend_module(filename):
    target = FRONTEND_DIR / filename
    if filename.endswith('.js') and target.exists():
        return send_from_directory(FRONTEND_DIR, filename)
    abort(404)

#   GET /get_floor_status     → returns all open table-seat pairs (occupied seats)


# --- get_floor_status endpoint ---
@app.route("/get_floor_status", methods=["GET"])
def get_floor_status():
    """
    Returns a list of occupied table-seat pairs (open sessions) for the gaming floor.
    Primary path: returns cached layout with per-seat status and rich seat/table context written by status_server.
    Output: {"updated_at": ..., "layout": [{"table_id": ..., "x": ..., "y": ..., "status": {"1":0,"2":1,...}, "seat_info": {...}, "table_metrics": {...}}]}
    """
    try:
        with get_db_conn() as conn:
            row = conn.execute(
                "SELECT layout_json FROM status_snapshots ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            if row and row[0]:
                payload = json.loads(row[0])
                layout = payload.get("layout")
                if layout is not None:
                    resp = jsonify({
                        "updated_at": payload.get("updated_at"),
                        "layout": layout,
                    })
                    resp.headers["Access-Control-Allow-Origin"] = "*"
                    return resp
    except Exception as e:
        print(f"[api] get_floor_status DB error: {e}")

    df = pd.DataFrame()
    buffer_path = BASE_DIR / "out_trainer" / "sessions_buffer.csv"
    if buffer_path.exists() and buffer_path.stat().st_size < 50 * 1024 * 1024:
        try:
            df = pd.read_csv(buffer_path)
        except Exception:
            df = pd.DataFrame()

    # Fallback: sample data last resort
    if df.empty:
        sample_path = BASE_DIR / "sample data" / "SmartTableData_tsession_sample.csv"
        try:
            df = pd.read_csv(sample_path)
        except Exception:
            return jsonify({"occupied": []})

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
        return jsonify({"occupied": []})

    occupied = (
        open_sessions[["table_id", seat_col]]
        .dropna()
        .rename(columns={seat_col: "seat_id"})
        .astype({"table_id": str, "seat_id": str})
        .drop_duplicates(subset=["table_id", "seat_id"], keep="last")
        .to_dict(orient="records")
    )
    resp = jsonify({"occupied": occupied})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

@app.route("/get_hc_history", methods=["GET"])
def get_hc_history():
    """Returns the historical headcounts from table_hc.csv

    Optional query param:
      - hours: number of past hours to return (int). If provided, we compute an approximate
               number of rows based on `TABLE_STATUS_REFRESH_SECONDS` and return that many rows.
    """
    try:
        with get_db_conn() as conn:
            hours_param = request.args.get('hours')
            limit_rows = 200
            where_clause = ""
            params = []
            if hours_param is not None:
                try:
                    hours = float(hours_param)
                    cutoff = datetime.now(HK_TZ) - timedelta(hours=hours)
                    where_clause = "WHERE ts > ?"
                    params.append(cutoff.isoformat())
                    sec_per_row = float(getattr(config, 'TABLE_STATUS_REFRESH_SECONDS', 45))
                    rows_needed = int((hours * 3600.0) / sec_per_row)
                    limit_rows = max(10, min(rows_needed, 10000))
                except Exception:
                    pass

            query = f"SELECT * FROM hc_history {where_clause} ORDER BY ts DESC LIMIT ?"
            params.append(limit_rows)
            df = pd.read_sql_query(query, conn, params=params)
            if df.empty:
                return jsonify([])
            # return newest first already; frontend can handle ordering
            data = df.to_dict(orient="records")
            resp = jsonify(data)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- get_validation endpoint ---
@app.route("/get_validation", methods=["GET"])
def get_validation():
    ts = request.args.get("ts")
    start = datetime.now()
    try:
        with get_db_conn() as conn:
            df = pd.read_sql_query("SELECT * FROM validation_results", conn)
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})

    if df.empty:
        return jsonify({"results": []})

    df["validated_at"] = pd.to_datetime(df["validated_at"], errors="coerce")
    df = df.dropna(subset=["validated_at"]).sort_values("validated_at")

    bet_id = request.args.get('bet_id')
    bet_ids = request.args.get('bet_ids')
    if bet_ids:
        try:
            ids = [s.strip() for s in str(bet_ids).split(',') if s.strip()]
            df = df[df["bet_id"].astype(str).isin(ids)]
        except Exception:
            pass
    elif bet_id:
        try:
            df = df[df["bet_id"].astype(str) == str(bet_id)]
        except Exception:
            pass

    if ts:
        try:
            ts_dt = pd.to_datetime(ts)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.tz_localize(HK_TZ)
            else:
                ts_dt = ts_dt.tz_convert(HK_TZ)
            df = df[df["validated_at"] > ts_dt]
        except Exception:
            pass

    if df.empty:
        return jsonify({"results": []})

    out = df[["alert_ts", "player_id", "bet_id", "gap_start", "result", "validated_at", "reason", "bet_ts"]].rename(columns={
        "alert_ts": "ts",
        "gap_start": "walkaway_ts",
        "result": "TP",
        "validated_at": "sync_ts"
    }).copy()

    for col in ["ts", "walkaway_ts", "sync_ts", "bet_ts"]:
        dt_col = pd.to_datetime(out[col], errors="coerce")
        if getattr(dt_col.dt, "tz", None) is None:
            dt_col = dt_col.dt.tz_localize(HK_TZ)
        else:
            dt_col = dt_col.dt.tz_convert(HK_TZ)
        out[col] = dt_col.dt.floor("s").dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    out = out.replace({np.nan: None, np.inf: None, -np.inf: None})
    results = out.to_dict(orient="records")
    print(f"[api] get_validation: {len(results)} rows in {(datetime.now()-start).total_seconds():.3f}s")
    resp = jsonify({"results": results})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

@app.route("/get_alerts", methods=["GET"])
def get_alerts():
    ts = request.args.get("ts")
    start = datetime.now()
    try:
        with get_db_conn() as conn:
            df = pd.read_sql_query("SELECT * FROM alerts", conn)
    except Exception as e:
        return jsonify({"alerts": [], "error": str(e)})

    if df.empty:
        return jsonify({"alerts": []})

    df["ts_dt"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts_dt"]).sort_values("ts_dt")

    if ts:
        try:
            ts_dt = pd.to_datetime(ts)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.tz_localize(HK_TZ)
            else:
                ts_dt = ts_dt.tz_convert(HK_TZ)
            df = df[df["ts_dt"] > ts_dt]
        except Exception:
            pass

    if df.empty:
        return jsonify({"alerts": []})

    df["ts"] = df["ts_dt"].dt.tz_localize(HK_TZ, ambiguous='NaT', nonexistent='shift_forward') if df["ts_dt"].dt.tz is None else df["ts_dt"].dt.tz_convert(HK_TZ)
    df["ts"] = df["ts"].dt.floor("s").dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    df_out = df.drop(columns=["ts_dt"]).replace({np.nan: None, np.inf: None, -np.inf: None})
    alerts = df_out.to_dict(orient="records")
    print(f"[api] get_alerts: {len(alerts)} rows in {(datetime.now()-start).total_seconds():.3f}s")
    resp = jsonify({"alerts": alerts})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

if __name__ == "__main__":
    print(f" * SQLite Path: {STATE_DB_PATH}")
    app.run(host="0.0.0.0", port=8000, debug=True)
