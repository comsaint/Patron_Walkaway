"""Minimal reproducible tests for Round 174 Review — _duckdb_sort_and_split().

Tests-only: no production code changes.
Review risks (Round 174 Review in STATUS.md) are turned into contract/source
guards; tests that document current bugs use @unittest.expectedFailure.

_duckdb_sort_and_split is defined inside trainer.run_pipeline() and is not
directly callable. We test: (1) replicated effective temp-dir and fraction
contract, (2) source-code checks for ORDER BY NULLS LAST, empty chunk_paths,
fallback mkdir, COPY cleanup, docstring (xfail where production is wrong).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

# Replicate effective temp dir logic from _configure_step7_duckdb_runtime:
# when temp_dir_raw contains single quote, effective = DATA_DIR/duckdb_tmp.
def _effective_temp_dir_replica(temp_dir_raw: str | None, data_dir_duckdb_tmp: str) -> str:
    raw = temp_dir_raw if temp_dir_raw else data_dir_duckdb_tmp
    if "'" in raw:
        return data_dir_duckdb_tmp
    return raw


# Replicate split index calculation from _duckdb_sort_and_split (same formula).
def _split_indices_replica(n_rows: int, train_frac: float, valid_frac: float) -> tuple[int, int]:
    train_end_idx = int(n_rows * train_frac)
    valid_end_idx = int(n_rows * (train_frac + valid_frac))
    return train_end_idx, valid_end_idx


def _get_trainer_source() -> str:
    path = Path(__file__).resolve().parent.parent / "trainer" / "trainer.py"
    return path.read_text(encoding="utf-8")


def _find_duckdb_sort_and_split_body(source: str) -> str | None:
    """Return the body of _duckdb_sort_and_split (from def to next top-level def or end)."""
    start = source.find("def _duckdb_sort_and_split(")
    if start == -1:
        return None
    # Find next line that starts with "    def " (same indent as _duckdb_sort_and_split)
    rest = source[start:]
    end_match = re.search(r"\n    def [a-z_]+\(|\n    # [0-9]+\. ", rest)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


class TestR174EffectiveTempDirContract(unittest.TestCase):
    """P1 #1: Effective temp dir when config contains quote must be fallback (contract)."""

    def test_r174_effective_temp_dir_when_quote_uses_fallback(self):
        """When temp_dir_raw contains single quote, effective dir = DATA_DIR/duckdb_tmp."""
        import trainer.trainer as tr
        fallback = str(tr.DATA_DIR / "duckdb_tmp")
        effective = _effective_temp_dir_replica("/tmp/patron's", fallback)
        self.assertEqual(effective, fallback)

    def test_r174_effective_temp_dir_no_quote_uses_raw(self):
        """When no quote, effective = raw path."""
        import trainer.trainer as tr
        fallback = str(tr.DATA_DIR / "duckdb_tmp")
        raw = "/tmp/duckdb_tmp"
        self.assertEqual(_effective_temp_dir_replica(raw, fallback), raw)


class TestR174FallbackDirMustBeCreated(unittest.TestCase):
    """P1 #1: Production must mkdir the effective (fallback) dir when path has quote."""

    def test_r174_fallback_dir_created_when_quote_in_temp_dir(self):
        """Production should create fallback dir when STEP7_DUCKDB_TEMP_DIR contains quote.
        Either: else branch that mkdirs duckdb_tmp, or effective_temp_dir set to fallback when quote then mkdir(effective_temp_dir).
        """
        source = _get_trainer_source()
        body = _find_duckdb_sort_and_split_body(source)
        self.assertIsNotNone(body)
        # Contract: when quote in path, effective dir = fallback and that dir is created (mkdir).
        # Accept: effective_temp_dir set from temp_dir_raw/duckdb_tmp and then mkdir(effective_temp_dir).
        has_quote_fallback_logic = "duckdb_tmp" in body and "temp_dir_raw" in body
        has_effective_mkdir = "mkdir" in body and "effective_temp_dir" in body
        after_else = body[body.find("else:") : body.find("else:") + 400] if "else:" in body else ""
        has_else_mkdir_fallback = "else:" in body and "mkdir" in after_else and "duckdb_tmp" in after_else
        self.assertTrue(
            has_quote_fallback_logic and (has_effective_mkdir or has_else_mkdir_fallback),
            "Production should create fallback dir when temp_dir contains quote (effective_temp_dir + mkdir or else with mkdir duckdb_tmp)",
        )


class TestR174OrderByNullsLast(unittest.TestCase):
    """P1 #2: ORDER BY must specify NULLS LAST for parity with pandas na_position='last'."""

    def test_r174_order_by_should_use_nulls_last(self):
        """CREATE TEMP VIEW sorted_bets ... ORDER BY ... must include NULLS LAST."""
        source = _get_trainer_source()
        body = _find_duckdb_sort_and_split_body(source)
        self.assertIsNotNone(body)
        # ORDER BY payout_complete_dtm, canonical_id, bet_id -> should be NULLS LAST each.
        self.assertIn("ORDER BY", body)
        self.assertIn(
            "NULLS LAST",
            body,
            "ORDER BY in sorted_bets view should include NULLS LAST for pandas parity",
        )


class TestR174FractionValidationContract(unittest.TestCase):
    """P2 #3: train_frac/valid_frac contract — invalid fractions yield invalid indices."""

    def test_r174_invalid_fractions_yield_valid_end_ge_n_rows(self):
        """When train_frac+valid_frac >= 1, replicated logic yields valid_end_idx >= n_rows."""
        n_rows = 1000
        train_end, valid_end = _split_indices_replica(n_rows, 0.9, 0.9)
        self.assertGreaterEqual(valid_end, n_rows, "valid_end_idx >= n_rows -> test split empty")

    def test_r174_valid_fractions_yield_sensible_indices(self):
        """When 0.7, 0.15: train_end=700, valid_end=850, test non-empty."""
        n_rows = 1000
        train_end, valid_end = _split_indices_replica(n_rows, 0.7, 0.15)
        self.assertEqual(train_end, 700)
        self.assertEqual(valid_end, 850)
        self.assertLess(valid_end, n_rows)


class TestR174EmptyChunkPaths(unittest.TestCase):
    """P2 #4: Production should raise when chunk_paths is empty."""

    def test_r174_empty_chunk_paths_should_be_checked(self):
        """_duckdb_sort_and_split should check chunk_paths and raise if empty."""
        source = _get_trainer_source()
        body = _find_duckdb_sort_and_split_body(source)
        self.assertIsNotNone(body)
        # Require explicit early check for empty chunk_paths (not just any use of chunk_paths + raise).
        has_early_empty_check = (
            "if not chunk_paths" in body or "if len(chunk_paths) == 0" in body
        ) and "raise" in body
        self.assertTrue(
            has_early_empty_check,
            "Production should raise when chunk_paths is empty (if not chunk_paths: raise ...)",
        )


class TestR174CopyFailureCleanup(unittest.TestCase):
    """P2 #5: On COPY failure, partial split files should be removed."""

    def test_r174_copy_failure_should_remove_partial_files(self):
        """On exception, production should delete any already-written split files."""
        source = _get_trainer_source()
        body = _find_duckdb_sort_and_split_body(source)
        self.assertIsNotNone(body)
        # Expect in except/finally: unlink train_path/valid_path/test_path if exist.
        has_cleanup = (
            ("except" in body or "finally" in body)
            and ("unlink" in body or "remove" in body or "exists" in body)
            and ("train_path" in body or "valid_path" in body or "split_train" in body)
        )
        self.assertTrue(
            has_cleanup,
            "On COPY failure production should remove partial split files",
        )


class TestR174ReadParquetListContract(unittest.TestCase):
    """P3 #6: read_parquet(?) with list — document contract (version-dependent)."""

    def test_r174_path_list_is_list_of_str(self):
        """Production builds path_list = [str(p) for p in chunk_paths]; uses read_parquet with list (inline SQL, not prepared)."""
        source = _get_trainer_source()
        body = _find_duckdb_sort_and_split_body(source)
        self.assertIsNotNone(body)
        self.assertIn("path_list = [str(p) for p in chunk_paths]", body)
        # Contract: read_parquet receives list of paths (inline [paths_sql] or similar), not prepared (?) with [path_list].
        self.assertIn("read_parquet([", body)


class TestR174DocstringTempDir(unittest.TestCase):
    """P3 #7: Docstring should state that function creates temp dir, not 'Caller must create'."""

    def test_r174_docstring_says_function_creates_temp_dir(self):
        """Docstring should not say 'Caller must create temp dir' (function does mkdir)."""
        source = _get_trainer_source()
        body = _find_duckdb_sort_and_split_body(source)
        self.assertIsNotNone(body)
        # Extract docstring: first """ ... """
        start = body.find('"""')
        self.assertGreaterEqual(start, 0)
        end = body.find('"""', start + 3)
        self.assertGreater(end, start)
        doc = body[start : end + 3]
        self.assertNotIn(
            "Caller must create temp dir",
            doc,
            "Docstring should state function creates temp dir, not caller",
        )


if __name__ == "__main__":
    unittest.main()
