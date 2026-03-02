#!/usr/bin/env python3
"""Full scan of session Parquet via DuckDB — patron history distribution.

Usage:
  python -m trainer.scripts.analyze_session_history_duckdb [--path PATH]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent / "data" / "gmwds_t_session.parquet",
        help="Path to session parquet",
    )
    args = parser.parse_args()
    path = args.path.resolve()

    if not path.exists():
        print(f"ERROR: File not found: {path}")
        return

    con = duckdb.connect(":memory:")

    # Canonical id: casino_player_id if valid (non-null, non-empty, not 'null'), else player_id
    # DQ: exclude is_manual=1, is_deleted=1, is_canceled=1
    q = """
    WITH base AS (
        SELECT
            session_id,
            player_id,
            COALESCE(
                CASE WHEN casino_player_id IS NOT NULL
                     AND TRIM(CAST(casino_player_id AS VARCHAR)) NOT IN ('', 'null', 'NULL', 'nan', 'None')
                THEN TRIM(CAST(casino_player_id AS VARCHAR))
                ELSE NULL END,
                CAST(player_id AS VARCHAR)
            ) AS canonical_id,
            CASE WHEN casino_player_id IS NOT NULL
                 AND TRIM(CAST(casino_player_id AS VARCHAR)) NOT IN ('', 'null', 'NULL')
            THEN 1 ELSE 0 END AS is_rated,
            COALESCE(session_end_dtm, lud_dtm, session_start_dtm) AS sess_time
        FROM read_parquet(?)
        WHERE COALESCE(is_manual, 0) = 0
          AND COALESCE(is_deleted, 0) = 0
          AND COALESCE(is_canceled, 0) = 0
    ),
    per_patron AS (
        SELECT
            canonical_id,
            MAX(is_rated) AS is_rated,
            COUNT(*) AS session_count,
            MIN(sess_time) AS first_session,
            MAX(sess_time) AS last_session,
            EXTRACT(EPOCH FROM (MAX(sess_time) - MIN(sess_time))) / 86400.0 AS history_span_days
        FROM base
        GROUP BY canonical_id
    )
    SELECT * FROM per_patron
    """
    agg = con.execute(q, [str(path)]).fetchdf()

    n_patrons = len(agg)
    n_rated = agg["is_rated"].sum()
    n_sessions = agg["session_count"].sum()

    print("\n" + "=" * 65)
    print("SESSION HISTORY — FULL SCAN (DuckDB)")
    print("=" * 65)
    print(f"Source: {path}")
    print(f"\nTotal patrons: {n_patrons:,}  (Rated: {int(n_rated):,}, Non-rated: {int(n_patrons - n_rated):,})")
    print(f"Total sessions: {int(n_sessions):,}")

    print("\n--- Sessions per patron (all) ---")
    for q in [0.5, 0.75, 0.9, 0.95, 0.99]:
        v = agg["session_count"].quantile(q)
        print(f"  p{q*100:.0f}: {v:.1f} sessions")
    print(f"  max: {agg['session_count'].max():.0f}")

    print("\n--- History span (days) per patron ---")
    for q in [0.5, 0.75, 0.9, 0.95, 0.99]:
        v = agg["history_span_days"].quantile(q)
        print(f"  p{q*100:.0f}: {v:.1f} days")
    print(f"  max: {agg['history_span_days'].max():.1f} days")

    if n_rated > 0:
        rated = agg[agg["is_rated"] == 1]
        print("\n--- Rated patrons only (would use player-level table) ---")
        print(f"  Count: {len(rated):,}")
        print(f"  Sessions/patron — median: {rated['session_count'].median():.0f}, mean: {rated['session_count'].mean():.1f}")
        print(f"  History span (days) — median: {rated['history_span_days'].median():.1f}, mean: {rated['history_span_days'].mean():.1f}")
        for thr_sess, thr_days in [(5, 30), (10, 90), (20, 180)]:
            pct_sess = (rated["session_count"] >= thr_sess).mean() * 100
            pct_days = (rated["history_span_days"] >= thr_days).mean() * 100
            print(f"  % with >= {thr_sess} sessions: {pct_sess:.1f}%  |  % with >= {thr_days} days history: {pct_days:.1f}%")

    n_unrated = n_patrons - int(n_rated)
    if n_unrated > 0:
        unrated = agg[agg["is_rated"] == 0]
        print("\n--- Unrated patrons only (sanity check) ---")
        print(f"  Count: {len(unrated):,}")
        print(f"  Sessions/patron — median: {unrated['session_count'].median():.0f}, mean: {unrated['session_count'].mean():.1f}")
        print(f"  History span (days) — median: {unrated['history_span_days'].median():.1f}, mean: {unrated['history_span_days'].mean():.1f}")
        for thr_sess, thr_days in [(2, 1), (5, 7), (10, 30)]:
            pct_sess = (unrated["session_count"] >= thr_sess).mean() * 100
            pct_days = (unrated["history_span_days"] >= thr_days).mean() * 100
            print(f"  % with >= {thr_sess} sessions: {pct_sess:.1f}%  |  % with >= {thr_days} days history: {pct_days:.1f}%")
        print(f"  % with 1 session only: {(unrated['session_count'] == 1).mean()*100:.1f}%")
        print(f"  % with 0-day span (single day): {(unrated['history_span_days'] == 0).mean()*100:.1f}%")

    print("\n--- Top 5 by history span (days) ---")
    top = agg.nlargest(5, "history_span_days")[
        ["canonical_id", "session_count", "history_span_days", "is_rated"]
    ]
    print(top.to_string(index=False))

    print("\n" + "=" * 65)
    con.close()


if __name__ == "__main__":
    main()
