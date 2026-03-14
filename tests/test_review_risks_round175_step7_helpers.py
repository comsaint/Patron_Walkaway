"""Minimal reproducible tests for Round 175 Review — _step7_oom_failsafe_next_frac and _step7_pandas_fallback.

Tests-only: no production code changes.
Review risks (Round 175 Review in STATUS.md) are turned into contract tests;
helpers are defined inside trainer.run_pipeline() so we test via replicated logic.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

import pandas as pd


def _get_trainer_source() -> str:
    path = Path(__file__).resolve().parent.parent / "trainer" / "training" / "trainer.py"
    return path.read_text(encoding="utf-8")


def _find_step7_pandas_fallback_body(source: str) -> str | None:
    """Return the body of _step7_pandas_fallback (from def to next top-level def)."""
    start = source.find("def _step7_pandas_fallback(")
    if start == -1:
        return None
    rest = source[start:]
    end_match = re.search(r"\n    def [a-z_]+\(|\n    # [0-9]+\. ", rest)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


# --- Replica: _step7_oom_failsafe_next_frac with desired contract (validate 0 < current_frac <= 1). ---
def _step7_oom_failsafe_next_frac_replica(current_frac: float, min_frac: float) -> tuple[float, bool]:
    """Replicate production formula; contract adds ValueError for invalid current_frac."""
    if not (0.0 < current_frac <= 1.0):
        raise ValueError("current_frac must be in (0, 1], got %s" % current_frac)
    new_frac = max(min_frac, current_frac / 2.0)
    if new_frac >= current_frac:
        raise RuntimeError(
            "Step 7 DuckDB OOM and NEG_SAMPLE_FRAC already at floor (%.2f). "
            "Reduce training window (--days / --start --end) or add RAM." % min_frac
        )
    return (new_frac, True)


# --- Replica: pandas fallback fraction validation (desired: if/raise, not assert). ---
def _step7_pandas_fallback_validate_frac_replica(
    train_frac: float, valid_frac: float
) -> None:
    """Contract: invalid fractions must raise ValueError (not assert)."""
    if not (0 < train_frac and 0 < valid_frac and train_frac + valid_frac < 1.0):
        raise ValueError(
            "train_frac and valid_frac must be in (0, 1) and train_frac + valid_frac < 1.0"
        )


# --- Replica: pandas fallback empty full_df contract (concat后 n_rows==0 -> ValueError). ---
def _step7_pandas_fallback_empty_check_replica(chunk_paths: list[Path]) -> None:
    """Contract: empty chunk_paths or concat with 0 rows must raise ValueError."""
    if not chunk_paths:
        raise ValueError("chunk_paths must be non-empty")
    import pandas as pd
    all_dfs = [pd.read_parquet(p) for p in chunk_paths]
    full_df = pd.concat(all_dfs, ignore_index=True)
    n_rows = len(full_df)
    if n_rows == 0:
        raise ValueError("chunk_paths produced no rows")


class TestR175OomFailsafeNextFracContract(unittest.TestCase):
    """P1 #1: _step7_oom_failsafe_next_frac — input validation and formula (replicated contract)."""

    def test_r175_failsafe_invalid_zero_raises_value_error(self):
        """current_frac=0 should raise ValueError (invalid input)."""
        import trainer.trainer as tr
        min_frac = tr.NEG_SAMPLE_FRAC_MIN
        with self.assertRaises(ValueError) as ctx:
            _step7_oom_failsafe_next_frac_replica(0.0, min_frac)
        self.assertIn("current_frac", str(ctx.exception))

    def test_r175_failsafe_invalid_negative_raises_value_error(self):
        """current_frac=-0.1 should raise ValueError."""
        import trainer.trainer as tr
        min_frac = tr.NEG_SAMPLE_FRAC_MIN
        with self.assertRaises(ValueError):
            _step7_oom_failsafe_next_frac_replica(-0.1, min_frac)

    def test_r175_failsafe_invalid_gt_one_raises_value_error(self):
        """current_frac=1.5 should raise ValueError."""
        import trainer.trainer as tr
        min_frac = tr.NEG_SAMPLE_FRAC_MIN
        with self.assertRaises(ValueError):
            _step7_oom_failsafe_next_frac_replica(1.5, min_frac)

    def test_r175_failsafe_at_floor_raises_runtime_error(self):
        """current_frac=NEG_SAMPLE_FRAC_MIN (already at floor) should raise RuntimeError."""
        import trainer.trainer as tr
        min_frac = tr.NEG_SAMPLE_FRAC_MIN
        with self.assertRaises(RuntimeError) as ctx:
            _step7_oom_failsafe_next_frac_replica(min_frac, min_frac)
        self.assertIn("floor", str(ctx.exception).lower())

    def test_r175_failsafe_valid_half_returns_quarter(self):
        """current_frac=0.5 -> (0.25, True)."""
        import trainer.trainer as tr
        min_frac = tr.NEG_SAMPLE_FRAC_MIN
        new_frac, should_retry = _step7_oom_failsafe_next_frac_replica(0.5, min_frac)
        self.assertEqual(new_frac, 0.25)
        self.assertTrue(should_retry)

    def test_r175_failsafe_valid_clamp_to_min(self):
        """current_frac=0.08 -> (0.05, True) (clamp to NEG_SAMPLE_FRAC_MIN)."""
        import trainer.trainer as tr
        min_frac = tr.NEG_SAMPLE_FRAC_MIN
        new_frac, should_retry = _step7_oom_failsafe_next_frac_replica(0.08, min_frac)
        self.assertEqual(new_frac, min_frac)
        self.assertTrue(should_retry)


class TestR175PandasFallbackFractionContract(unittest.TestCase):
    """P1 #2: _step7_pandas_fallback — fraction validation must be if/raise, not assert."""

    def test_r175_fallback_train_frac_zero_raises_value_error(self):
        """train_frac=0, valid_frac=0.15 should raise ValueError (contract)."""
        with self.assertRaises(ValueError):
            _step7_pandas_fallback_validate_frac_replica(0.0, 0.15)

    def test_r175_fallback_sum_equals_one_raises_value_error(self):
        """train_frac+valid_frac=1.0 should raise ValueError."""
        with self.assertRaises(ValueError):
            _step7_pandas_fallback_validate_frac_replica(0.7, 0.3)

    def test_r175_fallback_valid_fractions_do_not_raise(self):
        """train_frac=0.7, valid_frac=0.15 should not raise."""
        _step7_pandas_fallback_validate_frac_replica(0.7, 0.15)


class TestR175PandasFallbackEmptyContract(unittest.TestCase):
    """P2 #3: _step7_pandas_fallback — empty chunk_paths / empty full_df must raise ValueError."""

    def test_r175_fallback_empty_chunk_paths_raises_value_error(self):
        """Empty chunk_paths should raise ValueError (contract)."""
        with self.assertRaises(ValueError) as ctx:
            _step7_pandas_fallback_empty_check_replica([])
        self.assertIn("non-empty", str(ctx.exception))

    def test_r175_fallback_empty_concat_raises_value_error(self):
        """Concat of chunks that yield 0 rows should raise ValueError (contract)."""
        empty_df = pd.DataFrame(columns=["payout_complete_dtm", "canonical_id", "bet_id"])
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            path = Path(f.name)
        try:
            empty_df.to_parquet(path, index=False)
            with self.assertRaises(ValueError) as ctx:
                _step7_pandas_fallback_empty_check_replica([path])
            self.assertIn("no rows", str(ctx.exception))
        finally:
            path.unlink(missing_ok=True)


class TestR175ProductionSourceAssertReplaced(unittest.TestCase):
    """P1 #2: Document that production should use if/raise instead of assert for fractions."""

    def test_r175_fallback_body_should_use_value_error_not_assert(self):
        """Production _step7_pandas_fallback should validate fractions with if/raise, not assert (Round 175 Review P1 #2)."""
        source = _get_trainer_source()
        body = _find_step7_pandas_fallback_body(source)
        self.assertIsNotNone(body)
        for line in body.splitlines():
            if "assert" in line and ("train_frac" in line or "valid_frac" in line):
                self.fail(
                    "Production should use if/raise ValueError for train_frac/valid_frac, not assert"
                )


if __name__ == "__main__":
    unittest.main()
