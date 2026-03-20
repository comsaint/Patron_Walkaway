import sqlite3
import os
import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from flask import Flask, request, jsonify, send_from_directory, abort
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
try:
    import config  # type: ignore[import]
except ModuleNotFoundError:
    import trainer.config as config  # type: ignore[import, no-redef]
from pathlib import Path
import json

BASE_DIR = Path(__file__).resolve().parent.parent  # trainer/ (serving lives under trainer)
PROJECT_ROOT = BASE_DIR.parent
FRONTEND_DIR = BASE_DIR / "frontend"

# Point Flask's static handler to the actual frontend/static folder for Chart.js, etc.
app = Flask(
    __name__,
    static_folder=str(FRONTEND_DIR / "static"),
    static_url_path="/static",
)
HK_TZ = ZoneInfo(config.HK_TZ)
_state_db_env = os.environ.get("STATE_DB_PATH")
_state_db_effective = _state_db_env.strip() if (_state_db_env and _state_db_env.strip()) else None
STATE_DB_PATH = Path(_state_db_effective) if _state_db_effective else (PROJECT_ROOT / "local_state" / "state.db")
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


# --- ML API Protocol (package/ML_API_PROTOCOL.md): GET /alerts, GET /validation ---
# Default: last 24h when no query params. Port 8001. Protocol fields only.
# Timestamps: HK time, format +08:00 per spec. limit only when ts absent.

def _format_ts_hk_iso(series):
    """Format datetime series as ISO with +08:00 offset (spec: HK timezone)."""
    s = series.dt.floor("s").dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return s.str.replace(r"(\+|-)(\d{2})(\d{2})$", r"\1\2:\3", regex=True)


def _alerts_24h_cutoff():
    return datetime.now(HK_TZ) - timedelta(hours=24)


def _validation_24h_cutoff():
    return datetime.now(HK_TZ) - timedelta(hours=24)


def _query_alerts_df(ts_param=None, limit_param=None, default_24h=False):
    """Return alerts DataFrame with ts filter. If default_24h and no ts_param, restrict to last 24h. Optional limit when ts absent."""
    with get_db_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM alerts", conn)
    if df.empty:
        return df
    df["ts_dt"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts_dt"]).sort_values("ts_dt")
    if ts_param:
        try:
            ts_dt = pd.to_datetime(ts_param)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.tz_localize(HK_TZ)
            else:
                ts_dt = ts_dt.tz_convert(HK_TZ)
            df = df[df["ts_dt"] > ts_dt]
        except Exception:
            pass
    elif default_24h:
        cutoff = _alerts_24h_cutoff()
        df = df[df["ts_dt"] > cutoff]
    # Spec: limit only used when ts is absent
    if limit_param is not None and not ts_param:
        try:
            limit = int(limit_param)
            if limit > 0:
                df = df.tail(limit)
        except (ValueError, TypeError):
            pass
    return df


def _alerts_to_protocol_records(df):
    """Shape alerts to ML_API_PROTOCOL.md: only protocol fields; casino_player_id from DB when present; is_known_player from is_rated_obs; timestamps +08:00; bet_id/session_id int when possible."""
    if df.empty:
        return []
    df = df.copy()
    ts_ser = (
        df["ts_dt"].dt.tz_localize(HK_TZ, ambiguous="NaT", nonexistent="shift_forward")
        if df["ts_dt"].dt.tz is None
        else df["ts_dt"].dt.tz_convert(HK_TZ)
    )
    protocol_keys = [
        "bet_id", "ts", "bet_ts", "player_id", "casino_player_id", "table_id",
        "position_idx", "session_id", "visit_avg_bet", "is_known_player",
    ]
    out = pd.DataFrame(index=df.index)
    out["ts"] = _format_ts_hk_iso(ts_ser).replace("NaT", None)
    for k in ["bet_id", "bet_ts", "player_id", "table_id", "position_idx", "session_id", "visit_avg_bet"]:
        out[k] = df[k] if k in df.columns else None
    if "bet_ts" in df.columns:
        bet_ts_dt = pd.to_datetime(out["bet_ts"], errors="coerce")
        if hasattr(bet_ts_dt, "dt"):
            b = bet_ts_dt.dt.tz_localize(HK_TZ, ambiguous="NaT") if bet_ts_dt.dt.tz is None else bet_ts_dt.dt.tz_convert(HK_TZ)
            out["bet_ts"] = _format_ts_hk_iso(b).replace("NaT", None)
        else:
            out["bet_ts"] = out["bet_ts"]
    if "casino_player_id" in df.columns:
        out["casino_player_id"] = df["casino_player_id"].apply(
            lambda v: None if (v is None or pd.isna(v)) else (str(v).strip() or None)
        )
    else:
        out["casino_player_id"] = None
    out["is_known_player"] = df["is_rated_obs"].fillna(0).astype(int) if "is_rated_obs" in df.columns else 0
    out = out[protocol_keys]
    out = out.replace({np.nan: None, np.inf: None, -np.inf: None})
    records = out.to_dict(orient="records")
    for r in records:
        for key in ("bet_id", "session_id"):
            if key in r and r[key] is not None:
                try:
                    r[key] = int(r[key])
                except (TypeError, ValueError):
                    pass
    return records


def _query_validation_df(ts_param=None, bet_id_param=None, bet_ids_param=None, default_24h=False):
    """Return validation_results DataFrame with filters. If default_24h and no ts/bet_id/bet_ids => last 24h."""
    with get_db_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM validation_results", conn)
    if df.empty:
        return df
    df["validated_at"] = pd.to_datetime(df["validated_at"], errors="coerce")
    df = df.dropna(subset=["validated_at"]).sort_values("validated_at")
    if bet_ids_param:
        try:
            ids = [s.strip() for s in str(bet_ids_param).split(",") if s.strip()]
            df = df[df["bet_id"].astype(str).isin(ids)]
        except Exception:
            pass
    elif bet_id_param:
        try:
            df = df[df["bet_id"].astype(str) == str(bet_id_param)]
        except Exception:
            pass
    if ts_param:
        try:
            ts_dt = pd.to_datetime(ts_param)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.tz_localize(HK_TZ)
            else:
                ts_dt = ts_dt.tz_convert(HK_TZ)
            df = df[df["validated_at"] > ts_dt]
        except Exception:
            pass
    elif default_24h and not bet_id_param and not bet_ids_param:
        cutoff = _validation_24h_cutoff()
        df = df[df["validated_at"] > cutoff]
    return df


def _validation_to_protocol_records(df):
    """Shape validation to ML_API_PROTOCOL.md: TP as string; casino_player_id from DB when present; bet_id string; timestamps +08:00."""
    if df.empty:
        return []
    out = df[["alert_ts", "player_id", "bet_id", "gap_start", "result", "validated_at", "reason", "bet_ts"]].rename(columns={
        "alert_ts": "ts",
        "gap_start": "walkaway_ts",
        "validated_at": "sync_ts",
    }).copy()
    out["TP"] = out["result"].apply(lambda x: "TP" if x in (1, True, 1.0) else "FP")
    out = out.drop(columns=["result"], errors="ignore")
    if "casino_player_id" in df.columns:
        out["casino_player_id"] = df["casino_player_id"].apply(
            lambda v: None if (v is None or pd.isna(v)) else (str(v).strip() or None)
        )
    else:
        out["casino_player_id"] = None
    out["bet_id"] = out["bet_id"].astype(str)
    for col in ["ts", "walkaway_ts", "sync_ts", "bet_ts"]:
        dt_col = pd.to_datetime(out[col], errors="coerce")
        if getattr(dt_col.dt, "tz", None) is None:
            dt_col = dt_col.dt.tz_localize(HK_TZ, ambiguous="NaT")
        else:
            dt_col = dt_col.dt.tz_convert(HK_TZ)
        out[col] = _format_ts_hk_iso(dt_col).replace("NaT", None)
    out = out[["ts", "player_id", "casino_player_id", "bet_id", "walkaway_ts", "TP", "sync_ts", "reason", "bet_ts"]]
    out = out.replace({np.nan: None, np.inf: None, -np.inf: None})
    return out.to_dict(orient="records")


@app.route("/alerts", methods=["GET"])
def ml_alerts():
    """GET /alerts per doc/ML_API_PROTOCOL.md: ts (after), limit (when ts absent); default last 24h; protocol fields only."""
    ts_param = request.args.get("ts")
    limit_param = request.args.get("limit")
    df = _query_alerts_df(ts_param=ts_param, limit_param=limit_param, default_24h=True)
    records = _alerts_to_protocol_records(df)
    resp = jsonify({"alerts": records})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/validation", methods=["GET"])
def ml_validation():
    """GET /validation per doc/ML_API_PROTOCOL.md: ts, bet_id, bet_ids; default last 24h; protocol fields only."""
    ts_param = request.args.get("ts")
    bet_id_param = request.args.get("bet_id")
    bet_ids_param = request.args.get("bet_ids")
    df = _query_validation_df(ts_param=ts_param, bet_id_param=bet_id_param, bet_ids_param=bet_ids_param, default_24h=True)
    records = _validation_to_protocol_records(df)
    resp = jsonify({"results": records})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# --- get_validation endpoint (legacy: full result column, no 24h default) ---
@app.route("/get_validation", methods=["GET"])
def get_validation():
    start = datetime.now()
    try:
        df = _query_validation_df(
            ts_param=request.args.get("ts"),
            bet_id_param=request.args.get("bet_id"),
            bet_ids_param=request.args.get("bet_ids"),
            default_24h=False,
        )
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})
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
    """Legacy: full alert rows. For protocol use GET /alerts."""
    ts = request.args.get("ts")
    start = datetime.now()
    try:
        # Legacy: no 24h default when ts absent; limit not applied
        df = _query_alerts_df(ts_param=ts, limit_param=None, default_24h=False)
        if df.empty:
            return jsonify({"alerts": []})
        df["ts"] = (
            df["ts_dt"].dt.tz_localize(HK_TZ, ambiguous="NaT", nonexistent="shift_forward")
            if df["ts_dt"].dt.tz is None
            else df["ts_dt"].dt.tz_convert(HK_TZ)
        )
        df["ts"] = df["ts"].dt.floor("s").dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        df_out = df.drop(columns=["ts_dt"], errors="ignore").replace({np.nan: None, np.inf: None, -np.inf: None})
        alerts = df_out.to_dict(orient="records")
    except Exception as e:
        return jsonify({"alerts": [], "error": str(e)})
    print(f"[api] get_alerts: {len(alerts)} rows in {(datetime.now()-start).total_seconds():.3f}s")
    resp = jsonify({"alerts": alerts})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

if __name__ == "__main__":
    import os
    port = int(os.environ.get("ML_API_PORT", "8001"))
    print(f" * SQLite Path: {STATE_DB_PATH}")
    print(f" * ML API: http://localhost:{port}/alerts, http://localhost:{port}/validation")
    app.run(host="0.0.0.0", port=port, debug=True)
