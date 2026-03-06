"""Guardrail tests for ClickHouse temp-table profile-load risks.

Original round: tests-only with expectedFailure markers (production code unfixed).
Current round: production code fixed; expectedFailure markers removed, regex bug fixed.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _get_func_src(name: str) -> str:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    raise AssertionError(f"function {name!r} not found in trainer.py")


def _extract_insert_batch_value(func_src: str) -> int | None:
    """Best-effort parser for `_INSERT_BATCH = <int>` in load_player_profile.

    Handles Python numeric literal underscores (e.g. 200_000).
    """
    m = re.search(r"_INSERT_BATCH\s*=\s*([\d_]+)", func_src)
    if not m:
        return None
    return int(m.group(1).replace("_", ""))


class TestTrainerReviewRisksTempTable(unittest.TestCase):
    """Review risks converted to executable lint/test rules."""

    def test_temp_table_strategy_exists_for_large_canonical_ids(self):
        """Sanity: large canonical_id path must exist (regression guard)."""
        src = _get_func_src("load_player_profile")
        self.assertIn("_LARGE_CID_THRESHOLD", src)
        self.assertIn("CREATE TEMPORARY TABLE _tmp_profile_cids", src)
        self.assertIn("INNER JOIN _tmp_profile_cids", src)

    def test_session_client_is_closed_in_finally(self):
        """Risk-1: session-scoped ClickHouse client should be closed reliably."""
        src = _get_func_src("load_player_profile")
        self.assertIn("session_client.close()", src)
        self.assertIn("finally:", src)

    def test_join_type_rule_requires_non_string_temp_id_or_explicit_cast(self):
        """Risk-2: avoid String-vs-Int JOIN mismatch without CAST/typed temp table."""
        src = _get_func_src("load_player_profile")

        # Guardrail:
        # - preferred: temp table canonical_id is Int/UInt type, OR
        # - fallback: explicit CAST in JOIN predicate.
        has_int_temp_id = bool(
            re.search(
                r"CREATE TEMPORARY TABLE\s+_tmp_profile_cids\s*\(\s*canonical_id\s+U?Int\d+\s*\)",
                src,
            )
        )
        has_explicit_cast = "CAST(" in src
        self.assertTrue(
            has_int_temp_id or has_explicit_cast,
            msg=(
                "temp-table JOIN should either use numeric canonical_id type "
                "or explicit CAST to prevent type mismatch/perf regression."
            ),
        )

    def test_insert_stage_has_specific_error_logging(self):
        """Risk-3: temp-table INSERT failures should emit stage-specific logs."""
        src = _get_func_src("load_player_profile")
        self.assertIn("session_client.insert(", src)
        # Minimal lint-like requirement: insert stage has dedicated logger.error.
        self.assertRegex(
            src,
            r'logger\.error\([^)]*insert[^)]*\)',
            msg="Insert stage should have explicit error log for easier incident triage.",
        )

    def test_insert_batch_size_rule_for_large_id_lists(self):
        """Risk-4: batch size should be tuned to reduce round trips."""
        src = _get_func_src("load_player_profile")
        value = _extract_insert_batch_value(src)
        self.assertIsNotNone(value, "Expected _INSERT_BATCH assignment in temp-table path.")
        self.assertGreaterEqual(
            int(value),
            200_000,
            msg="Guardrail: _INSERT_BATCH should be >= 200,000 for large production ID sets.",
        )


if __name__ == "__main__":
    unittest.main()
