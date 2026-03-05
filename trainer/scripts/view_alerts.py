"""View alerts from the scorer SQLite state DB.

Default DB path: trainer/local_state/state.db (same as scorer, validator, api_server).

Usage:
  python -m trainer.scripts.view_alerts
  python -m trainer.scripts.view_alerts --limit 50
  python -m trainer.scripts.view_alerts --all
  python -m trainer.scripts.view_alerts --db path/to/state.db
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "local_state" / "state.db"


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
    args = parser.parse_args()

    db_path = args.db
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        count_row = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()
        total = count_row[0] if count_row else 0
        print(f"Total alerts in DB: {total}")

        if args.count_only:
            return

        if total == 0:
            print("No rows to display.")
            return

        limit = None if args.all else args.limit
        if limit is not None and limit <= 0:
            return

        try:
            import pandas as pd

            sql = "SELECT * FROM alerts ORDER BY ts DESC"
            if limit is not None:
                sql += f" LIMIT {limit}"
            df = pd.read_sql_query(sql, conn)
            pd.set_option("display.max_columns", None)
            pd.set_option("display.width", None)
            pd.set_option("display.max_colwidth", 40)
            print(df.to_string(index=False))
        except ImportError:
            # Fallback without pandas
            sql = "SELECT * FROM alerts ORDER BY ts DESC"
            if limit is not None:
                sql += f" LIMIT {limit}"
            cur = conn.execute(sql)
            col_names = [d[0] for d in cur.description]
            print("\t".join(col_names))
            for row in cur:
                print("\t".join(str(c) if c is not None else "" for c in row))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
