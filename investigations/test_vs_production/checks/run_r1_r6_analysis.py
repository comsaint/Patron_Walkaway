#!/usr/bin/env python3
"""
R1/R6 helper for production investigation.

This script provides two stages:
1) sample: build below-threshold candidate sample from prediction_log
2) evaluate: merge offline labels and compute:
   - precision/recall at current threshold (is_alert)
   - precision@recall=target (default 1%)

Notes:
- Read-only against prediction_log DB.
- Designed for low-memory environments: stream rows for sampling; evaluate on
  labeled subset only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


HK_TZ = ZoneInfo("Asia/Hong_Kong")
DEFAULT_TARGET_RECALL = 0.01


@dataclass
class CandidateRow:
    bet_id: str
    score: float
    scored_at: str
    is_alert: int
    is_rated_obs: int
    bin_id: int


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
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        k, v = parsed
        out[k] = v
    return out


def _resolve_pred_db_path(env_file: str, pred_db_path: str) -> Tuple[str, Optional[str]]:
    if pred_db_path.strip():
        return pred_db_path.strip(), None

    repo_root = _repo_root_from_script()
    env_candidates: Sequence[Path]
    if env_file.strip():
        env_candidates = [Path(env_file).resolve()]
    else:
        env_candidates = [
            Path.cwd() / "credential" / ".env",
            Path.cwd() / ".env",
            repo_root / "credential" / ".env",
            repo_root / ".env",
        ]

    env_used = None
    file_env: Dict[str, str] = {}
    for p in env_candidates:
        d = _load_env_file(p)
        if "PREDICTION_LOG_DB_PATH" in d:
            file_env = d
            env_used = str(p)
            break

    value = os.getenv("PREDICTION_LOG_DB_PATH") or file_env.get("PREDICTION_LOG_DB_PATH") or ""
    return value.strip(), env_used


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


def _bin_from_score(score: float, bins: int) -> int:
    if math.isnan(score):
        return -1
    if score <= 0.0:
        return 0
    if score >= 1.0:
        return bins - 1
    idx = int(score * bins)
    if idx >= bins:
        idx = bins - 1
    return idx


def _reservoir_update(reservoir: List[CandidateRow], item: CandidateRow, seen: int, target_size: int, rng_seed: int) -> None:
    # Deterministic pseudo-randomness without global random state.
    # Hash-based replacement for low-overhead reproducibility.
    if len(reservoir) < target_size:
        reservoir.append(item)
        return
    # pseudo-random integer in [0, seen-1]
    j = (hash((item.bet_id, seen, rng_seed)) & 0x7FFFFFFF) % seen
    if j < target_size:
        reservoir[j] = item


def run_sample_mode(
    db_path: str,
    start_ts: str,
    end_ts: str,
    sample_size: int,
    bins: int,
    seed: int,
    out_csv: Path,
) -> Dict[str, object]:
    if sample_size <= 0:
        raise ValueError("sample-size must be > 0")
    if bins <= 0:
        raise ValueError("bins must be > 0")

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_log'")
        if cur.fetchone() is None:
            raise RuntimeError("prediction_log table not found")

        cur.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN is_rated_obs=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_rated_obs=1 AND is_alert=1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN is_rated_obs=1 AND is_alert=0 THEN 1 ELSE 0 END)
            FROM prediction_log
            WHERE scored_at >= ? AND scored_at < ?
            """,
            (start_ts, end_ts),
        )
        n_total, n_rated, n_alert_rated, n_below_rated = cur.fetchone()

        per_bin_target = max(1, sample_size // bins)
        reservoirs: Dict[int, List[CandidateRow]] = {i: [] for i in range(bins)}
        seen_per_bin: Dict[int, int] = {i: 0 for i in range(bins)}

        cursor = conn.execute(
            """
            SELECT bet_id, score, scored_at, is_alert, is_rated_obs
            FROM prediction_log
            WHERE scored_at >= ? AND scored_at < ?
              AND is_rated_obs = 1
              AND is_alert = 0
            """,
            (start_ts, end_ts),
        )
        for bet_id, score, scored_at, is_alert, is_rated_obs in cursor:
            if bet_id is None or score is None:
                continue
            try:
                fscore = float(score)
            except (TypeError, ValueError):
                continue
            b = _bin_from_score(fscore, bins)
            if b < 0:
                continue
            row = CandidateRow(
                bet_id=str(bet_id),
                score=fscore,
                scored_at=str(scored_at),
                is_alert=int(is_alert or 0),
                is_rated_obs=int(is_rated_obs or 0),
                bin_id=b,
            )
            seen_per_bin[b] += 1
            _reservoir_update(reservoirs[b], row, seen_per_bin[b], per_bin_target, seed)

        sampled = [item for b in range(bins) for item in reservoirs[b]]
        sampled = sampled[:sample_size]

        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["bet_id", "score", "scored_at", "is_alert", "is_rated_obs", "bin_id"])
            for r in sampled:
                w.writerow([r.bet_id, f"{r.score:.10f}", r.scored_at, r.is_alert, r.is_rated_obs, r.bin_id])

        return {
            "mode": "sample",
            "window": {"start_ts": start_ts, "end_ts": end_ts},
            "db_path": db_path,
            "summary": {
                "n_total": int(n_total or 0),
                "n_rated": int(n_rated or 0),
                "n_alert_rated": int(n_alert_rated or 0),
                "n_below_rated": int(n_below_rated or 0),
                "sample_size_requested": sample_size,
                "sample_size_written": len(sampled),
                "bins": bins,
                "per_bin_target": per_bin_target,
            },
            "output_csv": str(out_csv),
            "note": "Run offline labeling on output_csv bet_id, then use evaluate mode with labeled CSV.",
        }
    finally:
        conn.close()


def _load_labels_csv(path: Path) -> Dict[str, int]:
    labels: Dict[str, int] = {}
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"bet_id", "label"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("labels CSV must contain columns: bet_id,label")
        for row in reader:
            bid = str(row["bet_id"]).strip()
            if not bid:
                continue
            try:
                y = int(str(row["label"]).strip())
            except ValueError:
                continue
            if y not in (0, 1):
                continue
            labels[bid] = y
    return labels


def _precision_recall_at_current_threshold(rows: List[Tuple[float, int, int]]) -> Dict[str, float]:
    # rows: (score, is_alert, label)
    tp = fp = fn = 0
    for _s, pred, y in rows:
        if pred == 1 and y == 1:
            tp += 1
        elif pred == 1 and y == 0:
            fp += 1
        elif pred == 0 and y == 1:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {
        "tp": float(tp),
        "fp": float(fp),
        "fn": float(fn),
        "precision": precision,
        "recall": recall,
    }


def _precision_at_recall_target(rows: List[Tuple[float, int]], target_recall: float) -> Dict[str, float]:
    # rows: (score, label)
    if not rows:
        return {"precision_at_target_recall": 0.0, "threshold_at_target": 0.0, "achieved_recall": 0.0}
    sorted_rows = sorted(rows, key=lambda x: x[0], reverse=True)
    total_pos = sum(y for _, y in sorted_rows)
    if total_pos <= 0:
        return {"precision_at_target_recall": 0.0, "threshold_at_target": sorted_rows[0][0], "achieved_recall": 0.0}

    tp = fp = 0
    best_precision = -1.0
    best_threshold = sorted_rows[-1][0]
    best_recall = 0.0
    for score, label in sorted_rows:
        if label == 1:
            tp += 1
        else:
            fp += 1
        recall = tp / total_pos
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        if recall >= target_recall and precision > best_precision:
            best_precision = precision
            best_threshold = score
            best_recall = recall

    if best_precision < 0.0:
        return {
            "precision_at_target_recall": 0.0,
            "threshold_at_target": sorted_rows[-1][0],
            "achieved_recall": tp / total_pos if total_pos else 0.0,
        }
    return {
        "precision_at_target_recall": best_precision,
        "threshold_at_target": best_threshold,
        "achieved_recall": best_recall,
    }


def run_evaluate_mode(
    db_path: str,
    start_ts: str,
    end_ts: str,
    labels_csv: Path,
    target_recall: float,
) -> Dict[str, object]:
    if target_recall <= 0 or target_recall > 1:
        raise ValueError("target-recall must be in (0, 1]")

    labels = _load_labels_csv(labels_csv)
    if not labels:
        raise ValueError("labels CSV contains no valid (bet_id,label) rows")

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_log'")
        if cur.fetchone() is None:
            raise RuntimeError("prediction_log table not found")

        # Create temp label table for efficient join.
        conn.execute("DROP TABLE IF EXISTS _tmp_labels")
        conn.execute("CREATE TEMP TABLE _tmp_labels (bet_id TEXT PRIMARY KEY, label INTEGER NOT NULL)")
        conn.executemany(
            "INSERT INTO _tmp_labels (bet_id, label) VALUES (?, ?)",
            [(bid, y) for bid, y in labels.items()],
        )

        rows: List[Tuple[float, int, int]] = []
        for score, is_alert, label in conn.execute(
            """
            SELECT p.score, p.is_alert, l.label
            FROM prediction_log p
            JOIN _tmp_labels l ON p.bet_id = l.bet_id
            WHERE p.scored_at >= ? AND p.scored_at < ?
              AND p.is_rated_obs = 1
            """,
            (start_ts, end_ts),
        ):
            if score is None:
                continue
            rows.append((float(score), int(is_alert or 0), int(label)))

        if not rows:
            raise ValueError("No labeled rows matched prediction_log in the selected window")

        current = _precision_recall_at_current_threshold(rows)
        pat = _precision_at_recall_target([(s, y) for s, _pred, y in rows], target_recall)

        return {
            "mode": "evaluate",
            "window": {"start_ts": start_ts, "end_ts": end_ts},
            "db_path": db_path,
            "labels_csv": str(labels_csv),
            "n_labeled_input": len(labels),
            "n_labeled_matched": len(rows),
            "current_threshold_metrics": current,
            "precision_at_recall_target": {
                "target_recall": target_recall,
                **pat,
            },
            "note": "Compare current_threshold_metrics and precision_at_recall_target against offline test metrics with same definition.",
        }
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run R1/R6 investigation helper.")
    p.add_argument("--mode", choices=["sample", "evaluate"], default="sample")
    p.add_argument("--start-ts", default="", help="Window start timestamp (inclusive). Default: dynamic yesterday start in HKT.")
    p.add_argument("--end-ts", default="", help="Window end timestamp (exclusive). Default: dynamic today start in HKT.")
    p.add_argument("--env-file", default="", help="Optional .env file path.")
    p.add_argument("--pred-db-path", default="", help="Override prediction log DB path.")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")

    # sample mode args
    p.add_argument("--sample-size", type=int, default=5000, help="Target below-threshold sample size.")
    p.add_argument("--bins", type=int, default=10, help="Number of score bins for stratified sampling.")
    p.add_argument("--seed", type=int, default=42, help="Deterministic seed for reservoir replacement.")
    p.add_argument(
        "--out-csv",
        default="investigations/test_vs_production/snapshots/latest_r1_r6_below_threshold_sample.csv",
        help="Output CSV for sample mode.",
    )

    # evaluate mode args
    p.add_argument("--labels-csv", default="", help="CSV with columns bet_id,label (required for evaluate mode).")
    p.add_argument("--target-recall", type=float, default=DEFAULT_TARGET_RECALL, help="Recall target for precision@recall.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        start_ts, end_ts = _resolve_window(args.start_ts, args.end_ts)
        start_dt = _parse_iso_ts(start_ts)
        end_dt = _parse_iso_ts(end_ts)
        if start_dt >= end_dt:
            raise ValueError("start-ts must be earlier than end-ts")
    except ValueError as exc:
        print(str(exc))
        return 2

    pred_db_path, env_file_used = _resolve_pred_db_path(args.env_file, args.pred_db_path)
    if not pred_db_path:
        print("PREDICTION_LOG_DB_PATH is empty. Provide --pred-db-path or set env/.env.")
        return 2
    if not Path(pred_db_path).exists():
        print(f"prediction log DB not found: {pred_db_path}")
        return 2

    try:
        if args.mode == "sample":
            payload = run_sample_mode(
                db_path=pred_db_path,
                start_ts=start_ts,
                end_ts=end_ts,
                sample_size=args.sample_size,
                bins=args.bins,
                seed=args.seed,
                out_csv=Path(args.out_csv),
            )
        else:
            if not args.labels_csv.strip():
                raise ValueError("--labels-csv is required in evaluate mode")
            payload = run_evaluate_mode(
                db_path=pred_db_path,
                start_ts=start_ts,
                end_ts=end_ts,
                labels_csv=Path(args.labels_csv),
                target_recall=args.target_recall,
            )
    except Exception as exc:
        print(f"R1/R6 script failed: {exc}")
        return 2

    payload["resolution"] = {
        "pred_db_path": pred_db_path,
        "env_file_used": env_file_used,
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "mode": args.mode,
    }

    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

