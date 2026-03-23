"""
Deploy entry: scorer loop + validator loop + Flask (GET /alerts, GET /validation).
Set STATE_DB_PATH and MODEL_DIR via env (or .env) before importing walkaway_ml.
"""
from __future__ import annotations

import logging
import os
import sys
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Deploy root: package/deploy/
DEPLOY_ROOT = Path(__file__).resolve().parent

# Load .env from deploy root so CH_* etc. are set before any walkaway_ml import
from dotenv import load_dotenv  # noqa: E402
_env_path = DEPLOY_ROOT / ".env"
if not _env_path.exists():
    _env_no_dot = DEPLOY_ROOT / "env"
    if _env_no_dot.exists():
        sys.exit(
            "[deploy] The config file must be named exactly .env (with a leading dot), not 'env'.\n"
            "You may have renamed .env.example to 'env' by mistake. Fix it:\n"
            "  Windows (cmd):     ren env .env\n"
            "  Windows (PowerShell): Rename-Item env .env\n"
            "  Linux / Mac:       mv env .env"
        )
    sys.exit(
        f"[deploy] .env not found at {_env_path}. Copy .env.example to .env and set CH_USER, CH_PASS.\n"
        "The filename must be exactly .env (including the leading dot)."
    )
load_dotenv(_env_path)
os.environ.setdefault("STATE_DB_PATH", str(DEPLOY_ROOT / "local_state" / "state.db"))
os.environ.setdefault("MODEL_DIR", str(DEPLOY_ROOT / "models"))

# Require ClickHouse credentials (fail fast)
if not os.environ.get("CH_USER") or not os.environ.get("CH_PASS"):
    sys.exit(
        "[deploy] CH_USER and CH_PASS must be set in .env. Edit .env and set your ClickHouse username and password."
    )

# Require feature_spec.yaml in model dir (fail fast)
_model_dir = Path(os.environ["MODEL_DIR"])
_feature_spec_path = _model_dir / "feature_spec.yaml"
if not _feature_spec_path.exists():
    sys.exit(
        f"[deploy] feature_spec.yaml not found at {_feature_spec_path}. Ensure the deploy package includes models/feature_spec.yaml."
    )

# DATA_DIR: profile + canonical mapping (scorer reads/writes here in deploy)
_data_dir = DEPLOY_ROOT / "data"
_data_dir.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(_data_dir)

# Now safe to import walkaway_ml (uses env for paths)
from walkaway_ml import config as _config  # noqa: E402
from walkaway_ml.scorer import run_scorer_loop  # noqa: E402
from walkaway_ml.validator import run_validator_loop  # noqa: E402

# Scorer and validator logs: always include timestamp in deployment console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from flask import Flask, request, jsonify  # noqa: E402

HK_TZ = ZoneInfo(_config.HK_TZ)
STATE_DB_PATH = Path(os.environ["STATE_DB_PATH"])
STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _format_ts_hk_iso(series):
    """Format datetime series as ISO with +08:00 offset (ML_API_PROTOCOL: HK timezone)."""
    s = series.dt.floor("s").dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return s.str.replace(r"(\+|-)(\d{2})(\d{2})$", r"\1\2:\3", regex=True)


def _alerts_1h_cutoff():
    return datetime.now(HK_TZ) - timedelta(hours=1)


def _validation_1h_cutoff():
    return datetime.now(HK_TZ) - timedelta(hours=1)


def _query_alerts_df(ts_param=None, limit_param=None, default_1h=False):
    """Per ML_API_PROTOCOL: limit only used when ts is absent."""
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
    elif default_1h:
        df = df[df["ts_dt"] > _alerts_1h_cutoff()]
    if limit_param is not None and not ts_param:
        try:
            limit = int(limit_param)
            if limit > 0:
                df = df.tail(limit)
        except (ValueError, TypeError):
            pass
    return df


def _alerts_to_protocol_records(df):
    """Per ML_API_PROTOCOL: protocol fields only; timestamps +08:00; bet_id/session_id int when possible."""
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


def _query_validation_df(ts_param=None, bet_id_param=None, bet_ids_param=None, default_1h=False):
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
    elif default_1h and not bet_id_param and not bet_ids_param:
        df = df[df["validated_at"] > _validation_1h_cutoff()]
    return df


def _validation_to_protocol_records(df):
    """Per ML_API_PROTOCOL: bet_id string, timestamps +08:00."""
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


app = Flask(__name__)


@app.route("/alerts", methods=["GET"])
def alerts():
    ts_param = request.args.get("ts")
    limit_param = request.args.get("limit")
    df = _query_alerts_df(ts_param=ts_param, limit_param=limit_param, default_1h=True)
    records = _alerts_to_protocol_records(df)
    resp = jsonify({"alerts": records})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/validation", methods=["GET"])
def validation():
    ts_param = request.args.get("ts")
    bet_id_param = request.args.get("bet_id")
    bet_ids_param = request.args.get("bet_ids")
    df = _query_validation_df(ts_param=ts_param, bet_id_param=bet_id_param, bet_ids_param=bet_ids_param, default_1h=True)
    records = _validation_to_protocol_records(df)
    resp = jsonify({"results": records})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _run_scorer():
    run_scorer_loop(
        interval_seconds=getattr(_config, "SCORER_POLL_INTERVAL_SECONDS", 45),
        lookback_hours=getattr(_config, "SCORER_LOOKBACK_HOURS", 8),
        model_dir=None,
        once=False,
    )


def _run_validator():
    run_validator_loop(
        interval_seconds=getattr(_config, "VALIDATOR_INTERVAL_SECONDS", 60),
        once=False,
        force_finalize=False,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("ML_API_PORT", "8001")))
    scorer_interval = getattr(_config, "SCORER_POLL_INTERVAL_SECONDS", 45)
    validator_interval = getattr(_config, "VALIDATOR_INTERVAL_SECONDS", 60)

    print(f"[deploy] STATE_DB_PATH={STATE_DB_PATH}")
    print(f"[deploy] MODEL_DIR={os.environ.get('MODEL_DIR')}")
    print(f"[deploy] Scorer running in background (poll every {scorer_interval}s)")
    print(f"[deploy] Validator running in background (poll every {validator_interval}s)")
    print(f"[deploy] ML API: http://0.0.0.0:{port}/alerts, http://0.0.0.0:{port}/validation")

    # Start scorer and validator in daemon threads
    scorer_thread = threading.Thread(target=_run_scorer, daemon=True)
    validator_thread = threading.Thread(target=_run_validator, daemon=True)
    scorer_thread.start()
    validator_thread.start()

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
