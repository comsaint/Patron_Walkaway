"""WS4 v2: cross-entry data preflight (local bridge vs ClickHouse)."""

from __future__ import annotations

import logging
import unittest
from unittest.mock import MagicMock, patch

from trainer.training.cross_entry_preflight import run_cross_entry_data_preflight


class TestCrossEntryPreflight(unittest.TestCase):
    def test_local_delegates_to_bridge_ensure(self) -> None:
        log = logging.getLogger("test_ws4")
        with patch(
            "trainer.training.local_bridge_preflight.ensure_local_bridge_ready_for_training"
        ) as m_ensure:
            run_cross_entry_data_preflight(
                entry="trainer",
                use_local_parquet=True,
                logger=log,
            )
        m_ensure.assert_called_once_with(logger=log)

    def test_clickhouse_path_calls_query_df(self) -> None:
        log = logging.getLogger("test_ws4b")
        with patch("trainer.core.db_conn.query_df", return_value=MagicMock()) as m_core:
            run_cross_entry_data_preflight(
                entry="scorer",
                use_local_parquet=False,
                logger=log,
            )
        m_core.assert_called_once_with("SELECT 1 AS _ok")

    def test_clickhouse_failure_wraps_runtimeerror(self) -> None:
        log = logging.getLogger("test_ws4c")
        with patch(
            "trainer.core.db_conn.query_df",
            side_effect=OSError("boom"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                run_cross_entry_data_preflight(
                    entry="validator",
                    use_local_parquet=False,
                    logger=log,
                )
        self.assertIn("DataPreflight[validator]", str(ctx.exception))
        self.assertIn("ClickHouse check failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
