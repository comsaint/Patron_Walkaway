"""
Estimate data size and fetch time per scorer poll using local ./data Parquet.

Scorer each cycle (default --lookback-hours 8):
  - Bets:  payout_complete_dtm in [now-8h, now-BET_AVAIL_DELAY_MIN], 12 columns
  - Sessions: session_start_dtm in [start-2d, end+1d] (~3 days), 8 columns after dedup

Uses data/gmwds_t_bet.parquet and data/gmwds_t_session.parquet if present.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# project root
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
BET_PATH = DATA / "gmwds_t_bet.parquet"
SESS_PATH = DATA / "gmwds_t_session.parquet"

# Scorer defaults
LOOKBACK_HOURS = 8
SESSION_EXTRA_DAYS_BEFORE = 2
SESSION_EXTRA_DAYS_AFTER = 1


def main() -> None:
    if not BET_PATH.exists() or not SESS_PATH.exists():
        print("Missing Parquet files. Expect:", BET_PATH, SESS_PATH, file=sys.stderr)
        sys.exit(1)

    try:
        import pyarrow.parquet as pq
    except ImportError:
        print("Need pyarrow: pip install pyarrow", file=sys.stderr)
        sys.exit(1)

    # File sizes
    bet_size_mb = BET_PATH.stat().st_size / (1024 * 1024)
    sess_size_mb = SESS_PATH.stat().st_size / (1024 * 1024)

    # Row counts and date range from metadata / one column
    bet_meta = pq.read_metadata(BET_PATH)
    bet_total_rows = bet_meta.num_rows
    sess_meta = pq.read_metadata(SESS_PATH)
    sess_total_rows = sess_meta.num_rows

    # Get date range for bets (payout_complete_dtm)
    bet_ts_col = "payout_complete_dtm"
    bet_table = pq.read_table(BET_PATH, columns=[bet_ts_col])
    bet_ts = bet_table.column(bet_ts_col)
    if hasattr(bet_ts, "min_max"):
        lo, hi = bet_ts.min_max()
    else:
        import pyarrow.compute as pc
        lo, hi = pc.min(bet_ts), pc.max(bet_ts)
    try:
        bet_span_hours = (hi.as_py() - lo.as_py()).total_seconds() / 3600.0
    except Exception:
        bet_span_hours = None
    del bet_table, bet_ts

    # Get date range for sessions (session_start_dtm)
    sess_ts_col = "session_start_dtm"
    sess_table = pq.read_table(SESS_PATH, columns=[sess_ts_col])
    sess_ts = sess_table.column(sess_ts_col)
    if hasattr(sess_ts, "min_max"):
        slo, shi = sess_ts.min_max()
    else:
        import pyarrow.compute as pc
        slo, shi = pc.min(sess_ts), pc.max(sess_ts)
    try:
        sess_span_hours = (shi.as_py() - slo.as_py()).total_seconds() / 3600.0
    except Exception:
        sess_span_hours = None
    del sess_table, sess_ts

    # Rows per hour (bets)
    if bet_span_hours and bet_span_hours > 0:
        bets_per_hour = bet_total_rows / bet_span_hours
        bets_per_8h = bets_per_hour * LOOKBACK_HOURS
    else:
        bets_per_hour = bets_per_8h = None

    # Bytes per bet row (approximate: full file size / rows; scorer selects 12 cols so less)
    bet_bytes_per_row = (BET_PATH.stat().st_size / bet_total_rows) if bet_total_rows else 0
    # Scorer bet query returns 12 columns; parquet may have 80+. Assume ~40% of row size.
    scorer_bet_cols_ratio = 12 / max(1, len(pq.read_schema(BET_PATH).names))
    bet_fetch_bytes_per_row = bet_bytes_per_row * scorer_bet_cols_ratio
    if bets_per_8h is not None:
        bet_fetch_mb = (bets_per_8h * bet_fetch_bytes_per_row) / (1024 * 1024)
    else:
        bet_fetch_mb = None

    # Sessions: scorer window is (start - 2d) to (end + 1d) with start=now-8h -> ~3 days
    session_window_hours = LOOKBACK_HOURS + (SESSION_EXTRA_DAYS_BEFORE + SESSION_EXTRA_DAYS_AFTER) * 24
    if sess_span_hours and sess_span_hours > 0:
        sess_per_hour = sess_total_rows / sess_span_hours
        sess_per_poll = sess_per_hour * session_window_hours
    else:
        sess_per_poll = None
    sess_schema_cols = len(pq.read_schema(SESS_PATH).names)
    sess_bytes_per_row = (SESS_PATH.stat().st_size / sess_total_rows) if sess_total_rows else 0
    sess_cols_ratio = 8 / max(1, sess_schema_cols)
    sess_fetch_bytes_per_row = sess_bytes_per_row * sess_cols_ratio
    if sess_per_poll is not None:
        sess_fetch_mb = (sess_per_poll * sess_fetch_bytes_per_row) / (1024 * 1024)
    else:
        sess_fetch_mb = None

    # Fetch time: rough ClickHouse throughput 50–200 MB/s depending on network and disk
    total_fetch_mb = (bet_fetch_mb or 0) + (sess_fetch_mb or 0)
    fetch_time_low = total_fetch_mb / 200.0 if total_fetch_mb else 0   # 200 MB/s
    fetch_time_high = total_fetch_mb / 50.0 if total_fetch_mb else 0  # 50 MB/s

    # Print report
    print("=== Scorer fetch estimate (per poll, --lookback-hours 8) ===\n")
    print("Data source: local Parquet (same shape as ClickHouse export)\n")
    print("File sizes:")
    print(f"  gmwds_t_bet.parquet:    {bet_size_mb:.1f} MB, {bet_total_rows:,} rows")
    print(f"  gmwds_t_session.parquet: {sess_size_mb:.1f} MB, {sess_total_rows:,} rows")
    if bet_span_hours is not None:
        print(f"  Bet date span: {bet_span_hours/24:.1f} days")
    if sess_span_hours is not None:
        print(f"  Session date span: {sess_span_hours/24:.1f} days")
    print()
    print("Per poll (8h bet window, ~3d session window):")
    if bets_per_8h is not None:
        print(f"  Bets:    ~{bets_per_8h:,.0f} rows  -> ~{bet_fetch_mb:.2f} MB (12 cols)")
    else:
        print("  Bets:    (could not estimate rows)")
    if sess_per_poll is not None:
        print(f"  Sessions: ~{sess_per_poll:,.0f} rows  -> ~{sess_fetch_mb:.2f} MB (8 cols)")
    else:
        print("  Sessions: (could not estimate rows)")
    print()
    print("Total data per poll (approx):")
    print(f"  Rows:  bets {bets_per_8h or 0:,.0f} + sessions {sess_per_poll or 0:,.0f}")
    print(f"  Size:  ~{total_fetch_mb:.2f} MB")
    print()
    print("Estimated fetch time (ClickHouse, network + scan):")
    print(f"  Best case (~200 MB/s):  {fetch_time_low:.2f} s")
    print(f"  Typical (~50-100 MB/s): {total_fetch_mb/100:.2f} - {fetch_time_high:.2f} s")
    print(f"  Worst case (~50 MB/s):  {fetch_time_high:.2f} s")
    print()
    print("(Actual time depends on ClickHouse load, indexes, and network.)")


if __name__ == "__main__":
    main()
