"""Minimal reproducible tests for Round 213 Review — DuckDB temp 目錄清理風險點.

Review risks (Round 213 Review in STATUS.md) are turned into contract tests.
Tests that document desired behaviour not yet in production use @unittest.expectedFailure.
Tests-only: no production code changes.
"""

from __future__ import annotations

import inspect
import unittest

import trainer.trainer as trainer_mod


# ---------------------------------------------------------------------------
# R213 Review #1 — 安全性：僅允許刪除 DATA_DIR 下或等於 DATA_DIR/duckdb_tmp 的路徑
# ---------------------------------------------------------------------------

class TestR213Step7CleanupRestrictsPathToDataDir(unittest.TestCase):
    """R213 Review #1: _step7_clean_duckdb_temp_dir must only delete when path is under DATA_DIR (whitelist)."""

    def test_step7_clean_duckdb_temp_dir_guards_rmtree_with_data_dir_check(self):
        """Before shutil.rmtree, cleanup must ensure effective path is under DATA_DIR or equals DATA_DIR/duckdb_tmp.

        Prevents accidental deletion of system or user dirs when STEP7_DUCKDB_TEMP_DIR is misconfigured.
        """
        src = inspect.getsource(trainer_mod.run_pipeline)
        idx_def = src.find("def _step7_clean_duckdb_temp_dir()")
        self.assertGreater(idx_def, -1, "_step7_clean_duckdb_temp_dir not found")
        idx_rmtree = src.find("shutil.rmtree(effective)", idx_def)
        self.assertGreater(idx_rmtree, -1, "shutil.rmtree(effective) not found in cleanup")
        # Function body from def up to and including the rmtree line
        segment = src[idx_def:idx_rmtree]
        # Require a guard that ties allowed deletion to DATA_DIR (e.g. resolve + DATA_DIR)
        has_data_dir_guard = "DATA_DIR" in segment and "resolve" in segment
        self.assertTrue(
            has_data_dir_guard,
            "R213 Review #1: _step7_clean_duckdb_temp_dir must guard rmtree with DATA_DIR (e.g. resolve and check under DATA_DIR).",
        )


# ---------------------------------------------------------------------------
# R213 Review #2 — 邊界：文件註明不建議多 process 共用同一 temp 目錄（可選）
# ---------------------------------------------------------------------------

class TestR213Step7TempDirDocstringOrConfigMentionsSingleProcess(unittest.TestCase):
    """R213 Review #2 (optional): Doc or config should mention not sharing STEP7_DUCKDB_TEMP_DIR across processes."""

    def test_step7_duckdb_temp_dir_documented_or_in_config(self):
        """Config or trainer docstring/source should mention temp dir is per-run or not for multi-process sharing."""
        # Check config has STEP7_DUCKDB_TEMP_DIR and optionally a comment
        import trainer.config as config_mod

        self.assertTrue(
            hasattr(config_mod, "STEP7_DUCKDB_TEMP_DIR"),
            "STEP7_DUCKDB_TEMP_DIR must exist in config",
        )
        config_src = inspect.getsource(config_mod)
        # Optional: comment or docstring near STEP7_DUCKDB_TEMP_DIR mentioning temp / spill / single process
        # We only require the constant exists; comment is optional for this test to pass
        self.assertIn("STEP7_DUCKDB_TEMP_DIR", config_src)


# ---------------------------------------------------------------------------
# R213 Review #4 — 契約：清理僅在 DuckDB 成功路徑呼叫、路徑與 _duckdb_sort_and_split 一致
# ---------------------------------------------------------------------------

class TestR213Step7CleanupCalledOnlyOnDuckDBSuccessPaths(unittest.TestCase):
    """R213 Review #4: Cleanup must be called in every DuckDB success return path (six: try×3 + retry×3)."""

    def test_step7_clean_duckdb_temp_dir_called_in_run_pipeline(self):
        """run_pipeline source must call _step7_clean_duckdb_temp_dir in Step 7 DuckDB success paths."""
        src = inspect.getsource(trainer_mod.run_pipeline)
        # One occurrence is the def; the rest are calls
        total = src.count("_step7_clean_duckdb_temp_dir()")
        call_count = total - 1
        self.assertEqual(
            call_count,
            6,
            "R213: _step7_clean_duckdb_temp_dir() must be called in every DuckDB success path "
            "(try: KEEP+LIBSVM, try: KEEP+no LIBSVM, try: not KEEP; retry: same three = 6).",
        )


if __name__ == "__main__":
    unittest.main()
