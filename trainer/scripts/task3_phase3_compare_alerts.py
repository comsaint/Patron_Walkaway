from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Dict, List


def _fetch_columns(conn: sqlite3.Connection, schema: str, table: str) -> List[str]:
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return [str(r[1]) for r in rows]


def compare_alerts(
    baseline_db: Path,
    candidate_db: Path,
    *,
    score_tol: float,
    margin_tol: float,
) -> Dict[str, object]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("ATTACH DATABASE ? AS b", (str(baseline_db),))
        conn.execute("ATTACH DATABASE ? AS c", (str(candidate_db),))

        b_cols = _fetch_columns(conn, "b", "alerts")
        c_cols = _fetch_columns(conn, "c", "alerts")
        b_set = set(b_cols)
        c_set = set(c_cols)
        common = sorted(b_set & c_set)

        b_count = int(conn.execute("SELECT COUNT(*) FROM b.alerts").fetchone()[0])
        c_count = int(conn.execute("SELECT COUNT(*) FROM c.alerts").fetchone()[0])
        only_b = int(
            conn.execute(
                "SELECT COUNT(*) FROM (SELECT bet_id FROM b.alerts EXCEPT SELECT bet_id FROM c.alerts)"
            ).fetchone()[0]
        )
        only_c = int(
            conn.execute(
                "SELECT COUNT(*) FROM (SELECT bet_id FROM c.alerts EXCEPT SELECT bet_id FROM b.alerts)"
            ).fetchone()[0]
        )

        join_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM b.alerts ba INNER JOIN c.alerts ca ON ba.bet_id = ca.bet_id"
            ).fetchone()[0]
        )

        score_max_abs = 0.0
        margin_max_abs = 0.0
        score_over_tol = 0
        margin_over_tol = 0
        if "score" in common:
            score_max_abs = float(
                conn.execute(
                    "SELECT COALESCE(MAX(ABS(COALESCE(ba.score, 0.0) - COALESCE(ca.score, 0.0))), 0.0) "
                    "FROM b.alerts ba INNER JOIN c.alerts ca ON ba.bet_id = ca.bet_id"
                ).fetchone()[0]
            )
            score_over_tol = int(
                conn.execute(
                    "SELECT COUNT(*) FROM b.alerts ba INNER JOIN c.alerts ca ON ba.bet_id = ca.bet_id "
                    "WHERE ABS(COALESCE(ba.score, 0.0) - COALESCE(ca.score, 0.0)) > ?",
                    (float(score_tol),),
                ).fetchone()[0]
            )
        if "margin" in common:
            margin_max_abs = float(
                conn.execute(
                    "SELECT COALESCE(MAX(ABS(COALESCE(ba.margin, 0.0) - COALESCE(ca.margin, 0.0))), 0.0) "
                    "FROM b.alerts ba INNER JOIN c.alerts ca ON ba.bet_id = ca.bet_id"
                ).fetchone()[0]
            )
            margin_over_tol = int(
                conn.execute(
                    "SELECT COUNT(*) FROM b.alerts ba INNER JOIN c.alerts ca ON ba.bet_id = ca.bet_id "
                    "WHERE ABS(COALESCE(ba.margin, 0.0) - COALESCE(ca.margin, 0.0)) > ?",
                    (float(margin_tol),),
                ).fetchone()[0]
            )

        return {
            "schema": {
                "baseline_only_columns": sorted(b_set - c_set),
                "candidate_only_columns": sorted(c_set - b_set),
                "common_columns": common,
            },
            "alerts": {
                "baseline_count": b_count,
                "candidate_count": c_count,
                "intersection_count": join_count,
                "baseline_only_bet_ids": only_b,
                "candidate_only_bet_ids": only_c,
            },
            "numeric_drift": {
                "score_tolerance": float(score_tol),
                "margin_tolerance": float(margin_tol),
                "max_abs_score_diff": score_max_abs,
                "max_abs_margin_diff": margin_max_abs,
                "score_diff_rows_over_tolerance": score_over_tol,
                "margin_diff_rows_over_tolerance": margin_over_tol,
            },
        }
    finally:
        conn.close()


def _main() -> None:
    parser = argparse.ArgumentParser(description="Compare alerts table between baseline and candidate state DB.")
    parser.add_argument("--baseline-db", required=True, type=Path)
    parser.add_argument("--candidate-db", required=True, type=Path)
    parser.add_argument("--score-tol", type=float, default=1e-6)
    parser.add_argument("--margin-tol", type=float, default=1e-6)
    parser.add_argument("--out-json", required=False, type=Path)
    args = parser.parse_args()

    result = compare_alerts(
        args.baseline_db,
        args.candidate_db,
        score_tol=float(args.score_tol),
        margin_tol=float(args.margin_tol),
    )
    payload = json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True)
    print(payload)

    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(payload, encoding="utf-8")


if __name__ == "__main__":
    _main()
