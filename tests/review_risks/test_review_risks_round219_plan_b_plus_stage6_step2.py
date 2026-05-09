"""Minimal reproducible tests for Round 219 Review — 方案 B+ 階段 6 第 2 步審查風險點.

Round 219 Review (STATUS.md) risk points are turned into contract/source tests.
Tests-only: no production code changes.
"""

from __future__ import annotations


from tests.support.trainer_source_contracts import (
    module_level_def_body,
    pipeline_implementation_source,
    step7_split_runtime_source,
)
import unittest

import trainer.trainer as trainer_mod


def _get_run_pipeline_source() -> str:
    return pipeline_implementation_source()


def _find_step7_sort_and_split_body(source: str | None = None) -> str | None:
    """Return the body of ``_step7_sort_and_split`` in ``step7_split_runtime``."""
    src = source if source is not None else step7_split_runtime_source()
    return module_level_def_body(src, "_step7_sort_and_split")


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

    def test_step9_libsvm_only_cleans_legacy_csv(self):
        """Step 9 must remove legacy Plan B CSV before LibSVM export (B+ / full pipeline)."""
        src = _get_run_pipeline_source()
        self.assertIn(
            "remove_legacy_plan_b_csv_exports",
            src,
            "R219 #1: run_pipeline Step 9 must call remove_legacy_plan_b_csv_exports.",
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
        body = _find_step7_sort_and_split_body()
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
            "step7_keep_train_on_disk",
            block_before,
            "R219 #4: Skip-load valid/test must be under step7_keep_train_on_disk.",
        )
        self.assertIn(
            "step9_export_libsvm",
            block_before,
            "R219 #4: Skip-load valid/test must be under step9_export_libsvm (same decision source).",
        )


if __name__ == "__main__":
    unittest.main()
