"""Minimal reproducible tests for Code Review — OOM Phase 1 + Phase 2a 變更 (STATUS.md 2026-03-11).

Reviewer risk points (Code Review section in STATUS.md) are turned into source/contract
tests only. No production code changes. Tests that encode a desired contract not yet
implemented use @unittest.expectedFailure until production is fixed.
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
    end_match = re.search(
        r"\n    # [0-9]+\. Load all chunks|\n    def [a-z_]+\(|\n    # [0-9]+\. ", rest
    )
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


def _find_step7_use_duckdb_false_block(body: str) -> str | None:
    """Return the block under 'if not STEP7_USE_DUCKDB:' up to (but not including) 'try:'."""
    start = body.find("if not STEP7_USE_DUCKDB:")
    if start == -1:
        return None
    rest = body[start:]
    # Same indent level: next line at 8 spaces that starts try:
    end_match = re.search(r"\n        try:", rest)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


def _find_canonical_pandas_branch(source: str) -> str | None:
    """Return the block under 'if use_full_sessions_pandas:' in run_pipeline (canonical build)."""
    start = source.find("if use_full_sessions_pandas:")
    if start == -1:
        return None
    rest = source[start : start + 2500]
    # End at next 'else:' at same indent (DuckDB path) or next major block
    end_match = re.search(r"\n        else:\s*\n\s+sess_path", rest)
    end = end_match.start() if end_match else min(800, len(rest))
    return rest[:end]


# --- Risk #1: STEP7_KEEP_TRAIN_ON_DISK=True and STEP7_USE_DUCKDB=False should raise ValueError ---
class TestOomReview1Step7KeepTrainRequiresUseDuckdb(unittest.TestCase):
    """Review #1 (high): When STEP7_USE_DUCKDB=False, if STEP7_KEEP_TRAIN_ON_DISK=True production should raise ValueError.

    Contract: config states STEP7_KEEP_TRAIN_ON_DISK requires STEP7_USE_DUCKDB=True; the orchestrator
    should fail fast instead of entering pandas fallback (which returns full train in memory, breaking B+).
    """

    def test_step7_invalid_combo_should_raise_value_error_in_source(self):
        """Production _step7_sort_and_split: when 'if not STEP7_USE_DUCKDB' block must check STEP7_KEEP_TRAIN_ON_DISK and raise ValueError before fallback."""
        source = _get_trainer_source()
        body = _find_step7_sort_and_split_body(source)
        self.assertIsNotNone(body, "_step7_sort_and_split not found")
        block = _find_step7_use_duckdb_false_block(body)
        self.assertIsNotNone(block, "'if not STEP7_USE_DUCKDB' block not found")
        # Desired contract: before _step7_pandas_fallback we must have a check that raises ValueError when STEP7_KEEP_TRAIN_ON_DISK
        has_keep_check = "STEP7_KEEP_TRAIN_ON_DISK" in block
        has_value_error = "ValueError" in block
        has_raise = "raise " in block
        self.assertTrue(
            has_keep_check and has_value_error and has_raise,
            "When STEP7_USE_DUCKDB=False, production should check STEP7_KEEP_TRAIN_ON_DISK and raise ValueError "
            "before calling _step7_pandas_fallback (STATUS.md Code Review #1).",
        )


# --- Risk #2: A19 warning must be present when STEP7_USE_DUCKDB=False ---
class TestOomReview2Step7UseDuckdbFalseLogsOomWarning(unittest.TestCase):
    """Review #2 (medium): A19 — when STEP7_USE_DUCKDB=False the code must log a warning with high OOM risk."""

    def test_step7_use_duckdb_false_block_logs_oom_warning(self):
        """Production 'if not STEP7_USE_DUCKDB' block must contain logger.warning with STEP7_USE_DUCKDB=False and high OOM risk."""
        source = _get_trainer_source()
        body = _find_step7_sort_and_split_body(source)
        self.assertIsNotNone(body)
        block = _find_step7_use_duckdb_false_block(body)
        self.assertIsNotNone(block)
        self.assertIn(
            "logger.warning",
            block,
            "A19: when STEP7_USE_DUCKDB=False must log warning",
        )
        self.assertIn(
            "STEP7_USE_DUCKDB=False",
            block,
            "A19: warning message must mention STEP7_USE_DUCKDB=False",
        )
        self.assertIn(
            "high OOM risk",
            block,
            "A19: warning message must mention high OOM risk (STATUS.md Code Review #2).",
        )


# --- Risk #3: STEP7_USE_DUCKDB is read from config (import-time) ---
class TestOomReview3Step7UseDuckdbReadFromConfig(unittest.TestCase):
    """Review #3 (low): STEP7_USE_DUCKDB is read from config at import time; document in test."""

    def test_step7_use_duckdb_read_from_config_in_source(self):
        """Trainer must read STEP7_USE_DUCKDB from _cfg (value is read at import time, not runtime)."""
        source = _get_trainer_source()
        self.assertIn(
            'getattr(_cfg, "STEP7_USE_DUCKDB"',
            source,
            "STEP7_USE_DUCKDB must be read from config (import-time); see STATUS.md Review #3.",
        )


# --- Risk #4: A03 warning must be present in canonical full-sessions pandas branch ---
class TestOomReview4CanonicalFullSessionsPandasLogsWarning(unittest.TestCase):
    """Review #4 (medium): A03 — when CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS=True the code must log warning."""

    def test_canonical_map_full_sessions_pandas_branch_logs_warning(self):
        """Production 'if use_full_sessions_pandas' block must contain logger.warning with CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS and A03/high OOM risk."""
        source = _get_trainer_source()
        block = _find_canonical_pandas_branch(source)
        self.assertIsNotNone(block, "'if use_full_sessions_pandas' block not found")
        self.assertIn(
            "logger.warning",
            block,
            "A03: full sessions pandas branch must log warning",
        )
        self.assertIn(
            "CANONICAL_MAP_USE_FULL_SESSIONS_PANDAS",
            block,
            "A03: warning message must mention the flag",
        )
        self.assertTrue(
            "high OOM risk" in block or "A03" in block,
            "A03: warning message must mention high OOM risk or A03 (STATUS.md Code Review #4).",
        )


if __name__ == "__main__":
    unittest.main()
