"""
Deploy entry: scorer loop + validator loop + Flask (GET /alerts, GET /validation).
Set STATE_DB_PATH and MODEL_DIR via env (or .env) before importing walkaway_ml.
Optional startup flush: --flush-all | --flush-state | --flush-prediction (mutually exclusive; default none).
"""
from __future__ import annotations

import argparse
from collections import deque
import logging
import math
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
# Before importing walkaway_ml.config: default prediction log next to state (avoids wheel site-packages path).
os.environ.setdefault(
    "PREDICTION_LOG_DB_PATH",
    str(DEPLOY_ROOT / "local_state" / "prediction_log.db"),
)

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

# Scorer and validator logs: always include timestamp in deployment console.
# Default INFO; can be overridden by DEPLOY_LOG_LEVEL / LOGLEVEL.
_deploy_log_level_name = (
    (os.environ.get("DEPLOY_LOG_LEVEL") or os.environ.get("LOGLEVEL") or "INFO")
    .strip()
    .upper()
)
_deploy_log_level = getattr(logging, _deploy_log_level_name, logging.INFO)
logging.basicConfig(
    level=_deploy_log_level,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# `from walkaway_ml ...` runs `trainer.training.trainer` module init, which calls
# basicConfig(INFO) first; a second basicConfig is a no-op. Force level on root
# and any handlers already attached so DEPLOY_LOG_LEVEL / LOGLEVEL still apply.
_root_log = logging.getLogger()
_root_log.setLevel(_deploy_log_level)
for _h in _root_log.handlers:
    _h.setLevel(_deploy_log_level)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from flask import Flask, request, jsonify  # noqa: E402

HK_TZ = ZoneInfo(_config.HK_TZ)
STATE_DB_PATH = Path(os.environ["STATE_DB_PATH"])
STATE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_PERF_WINDOW_SIZE = 200
_API_STAGE_TIMINGS: dict[str, deque[float]] = {}


def _record_api_stage_timing(stage: str, seconds: float) -> None:
    bucket = _API_STAGE_TIMINGS.setdefault(stage, deque(maxlen=_PERF_WINDOW_SIZE))
    bucket.append(max(0.0, float(seconds)))


def _emit_api_perf_summary(stage_seconds: dict[str, float]) -> None:
    if not stage_seconds:
        return
    for stage, sec in stage_seconds.items():
        _record_api_stage_timing(stage, sec)
    top_stages = sorted(stage_seconds.items(), key=lambda x: x[1], reverse=True)[:2]
    parts = []
    for stage, sec in top_stages:
        hist = _API_STAGE_TIMINGS.get(stage)
        if not hist:
            continue
        arr = np.asarray(hist, dtype=float)
        p50 = float(np.percentile(arr, 50))
        p95 = float(np.percentile(arr, 95))
        parts.append(f"{stage}={sec:.3f}s (p50={p50:.3f}s, p95={p95:.3f}s, n={len(arr)})")
    if parts:
        logging.getLogger(__name__).debug("[api][perf] top_hotspots: %s", "; ".join(parts))


def _unlink_sqlite_bundle(db_path: Path) -> None:
    """Remove a SQLite file and its -wal / -shm siblings if present."""
    db_path = Path(db_path).resolve()
    log = logging.getLogger(__name__)
    for suffix in ("", "-wal", "-shm"):
        p = db_path.parent / (db_path.name + suffix) if suffix else db_path
        try:
            if p.is_file():
                p.unlink()
                log.warning("[deploy] flush removed %s", p)
        except OSError as exc:
            log.warning("[deploy] flush could not remove %s: %s", p, exc)


def flush_state_db_only() -> None:
    """Delete STATE_DB_PATH SQLite bundle only (does not touch PREDICTION_LOG_DB_PATH)."""
    _unlink_sqlite_bundle(STATE_DB_PATH)


def flush_prediction_log_db_only() -> None:
    """Delete PREDICTION_LOG_DB_PATH SQLite bundle only (does not touch STATE_DB_PATH)."""
    pl_raw = str(getattr(_config, "PREDICTION_LOG_DB_PATH", "") or "").strip()
    if not pl_raw:
        logging.getLogger(__name__).warning(
            "[deploy] flush prediction: PREDICTION_LOG_DB_PATH is empty; skipped"
        )
        return
    _unlink_sqlite_bundle(Path(pl_raw))


def flush_all_sqlite_bundles() -> None:
    """Delete STATE_DB_PATH and PREDICTION_LOG_DB_PATH bundles (same semantics as --flush-all)."""
    flush_state_db_only()
    flush_prediction_log_db_only()


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
    where_sql = []
    params: list[object] = []
    order_sql = " ORDER BY ts ASC"
    post_sort = False
    limit_sql = ""
    effective_limit: int | None = None

    if ts_param:
        try:
            ts_dt = pd.to_datetime(ts_param)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.tz_localize(HK_TZ)
            else:
                ts_dt = ts_dt.tz_convert(HK_TZ)
            where_sql.append("datetime(ts) > datetime(?)")
            params.append(ts_dt.isoformat())
        except Exception:
            pass
    elif default_1h:
        where_sql.append("datetime(ts) > datetime(?)")
        params.append(_alerts_1h_cutoff().isoformat())

    if limit_param is not None and not ts_param:
        try:
            limit = int(limit_param)
            if limit > 0:
                # Keep protocol semantics (tail N by ts) via DESC LIMIT then resort to ASC.
                effective_limit = limit
                order_sql = " ORDER BY ts DESC"
                post_sort = True
                limit_sql = " LIMIT ?"
                params.append(limit)
        except (ValueError, TypeError):
            pass

    where_clause = f" WHERE {' AND '.join(where_sql)}" if where_sql else ""
    query = f"SELECT * FROM alerts{where_clause}{order_sql}{limit_sql}"
    with get_db_conn() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return df
    df["ts_dt"] = pd.to_datetime(df["ts"], errors="coerce")
    df = df.dropna(subset=["ts_dt"]).sort_values("ts_dt")
    if post_sort and effective_limit is not None and effective_limit > 0:
        df = df.tail(effective_limit)
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
    where_sql = []
    params: list[object] = []

    if bet_ids_param:
        try:
            ids = [s.strip() for s in str(bet_ids_param).split(",") if s.strip()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                where_sql.append(f"CAST(bet_id AS TEXT) IN ({placeholders})")
                params.extend(ids)
        except Exception:
            pass
    elif bet_id_param:
        try:
            where_sql.append("CAST(bet_id AS TEXT) = ?")
            params.append(str(bet_id_param))
        except Exception:
            pass

    if ts_param:
        try:
            ts_dt = pd.to_datetime(ts_param)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.tz_localize(HK_TZ)
            else:
                ts_dt = ts_dt.tz_convert(HK_TZ)
            where_sql.append("datetime(validated_at) > datetime(?)")
            params.append(ts_dt.isoformat())
        except Exception:
            pass
    elif default_1h and not bet_id_param and not bet_ids_param:
        where_sql.append("datetime(validated_at) > datetime(?)")
        params.append(_validation_1h_cutoff().isoformat())

    where_clause = f" WHERE {' AND '.join(where_sql)}" if where_sql else ""
    query = f"SELECT * FROM validation_results{where_clause} ORDER BY validated_at ASC"
    with get_db_conn() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return df
    df["validated_at"] = pd.to_datetime(df["validated_at"], errors="coerce")
    df = df.dropna(subset=["validated_at"]).sort_values("validated_at")
    return df


def _validation_to_protocol_records(df):
    """Per ML_API_PROTOCOL: bet_id string, timestamps +08:00."""
    if df.empty:
        return []
    df = df.copy()
    if "bet_ts" not in df.columns:
        df["bet_ts"] = None
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
    stage_seconds: dict[str, float] = {}
    ts_param = request.args.get("ts")
    limit_param = request.args.get("limit")
    t_query = datetime.now().timestamp()
    df = _query_alerts_df(ts_param=ts_param, limit_param=limit_param, default_1h=True)
    stage_seconds["api_query_alerts"] = datetime.now().timestamp() - t_query
    t_transform = datetime.now().timestamp()
    records = _alerts_to_protocol_records(df)
    stage_seconds["api_transform_alerts"] = datetime.now().timestamp() - t_transform
    _emit_api_perf_summary(stage_seconds)
    resp = jsonify({"alerts": records})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/validation", methods=["GET"])
def validation():
    stage_seconds: dict[str, float] = {}
    ts_param = request.args.get("ts")
    bet_id_param = request.args.get("bet_id")
    bet_ids_param = request.args.get("bet_ids")
    t_query = datetime.now().timestamp()
    df = _query_validation_df(ts_param=ts_param, bet_id_param=bet_id_param, bet_ids_param=bet_ids_param, default_1h=True)
    stage_seconds["api_query_validation"] = datetime.now().timestamp() - t_query
    t_transform = datetime.now().timestamp()
    records = _validation_to_protocol_records(df)
    stage_seconds["api_transform_validation"] = datetime.now().timestamp() - t_transform
    _emit_api_perf_summary(stage_seconds)
    resp = jsonify({"results": records})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _deploy_validator_start_wait_timeout() -> float | None:
    """Seconds to wait for scorer's first cycle before starting validator.

    ``DEPLOY_VALIDATOR_START_TIMEOUT_SECONDS``: unset defaults to 600; ``0`` /
    ``0.0`` / empty / ``none`` / ``inf`` / ``infinite`` (case-insensitive) means
    wait indefinitely. A negative numeric value also means wait indefinitely.
    ``nan`` logs a warning and falls back to 600s. Other non-numeric strings log
    a warning and fall back to 600s.
    """
    raw = os.environ.get("DEPLOY_VALIDATOR_START_TIMEOUT_SECONDS")
    if raw is None:
        return 600.0
    s = raw.strip()
    if not s or s.lower() in ("0", "none", "inf", "infinite"):
        return None
    try:
        v = float(s)
    except ValueError:
        logging.getLogger(__name__).warning(
            "[deploy] Invalid DEPLOY_VALIDATOR_START_TIMEOUT_SECONDS=%r; using 600s",
            raw,
        )
        return 600.0
    if math.isnan(v):
        logging.getLogger(__name__).warning(
            "[deploy] DEPLOY_VALIDATOR_START_TIMEOUT_SECONDS is NaN (%r); using 600s",
            raw,
        )
        return 600.0
    if math.isinf(v):
        return None
    if v == 0.0:
        return None
    if v < 0:
        return None
    return v


def _run_scorer(first_cycle_done: threading.Event | None):
    run_scorer_loop(
        interval_seconds=getattr(_config, "SCORER_POLL_INTERVAL_SECONDS", 45),
        lookback_hours=getattr(_config, "SCORER_LOOKBACK_HOURS", 8),
        model_dir=None,
        once=False,
        first_cycle_done=first_cycle_done,
    )


def _run_validator_deferred(
    first_cycle_done: threading.Event,
    wait_timeout: float | None,
) -> None:
    log = logging.getLogger(__name__)
    if wait_timeout is None:
        first_cycle_done.wait()
    elif not first_cycle_done.wait(timeout=wait_timeout):
        log.warning(
            "[deploy] Scorer first cycle did not finish within %.1fs; starting validator anyway.",
            wait_timeout,
        )
    run_validator_loop(
        interval_seconds=getattr(_config, "VALIDATOR_INTERVAL_SECONDS", 60),
        once=False,
        force_finalize=False,
    )


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(
        description=(
            "Deploy: scorer loop + validator loop + Flask ML API (GET /alerts, GET /validation). "
            "Configure paths via env / .env (STATE_DB_PATH, MODEL_DIR, PREDICTION_LOG_DB_PATH, CH_*). "
            "Scoring payout-age window: SCORER_COLD_START_WINDOW_HOURS (optional; see trainer config). "
            "Flush flags run once before starting loops; default is no flush. Use at most one flush flag."
        ),
    )
    _flush_grp = _parser.add_mutually_exclusive_group()
    _flush_grp.add_argument(
        "--flush-all",
        action="store_true",
        help=(
            "Before start: delete SQLite bundles for STATE_DB_PATH and PREDICTION_LOG_DB_PATH "
            "(each: main file plus -wal/-shm if present). Prediction bundle skipped if path is empty."
        ),
    )
    _flush_grp.add_argument(
        "--flush-state",
        action="store_true",
        help=(
            "Before start: delete only the STATE_DB_PATH bundle; do not remove PREDICTION_LOG_DB_PATH."
        ),
    )
    _flush_grp.add_argument(
        "--flush-prediction",
        action="store_true",
        help=(
            "Before start: delete only the PREDICTION_LOG_DB_PATH bundle; do not remove STATE_DB_PATH."
        ),
    )
    _args, _unknown = _parser.parse_known_args()
    if _unknown:
        logging.getLogger(__name__).warning("[deploy] Ignoring unrecognized argv: %s", _unknown)

    if _args.flush_all:
        flush_all_sqlite_bundles()
    elif _args.flush_state:
        flush_state_db_only()
    elif _args.flush_prediction:
        flush_prediction_log_db_only()

    port = int(os.environ.get("PORT", os.environ.get("ML_API_PORT", "8001")))
    scorer_interval = getattr(_config, "SCORER_POLL_INTERVAL_SECONDS", 45)
    validator_interval = getattr(_config, "VALIDATOR_INTERVAL_SECONDS", 60)

    print(f"[deploy] STATE_DB_PATH={STATE_DB_PATH}")
    print(f"[deploy] MODEL_DIR={os.environ.get('MODEL_DIR')}")
    print(f"[deploy] Scorer running in background (poll every {scorer_interval}s)")
    _v_wait = _deploy_validator_start_wait_timeout()
    if _v_wait is None:
        print(
            f"[deploy] Validator starts after scorer's first cycle completes "
            f"(no timeout; poll every {validator_interval}s once started)"
        )
    else:
        print(
            f"[deploy] Validator starts after scorer's first cycle "
            f"(or after {_v_wait:.0f}s timeout; then poll every {validator_interval}s)"
        )
    print(f"[deploy] ML API: http://0.0.0.0:{port}/alerts, http://0.0.0.0:{port}/validation")

    scorer_first_cycle_done = threading.Event()
    scorer_thread = threading.Thread(
        target=_run_scorer,
        args=(scorer_first_cycle_done,),
        daemon=True,
    )
    validator_thread = threading.Thread(
        target=_run_validator_deferred,
        args=(scorer_first_cycle_done, _v_wait),
        daemon=True,
    )
    scorer_thread.start()
    validator_thread.start()

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
