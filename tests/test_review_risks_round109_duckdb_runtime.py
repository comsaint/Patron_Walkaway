"""Minimal reproducible guards for Round 109 DuckDB runtime risks.

Scope:
- Convert reviewer findings (Round 109) into executable tests.
- Tests only; no production code edits.
- Unresolved risks are marked expectedFailure so they remain visible in CI.
"""

from __future__ import annotations

import inspect
import re
import unittest

import trainer.config as cfg
import trainer.etl_player_profile as etl_mod


class _FakeDuckDBCon:
    """Tiny fake connection object for SET-statement behavior tests."""

    def __init__(self, fail_on_threads: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_on_threads = fail_on_threads

    def execute(self, sql: str) -> None:
        self.calls.append(sql)
        if self.fail_on_threads and "SET threads=" in sql:
            raise RuntimeError("simulated threads failure")


class TestR109DuckDBRuntimeRiskGuards(unittest.TestCase):
    """Guardrails for Round 109 review findings."""

    def test_r109_0_config_should_expose_duckdb_runtime_knobs(self):
        """Sanity: config.py should expose profile DuckDB runtime settings."""
        required = [
            "PROFILE_DUCKDB_RAM_FRACTION",
            "PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB",
            "PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB",
            "PROFILE_DUCKDB_THREADS",
            "PROFILE_DUCKDB_PRESERVE_INSERTION_ORDER",
        ]
        for name in required:
            self.assertTrue(hasattr(cfg, name), f"Missing config knob: {name}")

    def test_r109_1_fraction_should_be_range_validated(self):
        """Risk #1: RAM fraction should be validated to (0, 1] with warning fallback."""
        src = inspect.getsource(etl_mod._compute_duckdb_memory_limit_bytes)
        self.assertTrue(
            re.search(r"0\.0\s*<\s*.*PROFILE_DUCKDB_RAM_FRACTION.*<=\s*1\.0", src)
            or re.search(r"if\s+not\s*\(\s*0\.0\s*<\s*frac\s*<=\s*1\.0\s*\)", src),
            "Expected explicit range validation for PROFILE_DUCKDB_RAM_FRACTION.",
        )
        self.assertIn(
            "logger.warning",
            src,
            "Expected warning log when PROFILE_DUCKDB_RAM_FRACTION is invalid.",
        )

    def test_r109_2_min_max_should_be_normalized(self):
        """Risk #1: MIN_GB > MAX_GB should be detected and normalized."""
        src = inspect.getsource(etl_mod._compute_duckdb_memory_limit_bytes)
        self.assertRegex(
            src,
            r"if\s+_min\s*>\s*_max",
            "Expected guard for PROFILE_DUCKDB_MEMORY_LIMIT_MIN_GB > MAX_GB.",
        )

    def test_r109_3_get_available_ram_should_handle_psutil_runtime_errors(self):
        """Risk #3: psutil runtime failures (not just ImportError) should not escape."""
        src = inspect.getsource(etl_mod._get_available_ram_bytes)
        self.assertTrue(
            "except Exception" in src or "except (ImportError," in src,
            "Expected broad exception handling for psutil.virtual_memory() failures.",
        )

    def test_r109_4_runtime_set_failure_should_not_skip_later_settings(self):
        """Risk #4: each SET should fail independently (not one big try/except)."""
        fake = _FakeDuckDBCon(fail_on_threads=True)
        etl_mod._configure_duckdb_runtime(fake, budget_bytes=1 * 1024**3)
        self.assertIn(
            "SET preserve_insertion_order=false",
            fake.calls,
            "Expected preserve_insertion_order SET even if SET threads fails.",
        )

    def test_r109_5_oom_detection_should_prefer_exception_type(self):
        """Risk #6: OOM branch should prefer duckdb.OutOfMemoryException type check."""
        src = inspect.getsource(etl_mod._compute_profile_duckdb)
        self.assertRegex(
            src,
            r"isinstance\(\s*exc\s*,\s*.*OutOfMemoryException",
            "Expected isinstance(exc, duckdb.OutOfMemoryException) OOM detection.",
        )

    def test_r109_6_schema_hash_should_not_depend_on_runtime_guard_source(self):
        """Risk #2: hash should avoid invalidation from runtime-only helper edits."""
        src = inspect.getsource(etl_mod.compute_profile_schema_hash)
        self.assertNotIn(
            "inspect.getsource(_compute_profile_duckdb)",
            src,
            "Expected schema hash not to include full _compute_profile_duckdb source.",
        )


if __name__ == "__main__":
    unittest.main()

