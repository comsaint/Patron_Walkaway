"""Task 3 / Phase 3 reviewer risks -> minimal reproducible contract tests.

These tests document current risk surfaces without changing production code.
"""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCORER_PATH = REPO_ROOT / "trainer" / "serving" / "scorer.py"


def _scorer_text() -> str:
    return SCORER_PATH.read_text(encoding="utf-8")


class TestRisk1SqliteBulkInChunkGuard(unittest.TestCase):
    """Risk #1 fixed: bulk IN path now has chunking guard."""

    def test_bulk_queries_use_chunking_constant_and_loop(self) -> None:
        src = _scorer_text()
        self.assertIn("def get_session_totals_bulk(", src)
        self.assertIn("def get_session_count_bulk(", src)
        self.assertIn("def get_historical_avg_bulk(", src)
        self.assertIn("_SQLITE_IN_CHUNK_SIZE = 500", src)
        self.assertIn("for i in range(0, len(keys), _SQLITE_IN_CHUNK_SIZE):", src)


class TestRisk2IncrementalFallbackIsFullWindow(unittest.TestCase):
    """Risk #2 fixed: canonical-map-missing branch falls back to full window."""

    def test_missing_or_invalid_canonical_map_falls_back_to_full_bets(self) -> None:
        src = _scorer_text()
        self.assertIn("def _select_incremental_bets_window(", src)
        self.assertIn("if canonical_map is None or canonical_map.empty:", src)
        self.assertIn('logger.warning("[scorer] Incremental narrowing disabled: canonical_map unavailable")', src)
        self.assertIn("return bets", src)
        self.assertIn('if not {"player_id", "canonical_id"}.issubset(canonical_map.columns):', src)


class TestRisk3NumbaCheckUsesJitProbeAtStartup(unittest.TestCase):
    """Risk #3: startup check compiles njit probe without explicit strict-mode gate."""

    def test_numba_check_has_njit_probe_and_no_strict_toggle(self) -> None:
        src = _scorer_text()
        self.assertIn("def _check_numba_runtime_once()", src)
        self.assertIn("from numba import njit", src)
        self.assertIn("@njit(cache=False)", src)
        self.assertNotIn("SCORER_NUMBA_STRICT_CHECK", src)


class TestRisk4IncrementalPathNoCoverageGuardrail(unittest.TestCase):
    """Risk #4: no high-coverage guardrail to skip narrowing work."""

    def test_no_new_pid_coverage_guardrail_in_incremental_selector(self) -> None:
        src = _scorer_text()
        start = src.find("def _select_incremental_bets_window(")
        self.assertNotEqual(start, -1)
        end = src.find("\ndef ", start + 1)
        block = src[start : end if end != -1 else len(src)]
        self.assertIn("new_pids = set(", block)
        self.assertIn("expanded_pids = set(", block)
        self.assertNotIn("coverage", block)
        self.assertNotIn("SCORER_INCREMENTAL_MAX_COVERAGE", block)


class TestRisk5IncrementalWarningNoThrottle(unittest.TestCase):
    """Risk #5: fallback warning path has no one-shot/throttle guard."""

    def test_warning_fallback_paths_have_no_throttle_state(self) -> None:
        src = _scorer_text()
        start = src.find("def _select_incremental_bets_window(")
        self.assertNotEqual(start, -1)
        end = src.find("\ndef ", start + 1)
        block = src[start : end if end != -1 else len(src)]
        self.assertIn("Incremental narrowing disabled: canonical_map unavailable", block)
        self.assertIn("Incremental narrowing disabled: canonical_map missing required columns", block)
        self.assertNotIn("WARNED_ONCE", block)
        self.assertNotIn("warning_throttle", block)


class TestRisk6BulkPathNoDedupBeforeChunking(unittest.TestCase):
    """Risk #6: bulk keys are not deduplicated before chunk loop."""

    def test_bulk_helpers_materialize_keys_without_drop_duplicates(self) -> None:
        src = _scorer_text()
        s1 = src.find("def get_session_totals_bulk(")
        s2 = src.find("def get_session_count_bulk(")
        s3 = src.find("def get_historical_avg_bulk(")
        self.assertNotEqual(s1, -1)
        self.assertNotEqual(s2, -1)
        self.assertNotEqual(s3, -1)
        b1 = src[s1:s2]
        b2 = src[s2:s3]
        next_def = src.find("\ndef ", s3 + 1)
        b3 = src[s3 : next_def if next_def != -1 else len(src)]
        for block in (b1, b2, b3):
            self.assertIn("keys = [str(", block)
            self.assertNotIn("drop_duplicates", block)
            self.assertNotIn("set(keys)", block)


class TestRisk7TargetCidsEmptyStillPartialNarrowing(unittest.TestCase):
    """Risk #7: target_cids empty branch still narrows to new_pids only."""

    def test_target_cids_empty_branch_is_partial_narrowing(self) -> None:
        src = _scorer_text()
        start = src.find("def _select_incremental_bets_window(")
        self.assertNotEqual(start, -1)
        end = src.find("\ndef ", start + 1)
        block = src[start : end if end != -1 else len(src)]
        self.assertIn("if not target_cids:", block)
        self.assertIn('return bets[bets["player_id"].astype(str).isin(new_pids)].copy()', block)


class TestRisk8ChunkSizeNotConfigurable(unittest.TestCase):
    """Risk #8: chunk size is hardcoded; no config override contract."""

    def test_sqlite_chunk_size_has_no_env_or_config_override(self) -> None:
        src = _scorer_text()
        self.assertIn("_SQLITE_IN_CHUNK_SIZE = 500", src)
        self.assertNotIn("SCORER_SQLITE_IN_CHUNK_SIZE", src)
        self.assertNotIn("getattr(config, \"SCORER_SQLITE_IN_CHUNK_SIZE\"", src)


if __name__ == "__main__":
    unittest.main()

