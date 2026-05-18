"""Flask ML API: ``/alerts``, ``/validation``, ``/health``, ``/predictions`` (SQLite)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from zoneinfo import ZoneInfo

from trainer_hightier.serving.prediction_log import init_prediction_log_db
from trainer_hightier.serving.runtime_config import HK_TZ, PREDICTION_LOG_DB_PATH, STATE_DB_PATH
from trainer_hightier.serving.state_db import apply_sqlite_serving_pragmas, init_state_db

logger = logging.getLogger(__name__)

app = Flask(__name__)


def _init_state_and_prediction_log_dbs() -> None:
    init_state_db(STATE_DB_PATH)
    init_prediction_log_db(PREDICTION_LOG_DB_PATH)


def get_db_conn() -> sqlite3.Connection:
    _init_state_and_prediction_log_dbs()
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    apply_sqlite_serving_pragmas(conn)
    return conn


def _format_ts_hk_iso(series: pd.Series) -> pd.Series:
    s = series.dt.floor("s").dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return s.str.replace(r"(\+|-)(\d{2})(\d{2})$", r"\1\2:\3", regex=True)


def _alerts_24h_cutoff() -> datetime:
    return datetime.now(ZoneInfo(HK_TZ)) - timedelta(hours=24)


def _query_alerts_df(ts_param=None, limit_param=None, default_24h: bool = False) -> pd.DataFrame:
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
                ts_dt = ts_dt.tz_localize(ZoneInfo(HK_TZ))
            else:
                ts_dt = ts_dt.tz_convert(ZoneInfo(HK_TZ))
            df = df[df["ts_dt"] > ts_dt]
        except Exception:
            pass
    elif default_24h:
        df = df[df["ts_dt"] > _alerts_24h_cutoff()]
    if limit_param is not None and not ts_param:
        try:
            lim = int(limit_param)
            if lim > 0:
                df = df.tail(lim)
        except (TypeError, ValueError):
            pass
    return df


def _alerts_to_protocol_records(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    df = df.copy()
    ts_ser = (
        df["ts_dt"].dt.tz_localize(ZoneInfo(HK_TZ), ambiguous="NaT", nonexistent="shift_forward")
        if df["ts_dt"].dt.tz is None
        else df["ts_dt"].dt.tz_convert(ZoneInfo(HK_TZ))
    )
    protocol_keys = [
        "bet_id",
        "ts",
        "bet_ts",
        "player_id",
        "casino_player_id",
        "table_id",
        "position_idx",
        "session_id",
        "visit_avg_bet",
        "is_known_player",
    ]
    out = pd.DataFrame(index=df.index)
    out["ts"] = _format_ts_hk_iso(ts_ser).replace("NaT", None)
    for k in ["bet_id", "bet_ts", "player_id", "table_id", "position_idx", "session_id", "visit_avg_bet"]:
        out[k] = df[k] if k in df.columns else None
    if "bet_ts" in df.columns:
        bet_ts_dt = pd.to_datetime(out["bet_ts"], errors="coerce")
        if hasattr(bet_ts_dt, "dt"):
            b = (
                bet_ts_dt.dt.tz_localize(ZoneInfo(HK_TZ), ambiguous="NaT")
                if bet_ts_dt.dt.tz is None
                else bet_ts_dt.dt.tz_convert(ZoneInfo(HK_TZ))
            )
            out["bet_ts"] = _format_ts_hk_iso(b).replace("NaT", None)
    if "casino_player_id" in df.columns:
        out["casino_player_id"] = df["casino_player_id"].apply(
            lambda v: None if (v is None or pd.isna(v)) else (str(v).strip() or None)
        )
    else:
        out["casino_player_id"] = None
    out["is_known_player"] = (
        df["is_rated_obs"].fillna(0).astype(int) if "is_rated_obs" in df.columns else 0
    )
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


def _query_validation_df(
    ts_param=None,
    bet_id_param=None,
    bet_ids_param=None,
    default_24h: bool = False,
) -> pd.DataFrame:
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
                ts_dt = ts_dt.tz_localize(ZoneInfo(HK_TZ))
            else:
                ts_dt = ts_dt.tz_convert(ZoneInfo(HK_TZ))
            df = df[df["validated_at"] > ts_dt]
        except Exception:
            pass
    elif default_24h and not bet_id_param and not bet_ids_param:
        df = df[df["validated_at"] > (datetime.now(ZoneInfo(HK_TZ)) - timedelta(hours=24))]
    return df


def _validation_to_protocol_records(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    df = df.copy()
    if "bet_ts" not in df.columns:
        df["bet_ts"] = None
    base_cols = [
        "alert_ts",
        "player_id",
        "bet_id",
        "gap_start",
        "result",
        "validated_at",
        "reason",
        "bet_ts",
    ]
    for c in base_cols:
        if c not in df.columns:
            df[c] = None
    out = df[base_cols].rename(
        columns={
            "alert_ts": "ts",
            "gap_start": "walkaway_ts",
            "validated_at": "sync_ts",
        }
    ).copy()
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
            dt_col = dt_col.dt.tz_localize(ZoneInfo(HK_TZ), ambiguous="NaT")
        else:
            dt_col = dt_col.dt.tz_convert(ZoneInfo(HK_TZ))
        out[col] = _format_ts_hk_iso(dt_col).replace("NaT", None)
    out = out[
        ["ts", "player_id", "casino_player_id", "bet_id", "walkaway_ts", "TP", "sync_ts", "reason", "bet_ts"]
    ]
    out = out.replace({np.nan: None, np.inf: None, -np.inf: None})
    return out.to_dict(orient="records")


def _predictions_24h_cutoff() -> datetime:
    return datetime.now(ZoneInfo(HK_TZ)) - timedelta(hours=24)


def _query_predictions_df(
    ts_param: str | None = None,
    limit_param: str | None = None,
    *,
    default_24h: bool = False,
) -> pd.DataFrame:
    if PREDICTION_LOG_DB_PATH is None:
        return pd.DataFrame()
    plp = Path(PREDICTION_LOG_DB_PATH)
    if not plp.is_file():
        return pd.DataFrame()
    with sqlite3.connect(plp) as conn:
        df = pd.read_sql_query("SELECT * FROM prediction_log", conn)
    if df.empty:
        return df
    df["scored_dt"] = pd.to_datetime(df["scored_at"], errors="coerce")
    df = df.dropna(subset=["scored_dt"]).sort_values("scored_dt")
    if ts_param:
        try:
            ts_dt = pd.to_datetime(ts_param)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.tz_localize(ZoneInfo(HK_TZ))
            else:
                ts_dt = ts_dt.tz_convert(ZoneInfo(HK_TZ))
            df = df[df["scored_dt"] > ts_dt]
        except Exception:
            pass
    elif default_24h:
        df = df[df["scored_dt"] > _predictions_24h_cutoff()]
    if limit_param is not None and not ts_param:
        try:
            lim = int(limit_param)
            if lim > 0:
                df = df.tail(lim)
        except (TypeError, ValueError):
            pass
    return df


@app.route("/health", methods=["GET"])
def health():
    ok = Path(STATE_DB_PATH).is_file()
    body = {
        "ok": bool(ok),
        "state_db": str(STATE_DB_PATH),
        "prediction_log_db": str(PREDICTION_LOG_DB_PATH) if PREDICTION_LOG_DB_PATH else None,
    }
    code = 200 if ok else 503
    resp = jsonify(body)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, code


@app.route("/alerts", methods=["GET"])
def ml_alerts():
    ts_param = request.args.get("ts")
    limit_param = request.args.get("limit")
    df = _query_alerts_df(ts_param=ts_param, limit_param=limit_param, default_24h=True)
    records = _alerts_to_protocol_records(df)
    resp = jsonify({"alerts": records})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/validation", methods=["GET"])
def ml_validation():
    ts_param = request.args.get("ts")
    bet_id_param = request.args.get("bet_id")
    bet_ids_param = request.args.get("bet_ids")
    df = _query_validation_df(
        ts_param=ts_param,
        bet_id_param=bet_id_param,
        bet_ids_param=bet_ids_param,
        default_24h=True,
    )
    records = _validation_to_protocol_records(df)
    resp = jsonify({"results": records})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/predictions", methods=["GET"])
def ml_predictions():
    """Read-only recent rows from ``prediction_log`` (all scored bets, not only alerts)."""
    if PREDICTION_LOG_DB_PATH is None:
        resp = jsonify({"predictions": [], "enabled": False})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    ts_param = request.args.get("ts")
    limit_param = request.args.get("limit")
    df = _query_predictions_df(ts_param=ts_param, limit_param=limit_param, default_24h=True)
    if df.empty:
        resp = jsonify({"predictions": [], "enabled": True})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp
    cols = [
        "prediction_id",
        "scored_at",
        "bet_id",
        "player_id",
        "canonical_id",
        "model_version",
        "score",
        "margin",
        "is_alert",
        "is_rated_obs",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    out = df[cols].replace({np.nan: None, np.inf: None, -np.inf: None})
    records = out.to_dict(orient="records")
    resp = jsonify({"predictions": records, "enabled": True})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO)
    pr = argparse.ArgumentParser(description="trainer_hightier ML API server")
    pr.add_argument("--host", default="127.0.0.1")
    pr.add_argument("--port", type=int, default=8001)
    args = pr.parse_args(argv)
    _init_state_and_prediction_log_dbs()
    app.run(host=args.host, port=int(args.port), threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
