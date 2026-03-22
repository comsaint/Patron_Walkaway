"""View alerts from the scorer SQLite state DB.

Default DB path: ``<repo>/local_state/state.db`` (same default as scorer / validator when
``STATE_DB_PATH`` is unset — PLAN Phase 2 DB path consolidation).

Usage:
  python -m trainer.scripts.view_alerts
  python -m trainer.scripts.view_alerts --limit 50
  python -m trainer.scripts.view_alerts --all
  python -m trainer.scripts.view_alerts --db path/to/state.db
  python -m trainer.scripts.view_alerts --csv alerts.csv   # export all rows to CSV
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = _REPO_ROOT / "local_state" / "state.db"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="View alerts from the scorer/validator SQLite state DB"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to SQLite DB (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max number of rows to show (default: 20)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show all alerts (ignores --limit)",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Only print total count of alerts",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        metavar="FILE",
        help="Save all alerts (all rows) to a CSV file",
    )
    args = parser.parse_args()

    db_path = args.db
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        count_row = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()
        total = count_row[0] if count_row else 0
        print(f"Total alerts in DB: {total}")

        if args.count_only and not args.csv:
            return

        if total == 0:
            print("No rows to display.")
            if args.csv:
                print("No rows to write to CSV.")
            return

        limit = None if args.all else args.limit
        if limit is not None and limit <= 0 and not args.csv:
            return

        try:
            import pandas as pd

            # Load full table if exporting to CSV, otherwise limited
            sql_all = "SELECT * FROM alerts ORDER BY ts DESC"
            if args.csv:
                df = pd.read_sql_query(sql_all, conn)
                df.to_csv(args.csv, index=False)
                print(f"Saved {len(df)} rows to {args.csv}")
            else:
                df = pd.read_sql_query(
                    sql_all + (f" LIMIT {limit}" if limit is not None else ""), conn
                )

            if not args.count_only and (args.csv is None or limit is not None or args.all):
                display = df.head(limit) if limit is not None and not args.all else df
                if not display.empty:
                    pd.set_option("display.max_columns", None)
                    pd.set_option("display.width", None)
                    pd.set_option("display.max_colwidth", 40)
                    print(display.to_string(index=False))
        except ImportError:
            # Fallback without pandas
            sql = "SELECT * FROM alerts ORDER BY ts DESC"
            cur = conn.execute(sql)
            col_names = [d[0] for d in cur.description]
            rows = cur.fetchall()
            if args.csv:
                with open(args.csv, "w", newline="", encoding="utf-8") as f:
                    import csv as csv_module
                    w = csv_module.writer(f)
                    w.writerow(col_names)
                    w.writerows(rows)
                print(f"Saved {len(rows)} rows to {args.csv}")
            if not args.count_only:
                to_show = rows if args.all else rows[: limit]
                print("\t".join(col_names))
                for row in to_show:
                    print("\t".join(str(c) if c is not None else "" for c in row))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
