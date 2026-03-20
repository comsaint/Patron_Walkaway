#!/usr/bin/env python3
"""
R2 baseline investigation for a production window.

What this script does:
1) Query prediction_log coverage in [start_ts, end_ts)
2) Query prediction_log alert-volume summary in [start_ts, end_ts)
3) Cross-check alerts table count (state DB) in [start_ts, end_ts)

Default window is dynamic "yesterday" in HKT:
- start: yesterday 00:00:00+08:00
- end:   today 00:00:00+08:00

How to run (default window is yesterday):
```bash
python investigations/test_vs_production/checks/investigate_r2_window.py --pretty
```

Optionally, supply parameters:
```bash
python investigations/test_vs_production/checks/investigate_r2_window.py \
  --start-ts "2026-03-19T00:00:00+08:00" \
  --end-ts   "2026-03-20T00:00:00+08:00" \
  --env-file "C:/path/to/credential/.env" \
  --pred-db-path "C:/.../prediction_log.db" \
  --state-db-path "C:/.../state.db" \
  --pretty
```
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo


HK_TZ = ZoneInfo("Asia/Hong_Kong")


@dataclass
class QueryResult:
    ok: bool
    data: Dict[str, Any]
    error: Optional[str] = None


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[3]


def _parse_env_line(line: str) -> Optional[Tuple[str, str]]:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    k, v = s.split("=", 1)
    k = k.strip()
    v = v.strip()
    if not k:
        return None
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        v = v[1:-1]
    return k, v


def _load_env_file(path: Path) -> Dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}
    env: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        k, v = parsed
        env[k] = v
    return env


def _resolve_paths(args: argparse.Namespace) -> Dict[str, Any]:
    repo_root = _repo_root_from_script()
    env_candidates = (
        Path(args.env_file).resolve(),
    ) if args.env_file.strip() else (
        Path.cwd() / "credential" / ".env",
        Path.cwd() / ".env",
        repo_root / "credential" / ".env",
        repo_root / ".env",
    )

    env_vars: Dict[str, str] = {}
    env_file_used: Optional[str] = None
    for candidate in env_candidates:
        loaded = _load_env_file(candidate)
        if loaded:
            env_vars = loaded
            env_file_used = str(candidate)
            break

    pred_db = args.pred_db_path or os.getenv("PREDICTION_LOG_DB_PATH") or env_vars.get("PREDICTION_LOG_DB_PATH") or ""
    state_db = args.state_db_path or os.getenv("STATE_DB_PATH") or env_vars.get("STATE_DB_PATH") or ""
    if not state_db:
        state_db = str(repo_root / "local_state" / "state.db")

    return {
        "repo_root": str(repo_root),
        "env_file_used": env_file_used,
        "pred_db_path": pred_db.strip(),
        "state_db_path": state_db.strip(),
        "resolution_precedence": "cli > process_env > env_file > repo_default_for_state_db",
    }


def _run_prediction_queries(db_path: str, start_ts: str, end_ts: str) -> QueryResult:
    path = Path(db_path)
    if not db_path:
        return QueryResult(ok=False, data={}, error="PREDICTION_LOG_DB_PATH is empty.")
    if not path.exists():
        return QueryResult(ok=False, data={"path": db_path}, error="prediction log DB file not found.")
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_log'")
        if cur.fetchone() is None:
            return QueryResult(ok=False, data={"path": db_path}, error="prediction_log table not found.")

        cur.execute(
            """
            SELECT COUNT(*) AS n_rows,
                   MIN(scored_at) AS min_scored_at,
                   MAX(scored_at) AS max_scored_at
            FROM prediction_log
            WHERE scored_at >= ? AND scored_at < ?
            """,
            (start_ts, end_ts),
        )
        n_rows, min_scored_at, max_scored_at = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(*) AS n_scored,
                   SUM(CASE WHEN is_alert = 1 THEN 1 ELSE 0 END) AS n_is_alert_1,
                   AVG(score) AS avg_score
            FROM prediction_log
            WHERE scored_at >= ? AND scored_at < ?
            """,
            (start_ts, end_ts),
        )
        n_scored, n_is_alert_1, avg_score = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(*) AS n_unique_bet_id
            FROM (
              SELECT DISTINCT bet_id
              FROM prediction_log
              WHERE scored_at >= ? AND scored_at < ?
            )
            """,
            (start_ts, end_ts),
        )
        (n_unique_bet_id,) = cur.fetchone()

        return QueryResult(
            ok=True,
            data={
                "path": db_path,
                "coverage": {
                    "n_rows": int(n_rows or 0),
                    "min_scored_at": min_scored_at,
                    "max_scored_at": max_scored_at,
                },
                "summary": {
                    "n_scored": int(n_scored or 0),
                    "n_is_alert_1": int(n_is_alert_1 or 0),
                    "avg_score": float(avg_score) if avg_score is not None else None,
                    "n_unique_bet_id": int(n_unique_bet_id or 0),
                },
            },
        )
    except sqlite3.Error as exc:
        return QueryResult(ok=False, data={"path": db_path}, error=f"sqlite error: {exc}")
    finally:
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass


def _run_alerts_count_query(db_path: str, start_ts: str, end_ts: str) -> QueryResult:
    path = Path(db_path)
    if not db_path:
        return QueryResult(ok=False, data={}, error="STATE_DB_PATH is empty.")
    if not path.exists():
        return QueryResult(ok=False, data={"path": db_path}, error="state DB file not found.")
    try:
        conn = sqlite3.connect(str(path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'")
        if cur.fetchone() is None:
            return QueryResult(ok=False, data={"path": db_path}, error="alerts table not found.")

        cur.execute(
            """
            SELECT COUNT(*) AS n_alerts_table
            FROM alerts
            WHERE scored_at >= ? AND scored_at < ?
            """,
            (start_ts, end_ts),
        )
        (n_alerts_table,) = cur.fetchone()

        return QueryResult(
            ok=True,
            data={
                "path": db_path,
                "n_alerts_table": int(n_alerts_table or 0),
            },
        )
    except sqlite3.Error as exc:
        return QueryResult(ok=False, data={"path": db_path}, error=f"sqlite error: {exc}")
    finally:
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="R2 baseline investigation for a fixed window.")
    p.add_argument("--start-ts", default="", help="Window start timestamp (inclusive). Default: dynamic yesterday start in HKT.")
    p.add_argument("--end-ts", default="", help="Window end timestamp (exclusive). Default: dynamic today start in HKT.")
    p.add_argument("--env-file", default="", help="Optional .env file path.")
    p.add_argument("--pred-db-path", default="", help="Override prediction log SQLite path.")
    p.add_argument("--state-db-path", default="", help="Override state SQLite path.")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return p.parse_args()


def _resolve_window(start_ts: str, end_ts: str) -> Tuple[str, str]:
    s = start_ts.strip()
    e = end_ts.strip()
    if bool(s) ^ bool(e):
        raise ValueError("start-ts and end-ts must be provided together")
    if s and e:
        return s, e
    now_hk = datetime.now(HK_TZ)
    today_start = now_hk.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    return yesterday_start.isoformat(), today_start.isoformat()


def _parse_iso_ts(ts: str) -> datetime:
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must include timezone offset: {ts}")
    return dt


def main() -> int:
    args = parse_args()
    try:
        start_ts, end_ts = _resolve_window(args.start_ts, args.end_ts)
        start_dt = _parse_iso_ts(start_ts)
        end_dt = _parse_iso_ts(end_ts)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if start_dt >= end_dt:
        print("start-ts must be earlier than end-ts", file=sys.stderr)
        return 2

    resolved = _resolve_paths(args)
    prediction = _run_prediction_queries(resolved["pred_db_path"], start_ts, end_ts)
    alerts = _run_alerts_count_query(resolved["state_db_path"], start_ts, end_ts)

    ratio = None
    if prediction.ok and alerts.ok:
        n_is_alert_1 = prediction.data["summary"]["n_is_alert_1"]
        n_alerts_table = alerts.data["n_alerts_table"]
        ratio = (float(n_is_alert_1) / n_alerts_table) if n_alerts_table > 0 else None

    payload = {
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "resolution": resolved,
        "prediction_log": {
            "ok": prediction.ok,
            "error": prediction.error,
            "data": prediction.data,
        },
        "alerts_table": {
            "ok": alerts.ok,
            "error": alerts.error,
            "data": alerts.data,
        },
        "cross_check": {
            "prediction_is_alert_to_alerts_ratio": ratio,
            "note": "ratio > 1 can be expected due to duplicate suppression before alerts write.",
        },
    }

    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))

    if not prediction.ok:
        return 2
    if not alerts.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
