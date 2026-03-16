"""Minimal reproducible tests for Round 202 Review — 方案 B+ 階段 1–2 審查風險點.

Round 202 Review (STATUS.md) risk points are turned into contract/behavior/source tests.
Helpers _read_parquet_head and _step7_metadata_from_paths are nested inside run_pipeline,
so we test contracts (PyArrow/DuckDB behavior) and run_pipeline source structure.
Tests-only: no production code changes.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import trainer.trainer as trainer_mod


def _get_run_pipeline_source() -> str:
    return inspect.getsource(trainer_mod.run_pipeline)


# ---------------------------------------------------------------------------
# R202 Review #1 — 0-row train Parquet 契約
# ---------------------------------------------------------------------------

class TestR202ContractZeroRowParquet(unittest.TestCase):
    """Round 202 #1: Contract for 0-row train (B+ path)."""

    def test_pyarrow_read_zero_row_parquet_returns_empty_dataframe(self):
        """0-row Parquet read via PyArrow (same pattern as _read_parquet_head) returns empty DataFrame."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        table = pa.Table.from_pandas(pd.DataFrame({"a": [], "label": []}))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "zero.parquet"
            pq.write_table(table, path)
            # Use read_table so file is closed (avoid Windows PermissionError on cleanup)
            tbl = pq.read_table(path)
            df = tbl.to_pandas()
            self.assertTrue(df.empty, "0-row parquet should produce empty DataFrame")

    def test_duckdb_count_zero_row_parquet_returns_zero(self):
        """DuckDB SELECT count(*) FROM read_parquet(0-row path) returns 0 (metadata contract)."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        import duckdb
        table = pa.Table.from_pandas(pd.DataFrame({"label": [], "payout_complete_dtm": []}))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "train.parquet"
            pq.write_table(table, path)
            con = duckdb.connect(":memory:")
            r = con.execute(f"SELECT count(*) FROM read_parquet('{str(path).replace(chr(39), chr(39)+chr(39))}')").fetchone()
            con.close()
            self.assertIsNotNone(r)
            self.assertEqual(int(r[0]), 0, "0-row parquet count must be 0")


# ---------------------------------------------------------------------------
# R202 Review #2 — metadata 路徑缺 label 時應有明確錯誤
# ---------------------------------------------------------------------------

class TestR202ContractMetadataMissingLabelRaises(unittest.TestCase):
    """Round 202 #2: Parquet without 'label' used in metadata query must raise with 'label' in message."""

    def test_duckdb_label_sum_on_parquet_without_label_raises_with_label_in_message(self):
        """DuckDB SELECT sum(cast(label AS INTEGER)) on parquet without 'label' raises; message contains 'label'."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        import duckdb
        table = pa.table({"x": [1.0]})  # no label
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "no_label.parquet"
            pq.write_table(table, path)
            con = duckdb.connect(":memory:")
            s = str(path).replace("'", "''")
            with self.assertRaises(Exception) as ctx:
                con.execute(f"SELECT coalesce(sum(cast(label AS INTEGER)), 0) FROM read_parquet('{s}')").fetchone()
            con.close()
            self.assertIn("label", str(ctx.exception).lower(), "Error for missing 'label' must mention 'label'")


# ---------------------------------------------------------------------------
# R202 Review #3 — B+ 路徑應 guard valid/test path 非 None（source）
# ---------------------------------------------------------------------------

class TestR202SourceGuardValidTestPathWhenTrainPathSet(unittest.TestCase):
    """Round 202 #3: When step7_train_path is not None, run_pipeline must guard step7_valid_path / step7_test_path."""

    def test_run_pipeline_bplus_branch_guards_valid_test_path_not_none(self):
        """run_pipeline block 'if step7_train_path is not None' must check valid/test path and raise ValueError."""
        src = _get_run_pipeline_source()
        self.assertIn("step7_train_path is not None", src)
        # Guard: when train path set, valid/test must be checked before _step7_metadata_from_paths
        self.assertIn("step7_valid_path", src)
        self.assertIn("step7_test_path", src)
        # Must raise when either is None (production does not have this guard yet)
        block_start = src.find("if step7_train_path is not None:")
        self.assertGreater(block_start, -1)
        block = src[block_start : block_start + 1200]
        self.assertIn("None", block)
        self.assertTrue(
            ("step7_valid_path is None" in block or "step7_test_path is None" in block) and "raise" in block,
            "B+ branch must raise when step7_valid_path or step7_test_path is None",
        )


# ---------------------------------------------------------------------------
# R202 Review #4 — _read_parquet_head 對不存在 path 的契約
# ---------------------------------------------------------------------------

class TestR202ContractReadParquetHeadNonexistentPathRaises(unittest.TestCase):
    """Round 202 #4: Reading nonexistent path (ParquetFile) must not return empty DataFrame silently."""

    def test_pyarrow_parquet_file_nonexistent_path_raises(self):
        """PyArrow ParquetFile(nonexistent path) raises; does not return empty DataFrame."""
        import pyarrow.parquet as pq
        path = Path("/nonexistent") / "train.parquet"
        if path.exists():
            self.skipTest("Path unexpectedly exists")
        with self.assertRaises((OSError, FileNotFoundError)):
            pq.ParquetFile(path)


# ---------------------------------------------------------------------------
# R202 Review #6 — 路徑含反斜線時 DuckDB 可讀（契約）
# ---------------------------------------------------------------------------

class TestR202ContractDuckDBReadParquetWithBackslashPath(unittest.TestCase):
    """Round 202 #6: DuckDB read_parquet with path containing backslash (e.g. Windows) should work."""

    def test_duckdb_read_parquet_with_path_containing_backslash_succeeds(self):
        """On Windows, path has backslash; DuckDB read_parquet must succeed (contract for B+ metadata)."""
        import pyarrow as pa
        import pyarrow.parquet as pq
        import duckdb
        table = pa.table({
            "label": [0, 1],
            "payout_complete_dtm": [pd.Timestamp("2025-01-01"), pd.Timestamp("2025-01-02")],
        })
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "train.parquet"
            pq.write_table(table, path)
            # Path on Windows will have backslash
            path_str = str(path)
            con = duckdb.connect(":memory:")
            try:
                r = con.execute(
                    f"SELECT count(*), coalesce(sum(cast(label AS INTEGER)), 0), max(payout_complete_dtm) FROM read_parquet('{path_str.replace(chr(39), chr(39)+chr(39))}')"
                ).fetchone()
            finally:
                con.close()
            self.assertIsNotNone(r)
            self.assertEqual(int(r[0]), 2)
            self.assertEqual(int(r[1]), 1)


if __name__ == "__main__":
    unittest.main()
