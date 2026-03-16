"""Minimal reproducible tests for Round 177 Review — Step 7 orchestrator wiring.

Tests-only: no production code changes.
Review risks (Round 177 Review in STATUS.md) are turned into contract/source
checks; tests that document current gaps use @unittest.expectedFailure.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


def _get_trainer_source() -> str:
    path = Path(__file__).resolve().parents[2] / "trainer" / "training" / "trainer.py"
    return path.read_text(encoding="utf-8")


def _find_step7_sort_and_split_body(source: str) -> str | None:
    """Return the body of _step7_sort_and_split (from def to next section/def)."""
    start = source.find("def _step7_sort_and_split(")
    if start == -1:
        return None
    rest = source[start:]
    end_match = re.search(r"\n    # [0-9]+\. Load all chunks|\n    def [a-z_]+\(|\n    # [0-9]+\. ", rest)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


def _find_duckdb_sort_and_split_body(source: str) -> str | None:
    """Return the body of _duckdb_sort_and_split."""
    start = source.find("def _duckdb_sort_and_split(")
    if start == -1:
        return None
    rest = source[start:]
    end_match = re.search(r"\n    def [a-z_]+\(|\n    # [0-9]+\. ", rest)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


def _find_oom_check_body(source: str) -> str | None:
    """Return the body of _oom_check_and_adjust_neg_sample_frac (module-level def)."""
    start = source.find("def _oom_check_and_adjust_neg_sample_frac(")
    if start == -1:
        return None
    rest = source[start:]
    # Next top-level def (no leading spaces)
    end_match = re.search(r"\ndef [a-z_]+\(|\n[a-z_A-Z].*^def ", rest, re.MULTILINE)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


def _find_step7_main_block(source: str) -> str | None:
    """Return the Step 7 main block (R803 + _step7_sort_and_split call)."""
    marker = "# 5. Load all chunks, sort, row-level train/valid/test split"
    start = source.find(marker)
    if start == -1:
        return None
    rest = source[start : start + 3500]
    # Up to next step (Step 8 or active_feature_cols)
    end_m = re.search(r"active_feature_cols = get_all_candidate_feature_ids|# 5b\. Full-feature screening", rest)
    end = end_m.start() if end_m else len(rest)
    return rest[:end]


# --- Contract: OOM estimate when STEP7_USE_DUCKDB=True should use train split only (smaller). ---
def _oom_estimate_duckdb_path(on_disk: float, factor: float, train_frac: float) -> float:
    """Desired formula when STEP7_USE_DUCKDB: peak = on_disk * factor * train_frac (read back train only)."""
    return on_disk * factor * train_frac


def _oom_estimate_pandas_path(on_disk: float, factor: float, train_frac: float) -> float:
    """Current formula: full concat + train split coexist: on_disk * factor * (1 + train_frac)."""
    return on_disk * factor * (1.0 + train_frac)


class TestR177Step7SplitsCleanedAfterSuccess(unittest.TestCase):
    """Round 177 Review #1: PLAN requires cleanup of step7_splits parquets after read."""

    def test_r177_orchestrator_cleans_split_parquets_after_read(self):
        """Production _step7_sort_and_split should unlink train/valid/test parquets after successful read (PLAN Step 7)."""
        source = _get_trainer_source()
        body = _find_step7_sort_and_split_body(source)
        self.assertIsNotNone(body)
        # Contract: after read_parquet, should unlink/delete the three paths before return.
        has_unlink = "unlink" in body or "missing_ok" in body
        has_split_path = "train_path" in body and "valid_path" in body and "test_path" in body
        self.assertTrue(
            has_unlink and has_split_path,
            "Orchestrator should unlink train_path/valid_path/test_path after read (PLAN Step 7 cleanup)",
        )


class TestR177Step7UniqueOutputPath(unittest.TestCase):
    """Round 177 Review #2: step7_splits path should be process/run unique to avoid concurrent overwrite."""

    def test_r177_duckdb_uses_unique_step7_dir(self):
        """Production _duckdb_sort_and_split should use a unique subdir (pid/mkdtemp) under step7_splits."""
        source = _get_trainer_source()
        body = _find_duckdb_sort_and_split_body(source)
        self.assertIsNotNone(body)
        # Contract: step7_dir (or equivalent) should not be a single fixed path; should include pid or mkdtemp.
        uses_pid = "getpid" in body or "pid" in body
        uses_mkdtemp = "mkdtemp" in body or "tempfile" in body
        self.assertTrue(
            uses_pid or uses_mkdtemp,
            "step7 output dir should be process-unique (getpid or mkdtemp) to avoid concurrent overwrite",
        )


class TestR177DuckDBReadParquetNotPreparedList(unittest.TestCase):
    """Round 177 Review #3: DuckDB read_parquet(list) may fail with prepared statement in some builds."""

    def test_r177_duckdb_read_parquet_avoids_prepared_list(self):
        """Production should not use con.execute('... read_parquet(?)', [path_list]) (Binder Error in some envs)."""
        source = _get_trainer_source()
        body = _find_duckdb_sort_and_split_body(source)
        self.assertIsNotNone(body)
        # Problematic pattern: read_parquet(?) with list argument (path_list).
        has_read_parquet_placeholder = "read_parquet(?" in body or 'read_parquet(?)' in body
        has_path_list_arg = "path_list" in body and ("[path_list]" in body or ", [path_list]")
        self.assertFalse(
            has_read_parquet_placeholder and has_path_list_arg,
            "read_parquet with prepared list can cause Binder Error; use inline paths or non-prepared execution",
        )


class TestR177OomCheckDistinguishesDuckDB(unittest.TestCase):
    """Round 177 Review #4: OOM pre-check should use different formula when STEP7_USE_DUCKDB=True."""

    def test_r177_oom_estimate_duckdb_path_smaller_than_pandas(self):
        """Contract: when using DuckDB path, peak estimate (train split only) is smaller than full concat estimate."""
        on_disk = 1e9
        factor = 15.0
        train_frac = 0.7
        duck_est = _oom_estimate_duckdb_path(on_disk, factor, train_frac)
        pandas_est = _oom_estimate_pandas_path(on_disk, factor, train_frac)
        self.assertLess(duck_est, pandas_est)

    def test_r177_oom_check_body_references_step7_use_duckdb(self):
        """Production _oom_check_and_adjust_neg_sample_frac should branch on STEP7_USE_DUCKDB (PLAN Step 6)."""
        source = _get_trainer_source()
        body = _find_oom_check_body(source)
        self.assertIsNotNone(body)
        self.assertIn(
            "STEP7_USE_DUCKDB",
            body,
            "OOM pre-check should use STEP7_USE_DUCKDB to choose estimate formula (PLAN Step 6)",
        )


class TestR177R803UsesValueErrorNotAssert(unittest.TestCase):
    """Round 177 Review #5: R803 fraction check should be if/raise, not assert (-O safe)."""

    def test_r177_step7_main_block_r803_value_error_not_assert(self):
        """Step 7 main block should validate TRAIN/VALID fractions with ValueError, not assert."""
        source = _get_trainer_source()
        block = _find_step7_main_block(source)
        self.assertIsNotNone(block)
        for line in block.splitlines():
            if "assert" in line and ("TRAIN_SPLIT_FRAC" in line or "VALID_SPLIT_FRAC" in line):
                self.fail(
                    "R803 should use if/raise ValueError for fraction check, not assert (Round 177 Review #5)"
                )


class TestR177OrchestratorDocstringReadFallback(unittest.TestCase):
    """Round 177 Review #6: Docstring should state that read_parquet failure falls back to pandas with chunk_paths."""

    def test_r177_step7_sort_and_split_docstring_mentions_read_fallback(self):
        """_step7_sort_and_split docstring should mention fallback on read failure using chunk_paths."""
        source = _get_trainer_source()
        start = source.find("def _step7_sort_and_split(")
        if start == -1:
            self.fail("_step7_sort_and_split not found")
        doc_start = source.find('"""', start)
        doc_end = source.find('"""', doc_start + 3)
        docstring = source[doc_start : doc_end + 3]
        self.assertIn("fallback", docstring.lower())
        self.assertIn("chunk_paths", docstring)


if __name__ == "__main__":
    unittest.main()
