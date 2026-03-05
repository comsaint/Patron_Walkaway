#!/usr/bin/env python3
"""Analyze patron session history distribution from session table.

Determines if building a cached player-level table would be useful by
checking how much session history patrons have (sessions per patron,
time span of history).

Usage:
  python -m trainer.scripts.analyze_session_history [--parquet PATH] [--csv PATH]

  Default: tries .data/local/sessions.parquet, then falls back to sample CSV.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def clean_casino_player_id(s: pd.Series) -> pd.Series:
    """FND-03: strip and treat 'null' as NaN."""
    out = s.astype(str).str.strip()
    out = out.replace(["", "null", "NULL", "nan", "None"], pd.NA)
    return out.where(out.notna() & (out != ""), other=pd.NA)


def load_sessions(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, nrows=None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze session history per patron")
    parser.add_argument(
        "--parquet",
        type=Path,
        default=None,
        help="Path to sessions.parquet (default: trainer/.data/local/sessions.parquet)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Path to sessions CSV (fallback if parquet missing)",
    )
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    parquet_path = args.parquet or (base / ".data" / "local" / "sessions.parquet")
    csv_path = args.csv or (base / "sample data" / "SmartTableData_tsession_sample.csv")

    if parquet_path.exists():
        print(f"Loading from Parquet: {parquet_path}")
        df = load_sessions(parquet_path)
    elif csv_path.exists():
        print(f"Parquet not found. Using CSV sample: {csv_path}")
        print("(Note: sample may be small/demo; use --parquet for full export)")
        df = load_sessions(csv_path)
    else:
        print("ERROR: No sessions file found.")
        print(f"  Parquet: {parquet_path}")
        print(f"  CSV:     {csv_path}")
        print("  Export from ClickHouse to .data/local/sessions.parquet first.")
        return

    # Parse times
    for col in ["session_start_dtm", "session_end_dtm", "lud_dtm"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Canonical id: casino_player_id (rated) or player_id (unrated)
    df["casino_player_id_clean"] = clean_casino_player_id(
        df.get("casino_player_id", pd.Series(dtype=object))
    )
    df["canonical_id"] = df["casino_player_id_clean"].fillna(
        df["player_id"].astype(str)
    )
    df["is_rated"] = df["casino_player_id_clean"].notna()

    # DQ: exclude is_manual, is_deleted, is_canceled (if present)
    if "is_manual" in df.columns:
        df = df[df["is_manual"].fillna(0) == 0]
    if "is_deleted" in df.columns:
        df = df[df["is_deleted"].fillna(0) == 0]
    if "is_canceled" in df.columns:
        df = df[df["is_canceled"].fillna(0) == 0]

    # Session time for ordering (FND-01 style)
    df["_sess_time"] = df["session_end_dtm"].fillna(df["lud_dtm"]).fillna(
        df["session_start_dtm"]
    )

    # Per-patron stats
    agg = (
        df.groupby("canonical_id")
        .agg(
            session_count=("session_id", "count"),
            first_session=("_sess_time", "min"),
            last_session=("_sess_time", "max"),
            is_rated=("is_rated", "any"),
        )
        .reset_index()
    )
    agg["history_span_days"] = (
        agg["last_session"] - agg["first_session"]
    ).dt.total_seconds() / 86400

    n_patrons = len(agg)
    n_rated = agg["is_rated"].sum()
    n_nonrated = n_patrons - n_rated

    print("\n" + "=" * 60)
    print("SESSION HISTORY — PATRON DISTRIBUTION")
    print("=" * 60)
    print(f"\nTotal patrons: {n_patrons:,}  (Rated: {n_rated:,}, Non-rated: {n_nonrated:,})")
    print(f"Total sessions: {len(df):,}")

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

    # Rated-only (they're the ones who'd get player table)
    if n_rated > 0:
        rated = agg[agg["is_rated"]]
        print("\n--- Rated patrons only (would use player-level table) ---")
        print(f"  Count: {len(rated):,}")
        print(f"  Sessions per patron — median: {rated['session_count'].median():.0f}, "
              f"mean: {rated['session_count'].mean():.1f}")
        print(f"  History span (days) — median: {rated['history_span_days'].median():.1f}, "
              f"mean: {rated['history_span_days'].mean():.1f}")
        print(f"  % with >= 5 sessions: {(rated['session_count'] >= 5).mean()*100:.1f}%")
        print(f"  % with >= 30 days history: {(rated['history_span_days'] >= 30).mean()*100:.1f}%")

    print("\n--- Sample of patrons with longest history ---")
    top = agg.nlargest(5, "history_span_days")[
        ["canonical_id", "session_count", "history_span_days", "is_rated"]
    ]
    print(top.to_string(index=False))

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
