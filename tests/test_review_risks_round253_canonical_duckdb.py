"""Round 253 Code Review — build_canonical_links_and_dummy_from_duckdb 風險點轉成測試。

STATUS.md Round 253 Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § Canonical mapping 全歷史 Step 2, STATUS Round 253 Review.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

# Required by the DuckDB view (trainer._CANONICAL_MAP_SESSION_COLS + filter columns)
_REQUIRED_VIEW_COLS = [
    "session_id", "player_id", "casino_player_id",
    "lud_dtm", "session_start_dtm", "session_end_dtm",
    "is_manual", "is_deleted", "is_canceled", "num_games_with_wager",
    "turnover",
]


def _make_minimal_valid_session_parquet(path: Path, train_end: datetime) -> None:
    """Write a minimal session parquet with one row that passes DQ and time filter."""
    one = pd.DataFrame([{
        "session_id": "s1",
        "player_id": 10,
        "casino_player_id": " C1 ",
        "lud_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "session_start_dtm": pd.Timestamp("2025-01-15 11:00:00"),
        "session_end_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "is_manual": 0,
        "is_deleted": 0,
        "is_canceled": 0,
        "num_games_with_wager": 1,
        "turnover": 10.0,
    }])
    one.to_parquet(path, index=False)


# ---------------------------------------------------------------------------
# R253 Review #1 — 缺少必要欄位時錯誤不友善
# ---------------------------------------------------------------------------

class TestR253_1_MissingRequiredColumnRaises(unittest.TestCase):
    """Review #1: Parquet missing required column (e.g. turnover) should lead to a clear failure."""

    def test_parquet_missing_turnover_raises(self):
        """When session parquet lacks 'turnover', call fails (DuckDB or future ValueError with 'missing')."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            # Parquet with all cols except turnover
            df = pd.DataFrame([{
                "session_id": "s1",
                "player_id": 10,
                "casino_player_id": "C1",
                "lud_dtm": pd.Timestamp("2025-01-15 12:00:00"),
                "session_start_dtm": pd.Timestamp("2025-01-15 11:00:00"),
                "session_end_dtm": pd.Timestamp("2025-01-15 12:00:00"),
                "is_manual": 0,
                "is_deleted": 0,
                "is_canceled": 0,
                "num_games_with_wager": 1,
                # turnover missing
            }])
            df.to_parquet(path, index=False)

            train_end = datetime(2025, 2, 1)
            with self.assertRaises(Exception) as ctx:
                build_canonical_links_and_dummy_from_duckdb(path, train_end)
            # When production adds explicit check: assert ValueError and "missing required columns".
            # Until then: either DuckDB reports missing column (turnover/column) or parameter binding fails (Review #6).
            msg = str(ctx.exception).lower()
            self.assertTrue(
                "turnover" in msg or "column" in msg or "parameter" in msg or "prepared" in msg or "binder" in msg,
                f"Failure should indicate schema/parameter issue (Review #1/#6); got: {ctx.exception!r}",
            )


# ---------------------------------------------------------------------------
# R253 Review #2 — 發生例外時 connection 未關閉
# ---------------------------------------------------------------------------

class TestR253_2_ConnectionClosedOnException(unittest.TestCase):
    """Review #2: When execute() raises, connection.close() must be called (resource leak)."""

    def test_duckdb_connection_close_called_when_execute_raises(self):
        """When first con.execute() raises, close() must have been called."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            _make_minimal_valid_session_parquet(path, datetime(2025, 2, 1))

            mock_con = MagicMock()
            mock_con.execute.side_effect = RuntimeError("fake OOM")
            mock_con.__enter__ = MagicMock(return_value=mock_con)
            mock_con.__exit__ = MagicMock(return_value=False)

            with patch("duckdb.connect", return_value=mock_con):
                try:
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))
                except RuntimeError:
                    pass
                mock_con.close.assert_called_once_with()


# ---------------------------------------------------------------------------
# R253 Review #4 — memory_limit / threads 未驗證
# ---------------------------------------------------------------------------

class TestR253_4_InvalidMemoryLimitRaisesOrFails(unittest.TestCase):
    """Review #4: CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB=-1 should not be passed through unsafely."""

    def test_negative_max_gb_raises_or_duckdb_fails(self):
        """When MAX_GB is -1, call should raise (our validation or DuckDB error), not succeed silently."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            _make_minimal_valid_session_parquet(path, datetime(2025, 2, 1))

            fake_cfg = MagicMock()
            fake_cfg.CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB = -1.0
            fake_cfg.CANONICAL_MAP_DUCKDB_THREADS = 2
            fake_cfg.PLACEHOLDER_PLAYER_ID = -1
            fake_cfg.CASINO_PLAYER_ID_CLEAN_SQL = (
                "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END"
            )

            with patch("trainer.trainer._cfg", fake_cfg):
                with self.assertRaises(Exception):
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))


class TestR253_4_ZeroThreadsRaisesOrFails(unittest.TestCase):
    """Review #4: CANONICAL_MAP_DUCKDB_THREADS=0 should not be passed through unsafely."""

    def test_zero_threads_raises_or_duckdb_fails(self):
        """When THREADS is 0, call should raise or DuckDB reject (not succeed with 0 threads)."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            _make_minimal_valid_session_parquet(path, datetime(2025, 2, 1))

            fake_cfg = MagicMock()
            fake_cfg.CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB = 6.0
            fake_cfg.CANONICAL_MAP_DUCKDB_THREADS = 0
            fake_cfg.PLACEHOLDER_PLAYER_ID = -1
            fake_cfg.CASINO_PLAYER_ID_CLEAN_SQL = (
                "CASE WHEN lower(trim(casino_player_id)) IN ('', 'null') THEN NULL ELSE trim(casino_player_id) END"
            )

            with patch("trainer.trainer._cfg", fake_cfg):
                with self.assertRaises(Exception):
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))


# ---------------------------------------------------------------------------
# R253 Review #5 — CASINO_PLAYER_ID_CLEAN_SQL 直接嵌入 SQL
# ---------------------------------------------------------------------------

class TestR253_5_MaliciousCleanSqlRaises(unittest.TestCase):
    """Review #5: If config CASINO_PLAYER_ID_CLEAN_SQL contains ';' or subquery, execution should fail (not run arbitrary SQL)."""

    def test_clean_sql_with_semicolon_raises(self):
        """When clean_sql contains ';', call should raise (parse error or our validation)."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            _make_minimal_valid_session_parquet(path, datetime(2025, 2, 1))

            fake_cfg = MagicMock()
            fake_cfg.CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB = 6.0
            fake_cfg.CANONICAL_MAP_DUCKDB_THREADS = 2
            fake_cfg.PLACEHOLDER_PLAYER_ID = -1
            fake_cfg.CASINO_PLAYER_ID_CLEAN_SQL = "; SELECT 1"

            with patch("trainer.trainer._cfg", fake_cfg):
                with self.assertRaises(Exception):
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))


# ---------------------------------------------------------------------------
# R253 Review #6 — read_parquet(?) 參數綁定相容性 / 整合
# ---------------------------------------------------------------------------

class TestR253_6_ValidParquetReturnsCorrectStructure(unittest.TestCase):
    """Review #6: With valid small parquet, function returns (links_df, dummy_pids) with correct structure."""

    def test_minimal_valid_parquet_returns_links_and_dummy_structure(self):
        """Call with minimal valid session parquet; assert links have [player_id, casino_player_id, lud_dtm], dummy is set."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            train_end = datetime(2025, 2, 1)
            _make_minimal_valid_session_parquet(path, train_end)

            links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(path, train_end)

            self.assertIn("player_id", links_df.columns, "links must have player_id (Review #6)")
            self.assertIn("casino_player_id", links_df.columns, "links must have casino_player_id (Review #6)")
            self.assertIn("lud_dtm", links_df.columns, "links must have lud_dtm (Review #6)")
            self.assertIsInstance(dummy_pids, set, "dummy_pids must be a set (Review #6)")
            # All elements should be int (player_id)
            if dummy_pids:
                for pid in dummy_pids:
                    self.assertIsInstance(pid, int, "dummy_pids must contain int (Review #6)")


# ---------------------------------------------------------------------------
# R253 Review #3 (optional) — train_end 時區：僅鎖定 naive 情境行為
# ---------------------------------------------------------------------------

class TestR253_3_NaiveTrainEndWithNaiveParquetSucceeds(unittest.TestCase):
    """Review #3 (optional): With naive train_end and naive parquet timestamps, call succeeds and structure is correct."""

    def test_naive_train_end_returns_consistent_structure(self):
        """Naive train_end + parquet with naive timestamps: no crash; links/dummy structure as in #6."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            train_end = datetime(2025, 2, 1)  # naive
            _make_minimal_valid_session_parquet(path, train_end)

            links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(path, train_end)

            self.assertIn("player_id", links_df.columns)
            self.assertIn("casino_player_id", links_df.columns)
            self.assertIn("lud_dtm", links_df.columns)
            self.assertIsInstance(dummy_pids, set)


if __name__ == "__main__":
    unittest.main()
