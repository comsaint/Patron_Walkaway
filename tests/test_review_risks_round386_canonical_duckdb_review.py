"""Round 386 Code Review — build_canonical_links_and_dummy_from_duckdb 風險點轉最小可重現測試。

STATUS.md Round 386 Code Review: 將 Reviewer 提到的 7 項風險轉成 tests-only 的
最小可重現測試或 source/lint 契約。不修改 production code。

對應項目：1=可寫性, 2=Windows 路徑, 3=SET temp_directory 失敗 log, 4=hint/磁碟,
5=路徑來源, 6=docstring 共用目錄, 7=單引號 fallback 存在。
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from trainer.trainer import (
    _CANONICAL_MAP_SESSION_COLS,
    build_canonical_links_and_dummy_from_duckdb,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")


def _minimal_valid_session_parquet(path: Path, train_end: datetime) -> None:
    """Minimal session parquet so schema/config checks pass."""
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


def _get_func_src(name: str) -> str:
    tree = ast.parse(_TRAINER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_TRAINER_SRC, node) or ""
    return ""


# ---------------------------------------------------------------------------
# Review #1 — 錯誤時 hint 應提到 temp 可寫性
# ---------------------------------------------------------------------------

class TestR386_1_HintMentionsWritable(unittest.TestCase):
    """Review #1: When DuckDB query fails, hint should mention temp_directory writability."""

    def test_failure_hint_contains_writable_and_temp_directory(self):
        """RuntimeError hint must contain 'writable' and 'temp_directory' for spill/permission debugging."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_count = [0]

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] >= 5:
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
        msg = ctx.exception.args[0]
        self.assertIn("temp_directory", msg, "Hint should mention temp_directory (Review #1/#4)")
        self.assertIn("writable", msg, "Hint should mention writable for permission debugging (Review #1)")


# ---------------------------------------------------------------------------
# Review #2 — Windows 路徑（含反斜線）不應導致崩潰
# ---------------------------------------------------------------------------

class TestR386_2_WindowsStylePath(unittest.TestCase):
    """Review #2: Path with backslashes (Windows-style) should not crash when passed to DuckDB."""

    def test_windows_style_temp_path_does_not_crash(self):
        """When DATA_DIR is Windows-style (backslashes), function completes with mocked DuckDB."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_count = [0]
        empty_links = pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"])

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] >= 5:
                result.df.return_value = empty_links
            return result

        mock_con.execute.side_effect = execute_side_effect
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            # Simulate Windows-style path for temp dir (e.g. C:\...\trainer\.data\duckdb_tmp)
            win_style = str(Path(tmp) / "duckdb_tmp").replace("/", "\\")
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                with patch("trainer.trainer.DATA_DIR", Path(win_style.replace("\\duckdb_tmp", ""))):
                    Path(win_style).mkdir(parents=True, exist_ok=True)
                    links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(
                        path, datetime(2025, 2, 1)
                    )
        self.assertIsInstance(links_df, pd.DataFrame)
        self.assertIsInstance(dummy_pids, set)


# ---------------------------------------------------------------------------
# Review #3 — SET temp_directory 失敗時應 log warning 且繼續執行
# ---------------------------------------------------------------------------

class TestR386_3_SetTempDirectoryFailureLogsWarning(unittest.TestCase):
    """Review #3: When SET temp_directory raises, log warning and continue (non-fatal)."""

    def test_set_temp_directory_failure_logs_warning_and_returns(self):
        """Mock SET temp_directory to raise; assert warning logged and function still returns."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_count = [0]
        empty_links = pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"])

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 3:
                # 3rd execute = SET temp_directory
                raise OSError("temp_directory set failed")
            if call_count[0] >= 5:
                result.df.return_value = empty_links
            return result

        mock_con.execute.side_effect = execute_side_effect
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                with self.assertLogs("trainer", level="WARNING") as log_ctx:
                    links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(
                        path, datetime(2025, 2, 1)
                    )
        self.assertIsInstance(links_df, pd.DataFrame)
        self.assertIsInstance(dummy_pids, set)
        warnings = [r.msg for r in log_ctx.records if "temp_directory" in r.msg]
        self.assertTrue(
            any("SET temp_directory failed" in w for w in warnings),
            "Log should contain 'SET temp_directory failed' (Review #3). Got: %s" % warnings,
        )


# ---------------------------------------------------------------------------
# Review #4 — 錯誤 hint 應有助辨識 temp/磁碟（與 #1 重疊，用 source 契約補強）
# ---------------------------------------------------------------------------

class TestR386_4_HintOrSourceMentionsTempDirectory(unittest.TestCase):
    """Review #4: Failure hint or docstring should mention temp_directory / disk space."""

    def test_failure_hint_contains_temp_directory(self):
        """RuntimeError on query failure must include temp_directory in hint."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_count = [0]

        def execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] >= 5:
                result.df.side_effect = IOError("No space left on device")
            return result

        mock_con.execute.side_effect = execute_side_effect
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                with self.assertRaises(RuntimeError) as ctx:
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))
        self.assertIn("temp_directory", ctx.exception.args[0], "Hint should help identify temp/disk (Review #4)")


# ---------------------------------------------------------------------------
# Review #5 — 路徑來源契約：目前應來自 DATA_DIR，無 config 覆寫
# ---------------------------------------------------------------------------

class TestR386_5_TempDirSourceUsesDataDir(unittest.TestCase):
    """Review #5: Until config override exists, temp dir must come from DATA_DIR (source guard)."""

    def test_temp_dir_assignment_uses_data_dir_and_duckdb_tmp(self):
        """Source of build_canonical_links_and_dummy_from_duckdb must use DATA_DIR and duckdb_tmp."""
        src = _get_func_src("build_canonical_links_and_dummy_from_duckdb")
        self.assertIn("DATA_DIR", src, "Temp dir should come from DATA_DIR (Review #5)")
        self.assertIn("duckdb_tmp", src, "Temp dir should be duckdb_tmp (Review #5)")


# ---------------------------------------------------------------------------
# Review #6 — Docstring 應註明與 Step 7 共用目錄（契約，待 production 補上後通過）
# ---------------------------------------------------------------------------

class TestR386_6_DocstringShouldMentionSharedDirWithStep7(unittest.TestCase):
    """Review #6: Docstring or comment should mention shared duckdb_tmp with Step 7 / rmtree."""

    def test_docstring_or_comment_mentions_shared_duckdb_tmp_with_step7(self):
        """Source (docstring or comment) must mention duckdb_tmp and Step 7 / shared / rmtree (Review #6)."""
        src = _get_func_src("build_canonical_links_and_dummy_from_duckdb")
        self.assertIn("duckdb_tmp", src)
        self.assertTrue(
            "Step 7" in src or "共用" in src or "shared" in src or "rmtree" in src,
            "Docstring or comment should mention Step 7 / shared / rmtree (Review #6)",
        )


# ---------------------------------------------------------------------------
# Review #7 — 單引號 fallback 分支存在（為日後 config 預留）
# ---------------------------------------------------------------------------

class TestR386_7_SourceHasFallbackForQuoteInTempDir(unittest.TestCase):
    """Review #7: Source must have fallback when temp_dir_raw contains single quote (for future config)."""

    def test_source_has_quote_fallback_branch(self):
        """Code must contain if \"'\" in temp_dir_raw (or equivalent) for future CANONICAL_MAP_DUCKDB_TEMP_DIR."""
        src = _get_func_src("build_canonical_links_and_dummy_from_duckdb")
        # Must have "if ... in temp_dir_raw" (fallback when path contains quote).
        self.assertRegex(
            src,
            r"if\s+.+in\s+temp_dir_raw",
            "Fallback for quote in path (Review #7)",
        )


if __name__ == "__main__":
    unittest.main()
