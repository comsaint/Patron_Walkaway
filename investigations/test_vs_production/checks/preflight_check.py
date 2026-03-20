#!/usr/bin/env python3
"""
Preflight checks for test-vs-production investigation.

This script is read-only: it inspects environment variables, SQLite metadata,
and required artifact files, then emits a JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


REQUIRED_DATA_FILES = (
    "player_profile.parquet",
    "canonical_mapping.parquet",
    "canonical_mapping.cutoff.json",
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str
    message: str
    details: Dict[str, Any]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_path(path_str: Optional[str]) -> Optional[str]:
    if path_str is None:
        return None
    text = path_str.strip()
    return text or None


def check_prediction_log(path_str: Optional[str], freshness_minutes: int) -> CheckResult:
    pl_path = _safe_path(path_str)
    if pl_path is None:
        return CheckResult(
            name="prediction_log_configured",
            ok=False,
            severity="blocker",
            message="PREDICTION_LOG_DB_PATH is empty or unset.",
            details={},
        )

    db_path = Path(pl_path)
    details: Dict[str, Any] = {"path": str(db_path), "exists": db_path.exists()}
    if not db_path.exists():
        return CheckResult(
            name="prediction_log_db_exists",
            ok=False,
            severity="blocker",
            message="Prediction log DB file does not exist.",
            details=details,
        )

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_log'")
        has_table = cur.fetchone() is not None
        details["has_prediction_log_table"] = has_table
        if not has_table:
            return CheckResult(
                name="prediction_log_table_exists",
                ok=False,
                severity="blocker",
                message="prediction_log table not found.",
                details=details,
            )

        cur.execute("SELECT MAX(scored_at), COUNT(*) FROM prediction_log")
        max_scored_at, row_count = cur.fetchone()
        details["max_scored_at"] = max_scored_at
        details["row_count"] = int(row_count or 0)
        if max_scored_at is None:
            return CheckResult(
                name="prediction_log_has_rows",
                ok=False,
                severity="blocker",
                message="prediction_log table is empty.",
                details=details,
            )

        parsed = _parse_iso_dt(max_scored_at)
        if parsed is None:
            return CheckResult(
                name="prediction_log_scored_at_parseable",
                ok=False,
                severity="major",
                message="MAX(scored_at) is not a parseable ISO datetime.",
                details=details,
            )

        lag_minutes = (_now_utc() - parsed.astimezone(timezone.utc)).total_seconds() / 60.0
        details["lag_minutes"] = round(lag_minutes, 2)
        details["freshness_threshold_minutes"] = freshness_minutes
        is_fresh = lag_minutes <= freshness_minutes
        return CheckResult(
            name="prediction_log_freshness",
            ok=is_fresh,
            severity="blocker" if not is_fresh else "info",
            message=(
                "Prediction log is fresh."
                if is_fresh
                else "Prediction log appears stale based on MAX(scored_at)."
            ),
            details=details,
        )
    except sqlite3.Error as exc:
        details["sqlite_error"] = str(exc)
        return CheckResult(
            name="prediction_log_sqlite_access",
            ok=False,
            severity="blocker",
            message="Failed to read prediction log SQLite DB.",
            details=details,
        )
    finally:
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass


def check_data_dir(path_str: Optional[str]) -> CheckResult:
    data_dir = _safe_path(path_str)
    if data_dir is None:
        return CheckResult(
            name="data_dir_configured",
            ok=False,
            severity="major",
            message="DATA_DIR is empty or unset (scorer may fallback to project data path).",
            details={},
        )

    root = Path(data_dir)
    details: Dict[str, Any] = {"path": str(root), "exists": root.exists(), "files": {}}
    if not root.exists():
        return CheckResult(
            name="data_dir_exists",
            ok=False,
            severity="major",
            message="DATA_DIR does not exist.",
            details=details,
        )

    all_ok = True
    for filename in REQUIRED_DATA_FILES:
        p = root / filename
        exists = p.exists()
        details["files"][filename] = {
            "exists": exists,
            "size_bytes": p.stat().st_size if exists else None,
        }
        if not exists:
            all_ok = False

    cutoff_path = root / "canonical_mapping.cutoff.json"
    if cutoff_path.exists():
        try:
            payload = json.loads(cutoff_path.read_text(encoding="utf-8"))
            cutoff_dtm = payload.get("cutoff_dtm")
            details["cutoff_dtm"] = cutoff_dtm
            details["cutoff_dtm_parseable"] = _parse_iso_dt(cutoff_dtm) is not None
        except Exception as exc:
            details["cutoff_json_error"] = str(exc)
            all_ok = False

    return CheckResult(
        name="data_dir_required_files",
        ok=all_ok,
        severity="major" if not all_ok else "info",
        message="DATA_DIR required files check passed." if all_ok else "Missing required data artifacts in DATA_DIR.",
        details=details,
    )


def summarize(results: Tuple[CheckResult, ...]) -> Dict[str, Any]:
    blockers = [r for r in results if (not r.ok and r.severity == "blocker")]
    majors = [r for r in results if (not r.ok and r.severity == "major")]
    if blockers:
        overall = "fail"
        exit_code = 2
    elif majors:
        overall = "warn"
        exit_code = 1
    else:
        overall = "pass"
        exit_code = 0

    return {
        "overall_status": overall,
        "exit_code": exit_code,
        "generated_at_utc": _now_utc().isoformat(),
        "results": [
            {
                "name": r.name,
                "ok": r.ok,
                "severity": r.severity,
                "message": r.message,
                "details": r.details,
            }
            for r in results
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight checks for production investigation.")
    parser.add_argument(
        "--prediction-log-db-path",
        default=os.getenv("PREDICTION_LOG_DB_PATH", ""),
        help="Override prediction log DB path (default from env PREDICTION_LOG_DB_PATH).",
    )
    parser.add_argument(
        "--data-dir",
        default=os.getenv("DATA_DIR", ""),
        help="Override DATA_DIR (default from env DATA_DIR).",
    )
    parser.add_argument(
        "--freshness-minutes",
        type=int,
        default=15,
        help="Freshness threshold for MAX(scored_at) lag in minutes (default: 15).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.freshness_minutes <= 0:
        print("freshness-minutes must be > 0", file=sys.stderr)
        return 2

    results = (
        check_prediction_log(args.prediction_log_db_path, args.freshness_minutes),
        check_data_dir(args.data_dir),
    )
    payload = summarize(results)
    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
