"""Join latest serving vs training ``feature_audit_feature_summary`` rows and print drift.

Run from repo root::

    python -m trainer.scripts.compare_feature_audit_summaries \\
        --serving-db path/to/prediction_log.db \\
        --training-db investigations/feature_audit_training.sqlite \\
        [--out-csv drift.csv]

Uses the latest ``feature_audit_runs.id`` per ``source`` (``serving`` / ``training``).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd


def _latest_run_id(conn: sqlite3.Connection, source: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM feature_audit_runs WHERE source = ? ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()
    return int(row[0]) if row else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare feature_audit_feature_summary serving vs training.")
    ap.add_argument("--serving-db", type=Path, required=True)
    ap.add_argument("--training-db", type=Path, required=True)
    ap.add_argument("--out-csv", type=Path, default=None)
    args = ap.parse_args()

    for path in (args.serving_db, args.training_db):
        if not path.is_file():
            print(f"DB not found: {path}", file=sys.stderr)
            return 1

    s_conn = sqlite3.connect(str(args.serving_db))
    t_conn = sqlite3.connect(str(args.training_db))
    try:
        s_id = _latest_run_id(s_conn, "serving")
        t_id = _latest_run_id(t_conn, "training")
        if s_id is None:
            print("No serving feature_audit_runs row (source='serving').", file=sys.stderr)
            return 1
        if t_id is None:
            print("No training feature_audit_runs row (source='training').", file=sys.stderr)
            return 1

        s_df = pd.read_sql_query(
            "SELECT feature_name, track, count_n, null_count, zero_count, mean_v, std_v, "
            "min_v, p01, p05, p50, p95, p99, max_v FROM feature_audit_feature_summary "
            "WHERE audit_run_id = ?",
            s_conn,
            params=(s_id,),
        )
        t_df = pd.read_sql_query(
            "SELECT feature_name, track, count_n, null_count, zero_count, mean_v, std_v, "
            "min_v, p01, p05, p50, p95, p99, max_v FROM feature_audit_feature_summary "
            "WHERE audit_run_id = ?",
            t_conn,
            params=(t_id,),
        )
    finally:
        s_conn.close()
        t_conn.close()

    merged = s_df.merge(
        t_df,
        on="feature_name",
        how="outer",
        suffixes=("_serving", "_training"),
        indicator=True,
    )
    merged["null_rate_serving"] = merged["null_count_serving"] / merged["count_n_serving"].replace(0, pd.NA)
    merged["null_rate_training"] = merged["null_count_training"] / merged["count_n_training"].replace(0, pd.NA)
    merged["mean_diff"] = merged["mean_v_serving"] - merged["mean_v_training"]
    merged["p50_diff"] = merged["p50_serving"] - merged["p50_training"]
    merged["p95_diff"] = merged["p95_serving"] - merged["p95_training"]

    cols = [
        "feature_name",
        "_merge",
        "track_serving",
        "track_training",
        "mean_v_serving",
        "mean_v_training",
        "mean_diff",
        "p50_diff",
        "p95_diff",
        "null_rate_serving",
        "null_rate_training",
    ]
    _m = merged.copy()
    _m["__abs_mean_diff"] = _m["mean_diff"].fillna(0.0).abs()
    _m = _m.sort_values("__abs_mean_diff", ascending=False)
    out = _m[[c for c in cols if c in _m.columns]]
    if args.out_csv is not None:
        args.out_csv.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(args.out_csv, index=False)
        print(f"Wrote {args.out_csv.resolve()} ({len(out)} rows)")
    else:
        with pd.option_context("display.max_rows", 50, "display.width", 200):
            print(out.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
