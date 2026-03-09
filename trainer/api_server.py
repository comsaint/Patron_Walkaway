import hashlib
import io
import json
import sqlite3
import threading
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify, send_from_directory, abort
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from werkzeug.security import safe_join
import config
from pathlib import Path

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / "frontend"
MODEL_DIR = BASE_DIR / "models"

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
    # R2323: safe_join prevents path traversal (e.g. "../../etc/passwd").
    safe = safe_join(str(FRONTEND_DIR), filename)
    if safe is None or not filename.endswith(".js"):
        abort(404)
    target = Path(safe)
    if not target.exists():
        abort(404)
    return send_from_directory(FRONTEND_DIR, target.name)

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

# ── Model artifact cache (Step 9) ─────────────────────────────────────────────
# Artifacts are loaded lazily on the first request and reloaded automatically
# whenever model_version changes (i.e. after trainer.py produces a new bundle).
_artifacts_cache: dict = {}
_cached_model_version: str = ""
_artifacts_lock = threading.Lock()
_MAX_SCORE_ROWS = 10_000


def _load_artifacts() -> dict | None:
    """Load model artifacts from MODEL_DIR (v10 single rated model, DEC-021).

    Returns a dict with keys: rated, feature_list, reason_code_map,
    model_version, training_metrics, rated_explainer.
    Returns None when no model file is found.
    """
    try:
        import joblib  # type: ignore[import]
    except ImportError:
        print("[api] joblib not installed — /score endpoint unavailable")
        return None

    model_path = MODEL_DIR / "model.pkl"        # v10 single rated model
    rated_path = MODEL_DIR / "rated_model.pkl"
    legacy_path = MODEL_DIR / "walkaway_model.pkl"
    feature_list_path = MODEL_DIR / "feature_list.json"
    reason_map_path = MODEL_DIR / "reason_code_map.json"
    version_path = MODEL_DIR / "model_version"

    arts: dict = {
        "rated": None,
        "feature_list": [],
        "reason_code_map": {},
        "model_version": "unknown",
    }

    if version_path.exists():
        arts["model_version"] = version_path.read_text(encoding="utf-8").strip()

    if feature_list_path.exists():
        with feature_list_path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
            arts["feature_list"] = [
                (entry["name"] if isinstance(entry, dict) else str(entry)) for entry in raw
            ]

    if reason_map_path.exists():
        with reason_map_path.open(encoding="utf-8") as fh:
            arts["reason_code_map"] = json.load(fh)

    # ── Read each pkl once, verify sha256, cache raw bytes for in-memory
    #    deserialization — avoids double I/O (R48, R58) ──
    # Priority: model.pkl (v10) > rated_model.pkl > legacy walkaway_model.pkl
    _pkl_raw: dict = {}
    for pkl_path in [model_path, rated_path, legacy_path]:
        if pkl_path.exists():
            raw = pkl_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            print(f"[api] {pkl_path.name} sha256={digest}")
            _pkl_raw[pkl_path] = raw

    def _load_model_pkl(rb: dict, src_name: str) -> None:
        arts["rated"] = {"model": rb["model"], "threshold": float(rb.get("threshold", 0.5))}
        arts["training_metrics"] = rb.get("metrics", {})
        if not arts["feature_list"]:
            arts["feature_list"] = rb.get("features", [])
        try:
            import shap  # type: ignore[import]
            arts["rated_explainer"] = shap.TreeExplainer(rb["model"])
        except Exception as exc:
            print(f"[api] SHAP explainer pre-build failed ({src_name}): {exc}")
            arts["rated_explainer"] = None

    def _load_training_metrics_from_file() -> None:
        """Set arts[\"training_metrics\"] from MODEL_DIR/training_metrics.json (PLAN § model_info).
        If file missing, read fails, or root value is not a JSON object, use {}."""
        metrics_path = MODEL_DIR / "training_metrics.json"
        arts["training_metrics"] = {}
        try:
            if metrics_path.exists():
                with metrics_path.open(encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    print("[api] training_metrics.json root is not an object; using {}")
                    arts["training_metrics"] = {}
                else:
                    arts["training_metrics"] = data
        except Exception as exc:
            print(f"[api] training_metrics.json read failed: {exc}")

    if model_path in _pkl_raw:
        rb = joblib.load(io.BytesIO(_pkl_raw[model_path]))
        _load_model_pkl(rb, "model.pkl")
        _load_training_metrics_from_file()
        return arts

    if rated_path in _pkl_raw:
        rb = joblib.load(io.BytesIO(_pkl_raw[rated_path]))
        _load_model_pkl(rb, "rated_model.pkl")
        _load_training_metrics_from_file()
        return arts

    if legacy_path in _pkl_raw:
        bundle = joblib.load(io.BytesIO(_pkl_raw[legacy_path]))
        _load_model_pkl(bundle, "walkaway_model.pkl")
        _load_training_metrics_from_file()
        return arts

    return None


def _get_artifacts() -> dict | None:
    """Return cached artifacts, reloading when model_version file changes.

    Cache reads and writes are protected by _artifacts_lock (R52) so that
    concurrent Flask worker threads cannot observe a partially-loaded bundle.
    """
    global _artifacts_cache, _cached_model_version
    version_path = MODEL_DIR / "model_version"
    with _artifacts_lock:
        current_version = (
            version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""
        )
        if not _artifacts_cache or current_version != _cached_model_version:
            loaded = _load_artifacts()
            if loaded is not None:
                _artifacts_cache = loaded
                _cached_model_version = current_version
        return _artifacts_cache or None


def _compute_shap_reason_codes_batch(
    explainer: object,
    X: np.ndarray,
    feature_list: list,
    reason_code_map: dict,
    top_k: int = 3,
) -> list:
    """Compute per-row SHAP-based reason codes for a batch of observations.

    Accepts a pre-built shap.TreeExplainer from the artifact cache (R56) so that
    the explainer is not rebuilt on every call.  Returns a list (length =
    X.shape[0]) where each element is a list of up to top_k reason-code strings.
    Falls back to empty lists on any error so that scoring is never blocked by an
    explainability failure.
    """
    n_rows = X.shape[0] if hasattr(X, "shape") else len(X)
    if explainer is None:
        return [[] for _ in range(n_rows)]
    try:
        sv = explainer.shap_values(X)  # type: ignore[attr-defined]
        sv_class1: np.ndarray = sv[1] if isinstance(sv, list) else sv
        results = []
        for row_sv in sv_class1:
            top_idx = np.argsort(np.abs(row_sv))[::-1][:top_k]
            codes = [reason_code_map.get(feature_list[i], feature_list[i]) for i in top_idx]
            results.append(codes)
        return results
    except Exception as exc:
        print(f"[api] SHAP reason codes failed: {exc}")
        return [[] for _ in range(n_rows)]


# ── New model-API endpoints ────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Returns {"status": "ok", "model_version": <current_version>, "model_loaded": <bool>}.

    Always returns 200.  model_version == "no_model" when no artifacts; model_loaded
    is true when arts[\"rated\"] is present (PLAN § api_server 對齊 model_api_protocol).
    """
    arts = _get_artifacts()
    version = arts["model_version"] if arts else "no_model"
    model_loaded = bool(arts and arts.get("rated") is not None)
    resp = jsonify({"status": "ok", "model_version": version, "model_loaded": model_loaded})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/model_info", methods=["GET"])
def model_info():
    """Returns model metadata.

    Response schema:
      {
        "model_type":        "rated" | "unavailable",
        "model_version":     str,
        "features":          [str, ...],
        "training_metrics":  {}  (from training_metrics.json; missing/fail/non-object → {})
      }

    503 when no model artifacts are present.
    """
    arts = _get_artifacts()
    if arts is None:
        return jsonify({"error": "model not ready"}), 503

    model_type = "rated" if arts["rated"] else "unavailable"
    raw_metrics = arts.get("training_metrics")
    metrics: dict = raw_metrics if isinstance(raw_metrics, dict) else {}

    resp = jsonify(
        {
            "model_type": model_type,
            "model_version": arts["model_version"],
            "features": arts["feature_list"],
            "training_metrics": metrics,
        }
    )
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


_RESERVED_KEYS = frozenset({"is_rated"})


@app.route("/score", methods=["POST"])
def score():
    """Stateless batch scoring endpoint (PLAN § api_server 對齊 model_api_protocol).

    Request: single object ``{"rows": [ {...}, ... ]}``.
    Each row must contain every feature in feature_list + ``bet_id``.
    ``is_rated`` (optional, default false) is reserved. Other keys not in feature_list
    are pass-through and echoed in response scores[i].

    Response: ``{"model_version": str, "threshold": float, "scores": [ {...}, ... ]}``.
    scores[i] contains bet_id, score, alert, and pass-through keys. reason_codes not returned.

    Errors:
      400 — Malformed JSON or missing 'rows'; rows not array; empty rows.
      422 — Batch over limit; missing features; invalid feature types.
      503 — No model (model not ready).
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Malformed JSON or missing 'rows'"}), 400
    if not isinstance(body, dict):
        return jsonify({"error": "Malformed JSON or missing 'rows'"}), 400
    if "rows" not in body:
        return jsonify({"error": "Malformed JSON or missing 'rows'"}), 400
    rows = body["rows"]
    if not isinstance(rows, list):
        return jsonify({"error": "Malformed JSON or missing 'rows'"}), 400
    if len(rows) == 0:
        return jsonify({"error": "empty rows"}), 400
    if len(rows) > _MAX_SCORE_ROWS:
        return jsonify({"error": "Batch size exceeds limit", "limit": _MAX_SCORE_ROWS}), 422

    arts = _get_artifacts()
    if arts is None:
        return jsonify({"error": "model not ready"}), 503

    feature_list = arts["feature_list"]
    version = arts["model_version"]
    _ = arts.get("rated_explainer")  # R56: score uses cached explainer when reason_codes are returned
    # Reject when feature_list is empty (R54) before any predict path
    if not feature_list:
        return jsonify({"error": "model not ready"}), 503

    required = list(feature_list) + ["bet_id"]

    # ── 422: missing required (feature_list + bet_id) ───────────────────────────
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return jsonify({"error": "Malformed JSON or missing 'rows'"}), 400
        missing = [k for k in required if k not in row]
        if missing:
            extra = [k for k in row if k not in feature_list and k not in _RESERVED_KEYS]
            return jsonify({"error": "missing features", "missing": missing, "extra": extra}), 422

    # ── 422: invalid feature types (non int/float/bool) ────────────────────────
    for i, row in enumerate(rows):
        bad = [
            k for k, v in row.items()
            if k in feature_list and not isinstance(v, (int, float, bool))
        ]
        if bad:
            return jsonify({"error": "invalid feature types", "missing": [], "extra": bad}), 422

    # ── Pass-through: keys not in feature_list and not reserved ──────────────────
    pass_through_keys: set = set()
    for row in rows:
        for k in row:
            if k not in feature_list and k not in _RESERVED_KEYS:
                pass_through_keys.add(k)
    if pass_through_keys:
        print(f"[api] Pass-through keys (not in feature list): {', '.join(sorted(pass_through_keys))}")

    # ── Build DataFrame ────────────────────────────────────────────────────────
    df = pd.DataFrame(rows)
    if feature_list:
        df[feature_list] = df[feature_list].fillna(0)
    if "is_rated" not in df.columns:
        df["is_rated"] = False
    df["is_rated"] = df["is_rated"].fillna(False).astype(bool)

    model_info_d = arts.get("rated")
    threshold_val = float(model_info_d["threshold"]) if model_info_d else 0.0
    is_rated_arr = df["is_rated"].to_numpy(dtype=bool)

    output: list = [None] * len(rows)
    if model_info_d is None:
        lgbm_model = None
        proba_arr = None
    else:
        lgbm_model = model_info_d["model"]
        threshold_val = model_info_d["threshold"]
        X = df[feature_list].values.astype(float)
        proba_arr = lgbm_model.predict_proba(X)[:, 1]

    for i in range(len(rows)):
        row = rows[i]
        pass_through = {k: row[k] for k in row if k not in feature_list and k not in _RESERVED_KEYS}
        if lgbm_model is None:
            out_i = {**pass_through, "score": None, "alert": False}
        else:
            score_val = float(proba_arr[i])
            out_i = {
                **pass_through,
                "score": round(score_val, 4),
                "alert": bool(score_val >= threshold_val and is_rated_arr[i]),
            }
        output[i] = out_i

    resp = jsonify({"model_version": version, "threshold": threshold_val, "scores": output})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


if __name__ == "__main__":
    print(f" * SQLite Path: {STATE_DB_PATH}")
    app.run(host="0.0.0.0", port=8000, debug=True)
