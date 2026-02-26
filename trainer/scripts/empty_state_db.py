"""Utility to wipe all data from the SQLite state DB for debugging.

Default DB path: ../local_state/state.db

Safety:
- Prompts for confirmation unless --yes is passed.
- Leaves schema intact; only deletes rows from user tables.
"""
import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "local_state" / "state.db"


def get_user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return [r[0] for r in rows]


def wipe_tables(conn: sqlite3.Connection, tables: list[str]) -> None:
    for tbl in tables:
        conn.execute(f"DELETE FROM {tbl}")
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Empty the SQLite state DB (data only, keep schema)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to SQLite DB (default: ../local_state/state.db)")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    db_path = args.db
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    if not args.yes:
        reply = input(f"This will delete ALL DATA (not schema) in {db_path}. Continue? [y/N]: ")
        if reply.strip().lower() not in {"y", "yes"}:
            print("Aborted.")
            return

    conn = sqlite3.connect(db_path)
    try:
        tables = get_user_tables(conn)
        if not tables:
            print("No user tables found; nothing to wipe.")
            return
        wipe_tables(conn, tables)
        conn.execute("VACUUM")
        print(f"Wiped data from {len(tables)} tables: {', '.join(tables)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
