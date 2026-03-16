"""Minimal reproducible tests for Code Review: Step 8 DuckDB 算 std（Phase 1 實作）.

Review risks (STATUS.md « Code Review：Step 8 DuckDB 算 std ») are turned into
contract/behavior tests. Tests-only: no production code changes.
"""

from __future__ import annotations

import decimal
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import trainer.features as features_mod
import trainer.trainer as trainer_mod


# ---------------------------------------------------------------------------
# §1 — [安全性／防禦] Path 以字串拼接進 SQL：path 含單引號時不拋錯且可解析
# ---------------------------------------------------------------------------

class TestStep8DuckDbStdPathWithSingleQuote(unittest.TestCase):
    """Code Review §1: Path containing single quote must not break SQL; result parseable."""

    def test_compute_column_std_duckdb_path_with_single_quote_in_filename(self):
        """Pass path with single quote in filename; assert no exception and result has correct length."""
        cols = ["x", "y"]
        df = pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0]})
        with tempfile.TemporaryDirectory() as tmp:
            # Filename containing single quote (allowed on Windows; only \ / : * ? " < > | are forbidden)
            path = Path(tmp) / "file'_x.parquet"
            df.to_parquet(path, index=False)
            result = features_mod.compute_column_std_duckdb(cols, path=path)
        self.assertEqual(len(result), 2, "Result must have one value per column")
        self.assertIn("x", result.index)
        self.assertIn("y", result.index)
        np.testing.assert_allclose(result.values, [0.5, 0.5], rtol=1e-9)


# ---------------------------------------------------------------------------
# §2 — [Bug／邊界] Parquet 缺欄時 fallback：不拋錯、日誌有 fallback、回傳合理 list
# ---------------------------------------------------------------------------

class TestStep8DuckDbStdParquetMissingColumnsFallback(unittest.TestCase):
    """Code Review §2: When Parquet misses requested columns, no crash; fallback and log."""

    def test_screen_features_train_path_parquet_missing_columns_does_not_raise(self):
        """Parquet has only subset of feature_names; screen_features must not raise and return a list (fallback to sample std)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "train.parquet"
            # Only column "a"; feature_names will include "a", "b", "c" -> DuckDB will fail on b,c
            pd.DataFrame({"a": [1.0, 2.0], "label": [0, 1]}).to_parquet(path, index=False)
            sample = pd.DataFrame({
                "a": [1.0, 2.0],
                "b": [1.0, 1.0],
                "c": [0.0, 0.0],
                "label": [0, 1],
            })
            result = features_mod.screen_features(
                feature_matrix=sample,
                labels=sample["label"],
                feature_names=["a", "b", "c"],
                screen_method="lgbm",
                train_path=path,
            )
            self.assertIsInstance(result, list, "Must not raise; must return list (fallback when Parquet misses columns)")
            # When fallback runs, screening still completes (result may be subset of features)
            self.assertLessEqual(len(result), 3)


# ---------------------------------------------------------------------------
# §3 — [邊界／語意] 同時傳入 train_path 與 train_df：目前行為為 path 優先、不拋錯
# ---------------------------------------------------------------------------

class TestStep8DuckDbStdBothPathAndDfContract(unittest.TestCase):
    """Code Review §3: When both train_path and train_df are set, current behavior: path wins, no ValueError."""

    def test_screen_features_both_train_path_and_train_df_does_not_raise(self):
        """Current contract: passing both path and df does not raise; returns list (path branch used)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "train.parquet"
            df = pd.DataFrame({"f": [1.0, 2.0], "label": [0, 1]})
            df.to_parquet(path, index=False)
            sample = df.copy()
            result = features_mod.screen_features(
                feature_matrix=sample,
                labels=sample["label"],
                feature_names=["f"],
                screen_method="lgbm",
                train_path=path,
                train_df=df,
            )
            self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# §4 — [數值／Parity] compute_column_std_duckdb 與 pandas std(ddof=0) 數值接近
# ---------------------------------------------------------------------------

class TestStep8DuckDbStdVsPandasDdof0(unittest.TestCase):
    """Code Review §4: DuckDB stddev_pop matches pandas std(ddof=0)."""

    def test_compute_column_std_duckdb_matches_pandas_std_ddof0(self):
        """Small DataFrame: compute_column_std_duckdb(..., df=df) vs df[cols].std(ddof=0)."""
        n = 100
        np.random.seed(42)
        df = pd.DataFrame({
            "a": np.random.randn(n),
            "b": np.random.randn(n) * 2,
            "c": np.random.randn(n) + 1,
        })
        cols = ["a", "b", "c"]
        duck = features_mod.compute_column_std_duckdb(cols, df=df)
        pan = df[cols].std(ddof=0)
        np.testing.assert_allclose(duck.values, pan.values, rtol=1e-9, err_msg="DuckDB stddev_pop vs pandas std(ddof=0)")


# ---------------------------------------------------------------------------
# §5 — [效能／資源] 較大 DataFrame 不 OOM、回傳長度與數值合理（可選，標記 skip 或小規模）
# ---------------------------------------------------------------------------

class TestStep8DuckDbStdLargeDfContract(unittest.TestCase):
    """Code Review §5: Optional large-df contract: no crash, return shape correct."""

    @unittest.skip("Optional: run manually or in CI with sufficient RAM (500k rows)")
    def test_compute_column_std_duckdb_large_df_no_oom(self):
        """With 500k rows × 10 cols, compute_column_std_duckdb completes and returns correct length."""
        n = 500_000
        cols = [f"f{i}" for i in range(10)]
        df = pd.DataFrame({c: np.random.randn(n) for c in cols})
        result = features_mod.compute_column_std_duckdb(cols, df=df)
        self.assertEqual(len(result), 10)
        self.assertTrue(np.all(np.isfinite(result.values) | (result.values == 0)))

    def test_compute_column_std_duckdb_medium_df_returns_correct_shape(self):
        """Smaller size: 10k rows, assert return length and finite or zero."""
        n = 10_000
        cols = ["a", "b", "c"]
        df = pd.DataFrame({c: np.random.randn(n) for c in cols})
        result = features_mod.compute_column_std_duckdb(cols, df=df)
        self.assertEqual(len(result), 3)
        self.assertTrue(np.all(np.isfinite(result.values) | (result.values == 0)))


# ---------------------------------------------------------------------------
# §6 — [邊界] 空 Parquet / 0 列 DataFrame：回傳長度正確、全 0 或 NaN 後 fillna 為 0
# ---------------------------------------------------------------------------

class TestStep8DuckDbStdEmptyParquetAndDf(unittest.TestCase):
    """Code Review §6: Empty Parquet or 0-row DataFrame returns correct length, zeros after fillna."""

    def test_compute_column_std_duckdb_empty_parquet_returns_zeros(self):
        """0-row Parquet: result length = columns, values 0 (after fillna in implementation)."""
        cols = ["a", "b"]
        empty = pd.DataFrame(columns=cols)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.parquet"
            empty.to_parquet(path, index=False)
            result = features_mod.compute_column_std_duckdb(cols, path=path)
        self.assertEqual(len(result), 2)
        self.assertEqual(list(result.index), cols)
        np.testing.assert_array_almost_equal(result.values, [0.0, 0.0])

    def test_compute_column_std_duckdb_empty_dataframe_returns_zeros(self):
        """0-row DataFrame: same contract."""
        cols = ["a", "b"]
        empty = pd.DataFrame(columns=cols)
        result = features_mod.compute_column_std_duckdb(cols, df=empty)
        self.assertEqual(len(result), 2)
        np.testing.assert_array_almost_equal(result.values, [0.0, 0.0])


# ---------------------------------------------------------------------------
# §7 — [可維護性] 含字串欄時不拋錯、回傳三欄、字串欄為 0 或 NaN
# ---------------------------------------------------------------------------

class TestStep8DuckDbStdStringColumnTolerated(unittest.TestCase):
    """Code Review §7: DataFrame with one string + two numeric; no exception, 3 cols, string col 0."""

    def test_compute_column_std_duckdb_with_string_column_does_not_raise(self):
        """One string + two numeric columns; no exception; 3 entries; string col 0 (skipped), numeric cols > 0."""
        df = pd.DataFrame({
            "s": ["x", "y", "z"],
            "a": [1.0, 2.0, 3.0],
            "b": [4.0, 5.0, 6.0],
        })
        result = features_mod.compute_column_std_duckdb(["s", "a", "b"], df=df)
        self.assertEqual(len(result), 3)
        self.assertIn("s", result.index)
        self.assertIn("a", result.index)
        self.assertIn("b", result.index)
        self.assertTrue(
            result["s"] == 0.0 or pd.isna(result["s"]),
            "String column should be 0 or NaN (stddev_pop on non-numeric)",
        )
        self.assertGreater(result["a"], 0)
        self.assertGreater(result["b"], 0)


# ---------------------------------------------------------------------------
# §8 — [Trainer] Step 8 使用 _cap / 2_000_000 且傳入 train_path / train_df 之契約（source 檢查）
# ---------------------------------------------------------------------------

class TestStep8TrainerCapAndPassThroughContract(unittest.TestCase):
    """Code Review §8: run_pipeline Step 8 must use cap (2_000_000 or config) and pass train_path/train_df."""

    def test_step8_block_uses_cap_and_passes_train_path_or_train_df(self):
        """Step 8 block must define _cap (or equivalent), use head(_cap)/head(_sample_n), and pass train_path/train_df to screen_features."""
        src = inspect.getsource(trainer_mod.run_pipeline)
        step8_marker = "Step 8 DuckDB std (PLAN)"
        self.assertIn(step8_marker, src, "Step 8 DuckDB cap/pass block must exist")
        # Must have cap (2_000_000 or STEP8_SCREEN_SAMPLE_ROWS)
        self.assertTrue(
            "2_000_000" in src or "STEP8_SCREEN_SAMPLE_ROWS" in src,
            "Step 8 must use 2_000_000 or STEP8_SCREEN_SAMPLE_ROWS for cap",
        )
        self.assertIn("train_path=", src, "screen_features must be called with train_path=...")
        self.assertIn("train_df=", src, "screen_features must be called with train_df=...")

    def test_step8_cap_equals_default_when_config_none(self):
        """When STEP8_SCREEN_SAMPLE_ROWS is None, effective cap should be 2_000_000 (source contract)."""
        import inspect
        src = inspect.getsource(trainer_mod.run_pipeline)
        # Logic: _cap = int(STEP8...) if set else 2_000_000
        self.assertIn("2_000_000", src)
        idx = src.find("_cap = ")
        self.assertGreater(idx, -1)
        snippet = src[idx : idx + 400]
        self.assertTrue(
            "2_000_000" in snippet or ("_cap" in snippet and "STEP8" in snippet),
            "Default cap 2_000_000 or _cap from STEP8 must appear in Step 8 block",
        )


# ---------------------------------------------------------------------------
# Code Review 2026-03-13（STATUS.md « Code Review：Step 8 DuckDB std 變更 »）風險點 → 最小可重現測試
# 僅新增 tests，不改 production。
# ---------------------------------------------------------------------------

class TestReviewPathSqlSensitiveChars(unittest.TestCase):
    """Review §1: Path containing SQL-sensitive chars (e.g. ; --) must not break execution; result correct."""

    def test_compute_column_std_duckdb_path_with_semicolon_in_filename(self):
        """Path with semicolon in filename: no exception, result length and values correct (regression for parameterized path)."""
        cols = ["x", "y"]
        df = pd.DataFrame({"x": [1.0, 2.0], "y": [3.0, 4.0]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file;x.parquet"
            df.to_parquet(path, index=False)
            result = features_mod.compute_column_std_duckdb(cols, path=path)
        self.assertEqual(len(result), 2)
        self.assertIn("x", result.index)
        self.assertIn("y", result.index)
        np.testing.assert_allclose(result.values, [0.5, 0.5], rtol=1e-9)


class TestReviewDuplicateColumnNames(unittest.TestCase):
    """Review §2: columns with duplicate names: no crash; return length and values consistent (or document current behavior)."""

    def test_compute_column_std_duckdb_duplicate_columns_no_crash_and_consistent_values(self):
        """Duplicate column names ['a','a','b']: must not raise; len(result)==3; values for a and b match single-call semantics."""
        df = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]})
        result = features_mod.compute_column_std_duckdb(["a", "a", "b"], df=df)
        self.assertEqual(len(result), 3, "Result must have one entry per requested column")
        self.assertEqual(list(result.index), ["a", "a", "b"])
        single = features_mod.compute_column_std_duckdb(["a", "b"], df=df)
        # result.iloc[0], result.iloc[1] = two 'a' values; result.iloc[2] = 'b'
        self.assertAlmostEqual(float(result.iloc[2]), float(single["b"]), places=9)
        self.assertAlmostEqual(float(result.iloc[0]), float(single["a"]), places=9)
        self.assertAlmostEqual(float(result.iloc[1]), float(single["a"]), places=9, msg="Both 'a' entries equal")


class TestReviewHelperMissingColumnsParquet(unittest.TestCase):
    """Review §4: compute_column_std_duckdb when Parquet has only subset of requested columns; missing get 0.0."""

    def test_compute_column_std_duckdb_parquet_missing_columns_returns_zeros_for_missing(self):
        """Parquet has only 'a'; request ['a','b','c']. len==3, result['a']>0, result['b']==0, result['c']==0."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "only_a.parquet"
            pd.DataFrame({"a": [1.0, 2.0, 3.0]}).to_parquet(path, index=False)
            result = features_mod.compute_column_std_duckdb(["a", "b", "c"], path=path)
        self.assertEqual(len(result), 3)
        self.assertIn("a", result.index)
        self.assertIn("b", result.index)
        self.assertIn("c", result.index)
        self.assertGreater(result["a"], 0, "Column present in Parquet must have positive std")
        self.assertEqual(result["b"], 0.0, "Missing column must be 0.0")
        self.assertEqual(result["c"], 0.0, "Missing column must be 0.0")


class TestReviewAllNonNumericColumns(unittest.TestCase):
    """Review §5: When all requested columns are non-numeric, return all 0.0."""

    def test_compute_column_std_duckdb_all_string_columns_returns_zeros(self):
        """Request ['s1','s2'] with df only string columns; len==2, both 0.0."""
        df = pd.DataFrame({"s1": ["x", "y"], "s2": ["a", "b"]})
        result = features_mod.compute_column_std_duckdb(["s1", "s2"], df=df)
        self.assertEqual(len(result), 2)
        self.assertEqual(result["s1"], 0.0)
        self.assertEqual(result["s2"], 0.0)


# ---------------------------------------------------------------------------
# Step 8 Phase 2（可選）— DuckDB 相關矩陣與 pandas 數值一致（PLAN 實作順序建議 2）
# ---------------------------------------------------------------------------

class TestStep8Phase2DuckDbCorrVsPandas(unittest.TestCase):
    """PLAN Phase 2: compute_correlation_matrix_duckdb matches pandas .corr().abs() for small df."""

    def test_compute_correlation_matrix_duckdb_matches_pandas_corr_abs(self):
        """Small DataFrame: DuckDB corr matrix (abs) vs df[cols].corr().abs()."""
        n = 50
        np.random.seed(42)
        df = pd.DataFrame({
            "a": np.random.randn(n),
            "b": np.random.randn(n) * 2,
            "c": np.random.randn(n) + 1,
        })
        cols = ["a", "b", "c"]
        duck = features_mod.compute_correlation_matrix_duckdb(cols, df=df)
        pan = df[cols].corr().abs()
        self.assertEqual(list(duck.index), cols)
        self.assertEqual(list(duck.columns), cols)
        np.testing.assert_allclose(
            duck.values,
            pan.values,
            rtol=1e-5,
            err_msg="DuckDB correlation matrix (abs) vs pandas .corr().abs()",
        )


# ---------------------------------------------------------------------------
# Step 8 Phase 2 — Code Review 風險點 → 最小可重現測試（STATUS.md Review 2026-03-13）
# 僅新增測試，不修改 production code。
# ---------------------------------------------------------------------------

class TestStep8Phase2Review1RowLengthMismatch(unittest.TestCase):
    """Review §1: When DuckDB fetchone() returns too few values, must not raise IndexError."""

    def test_corr_duckdb_row_length_mismatch_does_not_raise_index_error(self):
        """Mock fetchone to return 2 elements for K=3 (need 6). Contract: no IndexError."""
        import duckdb
        mock_con = MagicMock()
        mock_con.fetchone.return_value = (1.0, 0.5)  # too short for K=3
        mock_con.execute = MagicMock()
        mock_con.close = MagicMock()
        with patch.object(duckdb, "connect", return_value=mock_con):
            df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6], "c": [7, 8, 9]})
            try:
                result = features_mod.compute_correlation_matrix_duckdb(
                    ["a", "b", "c"], df=df
                )
                self.assertIsInstance(result, pd.DataFrame)
                self.assertEqual(result.shape, (3, 3))
            except IndexError:
                self.fail("Review §1: row length not validated; IndexError raised")


class TestStep8Phase2Review2EmptyTable(unittest.TestCase):
    """Review §2: Empty table (0 rows, 2+ numeric cols): diagonal 1.0, off-diagonal 0.0."""

    def test_compute_correlation_matrix_duckdb_empty_table(self):
        """Two numeric columns, 0 rows; assert 2×2, diagonal 1, off-diag 0. Use float dtype so cols are numeric."""
        empty = pd.DataFrame({"a": [], "b": []})
        self.assertTrue(pd.api.types.is_numeric_dtype(empty["a"]))
        result = features_mod.compute_correlation_matrix_duckdb(
            ["a", "b"], df=empty
        )
        self.assertEqual(list(result.index), ["a", "b"])
        self.assertEqual(list(result.columns), ["a", "b"])
        self.assertEqual(result.loc["a", "a"], 1.0)
        self.assertEqual(result.loc["b", "b"], 1.0)
        self.assertEqual(result.loc["a", "b"], 0.0)
        self.assertEqual(result.loc["b", "a"], 0.0)


class TestStep8Phase2Review3PathSingleQuote(unittest.TestCase):
    """Review §3: Path with single quote in filename must not break SQL; result parseable."""

    def test_compute_correlation_matrix_duckdb_path_with_single_quote_in_filename(self):
        """Write parquet to file'_x.parquet; path call must not raise and match df call."""
        cols = ["x", "y"]
        df = pd.DataFrame({"x": [1.0, 2.0, 3.0], "y": [2.0, 3.0, 4.0]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file'_x.parquet"
            df.to_parquet(path, index=False)
            by_path = features_mod.compute_correlation_matrix_duckdb(cols, path=path)
            by_df = features_mod.compute_correlation_matrix_duckdb(cols, df=df)
        self.assertEqual(list(by_path.index), cols)
        self.assertEqual(list(by_path.columns), cols)
        np.testing.assert_allclose(
            by_path.values, by_df.values, rtol=1e-9,
            err_msg="path vs df result must match when path has single quote",
        )


class TestStep8Phase2Review4ParquetMissingColumns(unittest.TestCase):
    """Review §4: Parquet has subset of requested columns; missing get 0 in output."""

    def test_compute_correlation_matrix_duckdb_parquet_missing_columns_zeros_for_missing(self):
        """Request ['a','b','c']; parquet has only ['a','b']. Output 3×3, c row/col all 0."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ab_only.parquet"
            pd.DataFrame({"a": [1.0, 2.0], "b": [3.0, 4.0]}).to_parquet(
                path, index=False
            )
            result = features_mod.compute_correlation_matrix_duckdb(
                ["a", "b", "c"], path=path
            )
        self.assertEqual(list(result.index), ["a", "b", "c"])
        self.assertEqual(list(result.columns), ["a", "b", "c"])
        np.testing.assert_array_equal(result.loc["c", :].values, [0.0, 0.0, 0.0])
        np.testing.assert_array_equal(result.loc[:, "c"].values, [0.0, 0.0, 0.0])


class TestStep8Phase2Review5LargeK(unittest.TestCase):
    """Review §5: Moderate K (e.g. 100) with small df still completes; regression guard."""

    def test_compute_correlation_matrix_duckdb_k100_small_df_returns_shape(self):
        """K=100 cols, 20 rows; assert shape (100,100), diagonal 1.0."""
        n_rows, k = 20, 100
        np.random.seed(42)
        df = pd.DataFrame({
            f"c{i}": np.random.randn(n_rows) for i in range(k)
        })
        cols = list(df.columns)
        result = features_mod.compute_correlation_matrix_duckdb(cols, df=df)
        self.assertEqual(result.shape, (k, k))
        np.testing.assert_array_almost_equal(
            np.diag(result.values), np.ones(k),
            err_msg="Diagonal must be 1.0",
        )


class TestStep8Phase2Review6DecimalInRow(unittest.TestCase):
    """Review §6: If DuckDB returns decimal.Decimal in row, conversion must not raise."""

    def test_corr_duckdb_fetchone_decimal_converts_to_float(self):
        """Mock fetchone with one Decimal; contract: no TypeError, value as float in matrix."""
        import duckdb
        # K=2 -> 3 values: (0,0),(0,1),(1,1). Put Decimal at off-diagonal.
        mock_con = MagicMock()
        mock_con.fetchone.return_value = (
            1.0,
            decimal.Decimal("0.5"),
            1.0,
        )
        mock_con.execute = MagicMock()
        mock_con.close = MagicMock()
        with patch.object(duckdb, "connect", return_value=mock_con):
            df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
            result = features_mod.compute_correlation_matrix_duckdb(
                ["a", "b"], df=df
            )
        self.assertIsInstance(result, pd.DataFrame)
        self.assertEqual(result.shape, (2, 2))
        self.assertEqual(result.loc["a", "a"], 1.0)
        self.assertEqual(result.loc["b", "b"], 1.0)
        self.assertAlmostEqual(float(result.loc["a", "b"]), 0.5, places=9)
        self.assertAlmostEqual(float(result.loc["b", "a"]), 0.5, places=9)


class TestStep8Phase2Review7PathAndDfMatchPandas(unittest.TestCase):
    """Review §7: path path and df path both match each other and pandas .corr().abs()."""

    def test_compute_correlation_matrix_duckdb_path_and_df_match_pandas(self):
        """Same small df: to_parquet → path call; df call; pandas .corr().abs(); all pairwise close."""
        n = 40
        np.random.seed(123)
        df = pd.DataFrame({
            "a": np.random.randn(n),
            "b": np.random.randn(n) * 2,
            "c": np.random.randn(n) + 1,
        })
        cols = ["a", "b", "c"]
        pan = df[cols].corr().abs()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "train.parquet"
            df.to_parquet(path, index=False)
            by_path = features_mod.compute_correlation_matrix_duckdb(cols, path=path)
        by_df = features_mod.compute_correlation_matrix_duckdb(cols, df=df)
        np.testing.assert_allclose(
            by_path.values, by_df.values, rtol=1e-9,
            err_msg="path vs df",
        )
        np.testing.assert_allclose(
            by_path.values, pan.values, rtol=1e-5,
            err_msg="path vs pandas .corr().abs()",
        )
        np.testing.assert_allclose(
            by_df.values, pan.values, rtol=1e-5,
            err_msg="df vs pandas .corr().abs()",
        )

