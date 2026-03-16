"""Round 246 Code Review — Canonical mapping Step 1（Config）風險點轉成測試。

STATUS.md Round 246 Code Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § Canonical mapping 全歷史 Step 1, STATUS Round 246 Review.
"""

from __future__ import annotations

import unittest

import trainer.config as config


class TestR246_1_CanonicalMapDuckDBMinLeMax(unittest.TestCase):
    """Review #1: MIN_GB <= MAX_GB so clamp semantics stay valid."""

    def test_canonical_map_duckdb_memory_limit_min_le_max(self):
        """config.CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MIN_GB must be <= MAX_GB."""
        min_gb = config.CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MIN_GB
        max_gb = config.CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB
        self.assertLessEqual(
            min_gb,
            max_gb,
            "CANONICAL_MAP_DUCKDB memory_limit MIN_GB must be <= MAX_GB (Review #1)",
        )


class TestR246_2_CanonicalMapDuckDBThreadsPositive(unittest.TestCase):
    """Review #2: THREADS >= 1 for DuckDB SET threads."""

    def test_canonical_map_duckdb_threads_at_least_one(self):
        """config.CANONICAL_MAP_DUCKDB_THREADS must be >= 1."""
        threads = config.CANONICAL_MAP_DUCKDB_THREADS
        self.assertGreaterEqual(
            threads,
            1,
            "CANONICAL_MAP_DUCKDB_THREADS must be >= 1 (Review #2)",
        )


class TestR246_3_CanonicalMapUseFullSessionsPandasDefault(unittest.TestCase):
    """Review #3: Default False so production does not accidentally load full sessions (OOM)."""

    def test_canonical_map_use_full_sessions_pandas_default_false(self):
        """CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS must default to False (debug-only flag)."""
        self.assertFalse(
            config.CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS,
            "CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS must be False by default (Review #3; debug only)",
        )


if __name__ == "__main__":
    unittest.main()
