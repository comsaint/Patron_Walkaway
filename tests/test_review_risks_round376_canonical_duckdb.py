"""Round 376 Code Review — build_canonical_links_and_dummy_from_duckdb 錯誤處理與 parity 風險轉成測試。

STATUS.md Round 376 Code Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: STATUS.md « Code Review：Round 376 變更 » (Review #1–#5).
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from trainer.trainer import _CANONICAL_MAP_SESSION_COLS, build_canonical_links_and_dummy_from_duckdb


def _minimal_valid_session_parquet(path: Path, train_end: datetime) -> None:
    """Write a minimal session parquet with required columns so schema/config checks pass."""
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
    cols = [c for c in _CANONICAL_MAP_SESSION_COLS if c in one.columns]
    one[cols].to_parquet(path, index=False)


# ---------------------------------------------------------------------------
# R376 Review #1 — __cause__ 保留且訊息含關鍵字
# ---------------------------------------------------------------------------

class TestR376_1_CauseAndMessageOnFailure(unittest.TestCase):
    """Review #1: On query failure, caller gets RuntimeError with __cause__ and message contains hint."""

    def test_on_keyerror_raises_runtime_error_with_cause_and_hint(self):
        """Mock con.execute().df() raising KeyError; assert RuntimeError, __cause__ is KeyError, message has hint."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_count = [0]

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            # After SET memory_limit, SET threads, SET temp_directory, SET preserve_insertion_order (4 calls), links_sql then dummy_sql
            if call_count[0] >= 5:
                result.df.side_effect = KeyError("some_column")
            return result

        mock_con.execute.side_effect = execute_side_effect
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                with self.assertRaises(RuntimeError) as ctx:
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))
        self.assertIsInstance(ctx.exception.__cause__, KeyError)
        self.assertIn("Canonical mapping DuckDB query failed", ctx.exception.args[0])
        self.assertIn("temp_directory", ctx.exception.args[0], "Hint should suggest temp_directory or threads/memory (PLAN Canonical DuckDB 對齊 Step 7)")


# ---------------------------------------------------------------------------
# R376 Review #2 — dummy 查詢失敗時訊息仍含 DuckDB query failed
# ---------------------------------------------------------------------------

class TestR376_2_DummyQueryFailureMessage(unittest.TestCase):
    """Review #2: When links succeed and dummy query fails, RuntimeError message contains DuckDB query failed."""

    def test_when_dummy_query_fails_message_contains_duckdb_query_failed_and_cause(self):
        """Mock links_sql .df() return empty df, dummy_sql .df() raise; assert message and __cause__."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_count = [0]

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            # call 1-4: SET memory_limit, threads, temp_directory, preserve_insertion_order; 5=links_sql, 6=dummy_sql
            if call_count[0] == 5:
                result.df.return_value = pd.DataFrame(
                    columns=["player_id", "casino_player_id", "lud_dtm"]
                )
            elif call_count[0] == 6:
                result.df.side_effect = RuntimeError("dummy query failed")
            return result

        mock_con.execute.side_effect = execute_side_effect
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                with self.assertRaises(RuntimeError) as ctx:
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))
        self.assertIn("DuckDB query failed", ctx.exception.args[0])
        self.assertIs(ctx.exception.__cause__.__class__, RuntimeError)
        self.assertIn("dummy query failed", str(ctx.exception.__cause__))


# ---------------------------------------------------------------------------
# R376 Review #3 — parity 測試 docstring 須註明 tiebreaker 假設（靜態 guardrail）
# ---------------------------------------------------------------------------

class TestR376_3_ParityTestDocumentsTiebreakerAssumption(unittest.TestCase):
    """Review #3: Parity test module must document that it assumes single row per session_id / no tiebreaker."""

    def test_parity_test_module_docstring_mentions_tiebreaker_or_single_row_assumption(self):
        """Parity test docstring should mention tiebreaker or __etl_insert_Dtm or single row per session."""
        import tests.test_canonical_mapping_duckdb_pandas_parity as parity_mod

        doc = (parity_mod.__doc__ or "") + (getattr(parity_mod.TestCanonicalMappingDuckDbPandasParity, "__doc__") or "")
        self.assertIn(
            "session_id",
            doc,
            "Parity test docstring should document session_id / tiebreaker assumption (R376 Review #3)",
        )
        self.assertTrue(
            "tiebreaker" in doc or "僅一筆" in doc or "__etl_insert_Dtm" in doc or "single row" in doc.lower(),
            "Parity test must mention tiebreaker or single row per session_id (R376 Review #3)",
        )


# ---------------------------------------------------------------------------
# R376 Review #4 — 長訊息例外時仍含 hint
# ---------------------------------------------------------------------------

class TestR376_4_LongExceptionMessageStillIncludesHint(unittest.TestCase):
    """Review #4: When original exception has very long message, re-raised RuntimeError still includes hint."""

    def test_on_long_exception_message_still_includes_hint(self):
        """Mock exception with 2000-char message; assert RuntimeError message contains CANONICAL_MAP hint."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_count = [0]

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] >= 5:  # links_sql then dummy_sql (after 4 SETs)
                result.df.side_effect = RuntimeError("x" * 2000)
            return result

        mock_con.execute.side_effect = execute_side_effect
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                with self.assertRaises(RuntimeError) as ctx:
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))
        self.assertIn("temp_directory", ctx.exception.args[0], "Hint should suggest temp_directory or threads/memory (PLAN Canonical DuckDB 對齊 Step 7)")
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# R376 Review #5 — 查詢失敗時確為 RuntimeError 且含 hint（契約測試）
# ---------------------------------------------------------------------------

class TestR376_5_QueryFailureContract(unittest.TestCase):
    """Review #5: DuckDB query failure must raise RuntimeError with message and __cause__ (contract test)."""

    def test_query_failure_raises_runtime_error_with_cause_and_hint(self):
        """Any exception from execute().df() yields RuntimeError, message has both key strings, __cause__ set."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_count = [0]

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] >= 5:  # links_sql then dummy_sql (after 4 SETs)
                result.df.side_effect = RuntimeError("Out of Memory")
            return result

        mock_con.execute.side_effect = execute_side_effect
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                with self.assertRaises(RuntimeError) as ctx:
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))
        self.assertIn("Canonical mapping DuckDB query failed", ctx.exception.args[0])
        self.assertIn("temp_directory", ctx.exception.args[0], "Hint should suggest temp_directory or threads/memory (PLAN Canonical DuckDB 對齊 Step 7)")
        self.assertIsNotNone(ctx.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
