"""Minimal reproducible tests for Round 219 Review — 方案 B+ 階段 6 第 2 步審查風險點.

Round 219 Review (STATUS.md) risk points are turned into contract/source tests.
Tests-only: no production code changes.
"""

from __future__ import annotations

import inspect
import re
import unittest

import trainer.trainer as trainer_mod


def _get_run_pipeline_source() -> str:
    return inspect.getsource(trainer_mod.run_pipeline)


def _find_step7_sort_and_split_body(source: str) -> str | None:
    """Return the body of _step7_sort_and_split (nested inside run_pipeline)."""
    start = source.find("def _step7_sort_and_split(")
    if start == -1:
        return None
    rest = source[start:]
    # End at next top-level section comment or next def at same indent
    end_m = re.search(r"\n    # [0-9]+\. Load all chunks|\n    def _step7_pandas_fallback\(", rest)
    end = end_m.start() if end_m else len(rest)
    return rest[:end]


# ---------------------------------------------------------------------------
# R219 Review #1 — 邊界條件：B+ 路徑下 valid_df/test_df 為 None 時有守衛
# ---------------------------------------------------------------------------

class TestR219BPlusValidTestNoneGuarded(unittest.TestCase):
    """R219 Review #1: When valid_df/test_df can be None (B+ path), log/print and Plan B export must be guarded."""

    def test_step7_uses_n_valid_print_n_test_print_instead_of_raw_len(self):
        """run_pipeline must use _n_valid_print / _n_test_print so valid/test counts work when valid_df/test_df is None."""
        src = _get_run_pipeline_source()
        self.assertIn(
            "_n_valid_print = _n_valid if valid_df is None else len(valid_df)",
            src,
            "R219 #1: Step 7 must use _n_valid_print (from _n_valid when valid_df is None) for log/print.",
        )
        self.assertIn(
            "_n_test_print = _n_test if test_df is None else len(test_df)",
            src,
            "R219 #1: Step 7 must use _n_test_print (from _n_test when test_df is None) for log/print.",
        )

    def test_plan_b_csv_export_guarded_by_valid_df_not_none(self):
        """Plan B CSV export must be skipped when valid_df is None (B+ LibSVM path)."""
        src = _get_run_pipeline_source()
        self.assertIn(
            "STEP9_TRAIN_FROM_FILE and train_df is not None and valid_df is not None",
            src,
            "R219 #1: Plan B CSV export must run only when train_df and valid_df are not None.",
        )


# ---------------------------------------------------------------------------
# R219 Review #2 — 邊界條件：else 分支必須設定 _n_valid / _n_test
# ---------------------------------------------------------------------------

class TestR219ElseBranchSetsNValidNTest(unittest.TestCase):
    """R219 Review #2: When step7_train_path is None, else branch must set _n_valid and _n_test for _n_valid_print use."""

    def test_else_branch_sets_n_valid_and_n_test(self):
        """In run_pipeline, the else branch (step7_train_path is None) must assign _n_valid and _n_test."""
        src = _get_run_pipeline_source()
        # Find the else block that follows "if step7_train_path is not None"
        idx = src.find("if step7_train_path is not None:")
        self.assertGreater(idx, -1, "step7_train_path block not found")
        # The else branch is the one that has assert train_df is not None and assert valid_df/test_df
        else_start = src.find("else:", idx)
        self.assertGreater(else_start, idx, "else for step7_train_path not found")
        # Look at a reasonable window (next ~800 chars) for _n_valid = and _n_test =
        segment = src[else_start : else_start + 900]
        self.assertIn(
            "_n_valid = ",
            segment,
            "R219 #2: else branch must set _n_valid so _n_valid_print is defined when valid_df is not None.",
        )
        self.assertIn(
            "_n_test = ",
            segment,
            "R219 #2: else branch must set _n_test so _n_test_print is defined when test_df is not None.",
        )


# ---------------------------------------------------------------------------
# R219 Review #3 — 契約：B+ 路徑下目前保留 valid/test Parquet（未 unlink）
# ---------------------------------------------------------------------------

class TestR219BPlusValidTestParquetNotUnlinked(unittest.TestCase):
    """R219 Review #3: Current contract — B+ path does not unlink step7_valid_path / step7_test_path (kept on disk)."""

    def test_bplus_block_does_not_unlink_valid_test_paths(self):
        """run_pipeline must not unlink step7_valid_path or step7_test_path in the B+ block (current behavior: keep on disk)."""
        src = _get_run_pipeline_source()
        # Current production only unlinks step7_train_path after loading train. Valid/test are kept.
        self.assertNotIn(
            "step7_valid_path.unlink",
            src,
            "R219 #3 contract: step7_valid_path is not unlinked in B+ path (document current behavior).",
        )
        self.assertNotIn(
            "step7_test_path.unlink",
            src,
            "R219 #3 contract: step7_test_path is not unlinked in B+ path (document current behavior).",
        )


# ---------------------------------------------------------------------------
# R219 Review #4 — 契約：不載入 valid/test 僅當 STEP7_KEEP_TRAIN_ON_DISK and STEP9_EXPORT_LIBSVM
# ---------------------------------------------------------------------------

class TestR219SkipLoadValidTestOnlyWhenBothFlags(unittest.TestCase):
    """R219 Review #4: Skip loading valid/test only when STEP7_KEEP_TRAIN_ON_DISK and STEP9_EXPORT_LIBSVM both True."""

    def test_step7_returns_none_none_none_only_when_keep_disk_and_libsvm(self):
        """_step7_sort_and_split return (None, None, None, paths) must be guarded by both STEP7_KEEP_TRAIN_ON_DISK and STEP9_EXPORT_LIBSVM."""
        src = _get_run_pipeline_source()
        body = _find_step7_sort_and_split_body(src)
        self.assertIsNotNone(body, "_step7_sort_and_split body not found")
        self.assertIn(
            "return (None, None, None, train_path, valid_path, test_path)",
            body,
            "B+ 階段 6 第 2 步: skip-load path must exist.",
        )
        # The block that does this return must check both flags
        idx_return = body.find("return (None, None, None, train_path, valid_path, test_path)")
        block_before = body[: idx_return + 1]
        self.assertIn(
            "STEP7_KEEP_TRAIN_ON_DISK",
            block_before,
            "R219 #4: Skip-load valid/test must be under STEP7_KEEP_TRAIN_ON_DISK.",
        )
        self.assertIn(
            "STEP9_EXPORT_LIBSVM",
            block_before,
            "R219 #4: Skip-load valid/test must be under STEP9_EXPORT_LIBSVM (same decision source).",
        )


if __name__ == "__main__":
    unittest.main()
