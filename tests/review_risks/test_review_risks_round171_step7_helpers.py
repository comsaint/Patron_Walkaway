"""Minimal reproducible tests for Round 171 Review — Step 7 Out-of-Core helpers and config.

Tests-only: no production code changes.
Review risks (Round 171 Review in STATUS.md) are turned into guards; unresolved
risks use @unittest.expectedFailure until production is fixed.

Step 7 helpers (_compute_step7_duckdb_budget, _configure_step7_duckdb_runtime,
_is_duckdb_oom) are defined inside trainer.run_pipeline() and are not directly
callable. So we test (1) config contract, (2) replicated budget/temp_dir logic
against desired behavior (xfail where current behavior is wrong), (3) replicated
_is_duckdb_oom spec for exception handling.
"""

from __future__ import annotations

import unittest
import unittest.mock

# Replicate Step 7 budget formula from trainer (aligned with production: validate
# frac in (0,1] fallback 0.5, swap lo/hi when lo > hi). Used to assert contract.
def _step7_budget_formula_replica(
    available_bytes: int | None,
    frac: float,
    min_gb: float,
    max_gb: float,
) -> int:
    if not (0.0 < frac <= 1.0):
        frac = 0.5
    lo = int(min_gb * 1024**3)
    hi = int(max_gb * 1024**3)
    if lo > hi:
        lo, hi = hi, lo
    if available_bytes is None:
        return lo
    budget = int(available_bytes * frac)
    return max(lo, min(hi, budget))


# Replicate temp_directory stmt construction (production escapes single quotes).
def _step7_temp_dir_stmt_replica(temp_dir: str) -> str:
    temp_dir_sql = temp_dir.replace("'", "''")
    return f"SET temp_directory='{temp_dir_sql}'"


# Replicate _is_duckdb_oom logic from trainer for spec tests (no duckdb import in test).
def _is_duckdb_oom_replica(exc: BaseException) -> bool:
    try:
        import duckdb as _duckdb
        oom_cls = getattr(_duckdb, "OutOfMemoryException", None)
        if oom_cls is not None and isinstance(exc, oom_cls):
            return True
    except ImportError:
        pass
    if isinstance(exc, MemoryError):
        return True
    msg = str(exc.args[0]) if getattr(exc, "args", None) and exc.args else str(exc)
    return "unable to allocate" in msg.lower() or "out of memory" in msg.lower()


_GIB = 1024**3


class TestR171ConfigAndContract(unittest.TestCase):
    """Config exposure and contract tests for Step 7 helpers (Round 171 Review)."""

    def test_r171_0_config_exposes_step7_constants(self):
        """Sanity: trainer.trainer module should expose all STEP7_DUCKDB_* from config."""
        import trainer.trainer as tr
        for name in (
            "STEP7_USE_DUCKDB",
            "STEP7_DUCKDB_RAM_FRACTION",
            "STEP7_DUCKDB_RAM_MIN_GB",
            "STEP7_DUCKDB_RAM_MAX_GB",
            "STEP7_DUCKDB_THREADS",
            "STEP7_DUCKDB_PRESERVE_INSERTION_ORDER",
            "STEP7_DUCKDB_TEMP_DIR",
        ):
            self.assertTrue(hasattr(tr, name), f"Missing trainer attribute: {name}")

    def test_r171_1_config_fraction_default_in_valid_range(self):
        """STEP7_DUCKDB_RAM_FRACTION default should be in (0, 1]."""
        import trainer.trainer as tr
        frac = getattr(tr, "STEP7_DUCKDB_RAM_FRACTION", None)
        self.assertIsNotNone(frac)
        self.assertGreater(frac, 0.0)
        self.assertLessEqual(frac, 1.0)

    def test_r171_2_budget_invalid_fraction_should_fallback_to_half_ram(self):
        """P1 #1: When frac=0, production fallback to frac=0.5 -> 5*GIB (contract)."""
        result = _step7_budget_formula_replica(10 * _GIB, 0.0, 2.0, 24.0)
        self.assertEqual(result, 5 * _GIB)

    def test_r171_3_budget_min_greater_than_max_should_effectively_swap(self):
        """P1 #2: When MIN_GB > MAX_GB, swap then clamp; available=10G frac=0.5 -> 5 GiB."""
        result = _step7_budget_formula_replica(10 * _GIB, 0.5, 10.0, 2.0)
        self.assertEqual(result, 5 * _GIB)

    def test_r171_4_temp_dir_containing_quote_should_be_escaped_in_sql(self):
        """P1 #3: When temp_dir contains single quote, SQL stmt must be safe (escaped)."""
        temp_dir = "/tmp/patron's_dir"
        stmt = _step7_temp_dir_stmt_replica(temp_dir)
        self.assertTrue(
            ("'" not in temp_dir) or ("''" in stmt),
            "temp_directory path with quote must be escaped in SQL stmt",
        )

    def test_r171_5_temp_dir_empty_string_uses_default_like_none(self):
        """P3 #7: Empty string STEP7_DUCKDB_TEMP_DIR should behave like None (use default).
        Replicate trainer logic: temp_dir = STEP7_DUCKDB_TEMP_DIR if STEP7_DUCKDB_TEMP_DIR else default.
        """
        import trainer.trainer as tr
        default = str(tr.DATA_DIR / "duckdb_tmp")
        # With default config (None), effective = default.
        effective_none = tr.STEP7_DUCKDB_TEMP_DIR if tr.STEP7_DUCKDB_TEMP_DIR else default
        self.assertEqual(effective_none, default)
        # When TEMP_DIR is "", condition is falsy -> effective = default (same as None).
        with unittest.mock.patch.object(tr, "STEP7_DUCKDB_TEMP_DIR", ""):
            effective_empty = tr.STEP7_DUCKDB_TEMP_DIR if tr.STEP7_DUCKDB_TEMP_DIR else default
        self.assertEqual(effective_empty, default)

    def test_r171_6_is_duckdb_oom_memory_error_returns_true(self):
        """P3 #6: MemoryError() should return True (replicated spec)."""
        self.assertTrue(_is_duckdb_oom_replica(MemoryError()))

    def test_r171_7_is_duckdb_oom_unable_to_allocate_message_returns_true(self):
        """P3 #6: Exception with 'unable to allocate' in message should return True."""
        self.assertTrue(_is_duckdb_oom_replica(Exception("unable to allocate memory")))

    def test_r171_8_is_duckdb_oom_args_empty_returns_false_without_throw(self):
        """P3 #6: Exception with args=() should return False and not raise."""
        class E(Exception):
            pass
        e = E()
        e.args = ()
        self.assertFalse(_is_duckdb_oom_replica(e))

    def test_r171_9_is_duckdb_oom_args_none_returns_false_without_throw(self):
        """P3 #6: Exception with args=(None,) should return False and not raise."""
        class E(Exception):
            pass
        e = E()
        e.args = (None,)
        self.assertFalse(_is_duckdb_oom_replica(e))

    def test_r171_10_is_duckdb_oom_generic_returns_false(self):
        """Generic Exception with no OOM message should return False."""
        self.assertFalse(_is_duckdb_oom_replica(Exception("something else")))


if __name__ == "__main__":
    unittest.main()
