from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime

import pandas as pd

from trainer.serving.scorer import _warehouse_timestamp_series_to_hk, update_state_with_new_bets


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        """
        CREATE TABLE session_stats (
            session_id TEXT PRIMARY KEY,
            bet_count INTEGER NOT NULL,
            sum_wager REAL NOT NULL,
            first_ts TEXT,
            last_ts TEXT,
            player_id TEXT,
            table_id TEXT,
            updated_at TEXT
        )
        """
    )
    return conn


class TestScorerIncrementalCursor(unittest.TestCase):
    def test_warehouse_naive_datetime_matches_explicit_utc(self) -> None:
        naive = pd.Series([pd.Timestamp("2025-05-27 08:27:48")])
        utc = pd.Series([pd.Timestamp("2025-05-27 08:27:48", tz="UTC")])
        a = _warehouse_timestamp_series_to_hk(naive).iloc[0]
        b = _warehouse_timestamp_series_to_hk(utc).iloc[0]
        self.assertEqual(a, b)

    def test_incremental_cursor_uses_etl_insert_timestamp(self) -> None:
        conn = _make_conn()
        window_end = datetime.fromisoformat("2026-03-25T13:15:00+08:00")
        batch1 = pd.DataFrame(
            {
                "bet_id": ["b1", "b2"],
                "session_id": ["s1", "s1"],
                "wager": [10.0, 20.0],
                "player_id": ["p1", "p1"],
                "table_id": ["t1", "t1"],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-03-25T12:00:00+08:00", "2026-03-25T12:01:00+08:00"]
                ),
                "__etl_insert_Dtm": pd.to_datetime(
                    ["2026-03-25T12:10:00+08:00", "2026-03-25T12:11:00+08:00"]
                ),
            }
        )
        first_new = update_state_with_new_bets(conn, batch1, window_end)
        self.assertEqual(len(first_new), 2)

        batch2 = pd.DataFrame(
            {
                "bet_id": ["b3", "b4"],
                "session_id": ["s2", "s2"],
                "wager": [10.0, 10.0],
                "player_id": ["p2", "p2"],
                "table_id": ["t2", "t2"],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-03-25T12:00:30+08:00", "2026-03-25T12:00:40+08:00"]
                ),
                "__etl_insert_Dtm": pd.to_datetime(
                    ["2026-03-25T12:12:00+08:00", "2026-03-25T12:10:30+08:00"]
                ),
            }
        )
        second_new = update_state_with_new_bets(conn, batch2, window_end)
        self.assertListEqual(second_new["bet_id"].tolist(), ["b3"])

    def test_legacy_last_processed_end_bootstraps_new_cursor(self) -> None:
        conn = _make_conn()
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('last_processed_end', ?)",
            ("2026-03-25T12:11:00+08:00",),
        )
        conn.commit()
        window_end = datetime.fromisoformat("2026-03-25T13:00:00+08:00")
        batch = pd.DataFrame(
            {
                "bet_id": ["b1", "b2"],
                "session_id": ["s1", "s1"],
                "wager": [10.0, 20.0],
                "player_id": ["p1", "p1"],
                "table_id": ["t1", "t1"],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-03-25T10:00:00+08:00", "2026-03-25T10:01:00+08:00"]
                ),
                "__etl_insert_Dtm": pd.to_datetime(
                    ["2026-03-25T12:11:30+08:00", "2026-03-25T12:10:59+08:00"]
                ),
            }
        )

        new_bets = update_state_with_new_bets(conn, batch, window_end)
        self.assertListEqual(new_bets["bet_id"].tolist(), ["b1"])


if __name__ == "__main__":
    unittest.main()
