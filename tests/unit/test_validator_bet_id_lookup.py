"""Task 9C: TBET lookup by bet_id when player_id+window returns no rows."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from trainer.serving.validator import HK_TZ, fetch_bet_payout_times_by_bet_ids


class TestFetchBetPayoutTimesByBetIds(unittest.TestCase):
    def test_empty_ids_returns_empty(self) -> None:
        m, q, r, f = fetch_bet_payout_times_by_bet_ids([], chunk_size=10)
        self.assertEqual(m, {})
        self.assertEqual(q, 0)
        self.assertEqual(r, 0)
        self.assertEqual(f, 0)

    @patch("trainer.serving.validator.get_clickhouse_client")
    def test_dedup_by_max_payout_and_chunking(self, mock_get_client: MagicMock) -> None:
        client = MagicMock()
        mock_get_client.return_value = client

        def fake_query_df(query: str, parameters: dict) -> pd.DataFrame:
            ids = parameters["ids"]
            if ids == (111,):
                return pd.DataFrame(
                    {
                        "bet_id": [111, 111],
                        "payout_complete_dtm": [
                            pd.Timestamp("2026-03-25 10:00:00", tz=HK_TZ),
                            pd.Timestamp("2026-03-25 12:00:00", tz=HK_TZ),
                        ],
                        "player_id": [1, 999999],
                    }
                )
            if ids == (222,):
                return pd.DataFrame(
                    {
                        "bet_id": [222],
                        "payout_complete_dtm": [pd.Timestamp("2026-03-26 08:00:00", tz=HK_TZ)],
                        "player_id": [2],
                    }
                )
            return pd.DataFrame()

        client.query_df.side_effect = fake_query_df

        m, chunks, rows_raw, failed = fetch_bet_payout_times_by_bet_ids(
            [111, 222],
            chunk_size=1,
        )
        self.assertEqual(failed, 0)
        self.assertEqual(chunks, 2)
        self.assertEqual(rows_raw, 3)
        self.assertEqual(set(m.keys()), {"111", "222"})
        t111, p111 = m["111"]
        self.assertEqual(p111, 999999)
        self.assertEqual(t111, datetime(2026, 3, 25, 12, 0, 0, tzinfo=HK_TZ))
        t222, p222 = m["222"]
        self.assertEqual(p222, 2)
        self.assertEqual(t222, datetime(2026, 3, 26, 8, 0, 0, tzinfo=HK_TZ))

    @patch("trainer.serving.validator.get_clickhouse_client")
    def test_query_failure_counts_failed_not_raise(self, mock_get_client: MagicMock) -> None:
        client = MagicMock()
        mock_get_client.return_value = client
        client.query_df.side_effect = RuntimeError("CH down")

        m, chunks, rows_raw, failed = fetch_bet_payout_times_by_bet_ids([42], chunk_size=50)
        self.assertEqual(m, {})
        self.assertEqual(chunks, 1)
        self.assertEqual(rows_raw, 0)
        self.assertEqual(failed, 1)


if __name__ == "__main__":
    unittest.main()
