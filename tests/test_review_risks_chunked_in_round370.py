"""Round 370: MRE guards for chunked-IN profile-load risks.

This round is tests-only:
- no production code changes
- convert reviewer findings into executable, minimal guardrails
"""

from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _get_func_src(name: str) -> str:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    raise AssertionError(f"function {name!r} not found in trainer.py")


class TestRound370ChunkedInRisks(unittest.TestCase):
    """Risk guards for load_player_profile chunked-IN strategy."""

    def test_r_cin_1_chunked_concat_should_global_sort(self):
        """R-CIN-1: after concat, profile rows should be globally sorted for merge_asof."""
        src = _get_func_src("load_player_profile")
        self.assertIn("pd.concat(_parts, ignore_index=True)", src)
        self.assertIn(
            "sort_values([\"canonical_id\", \"snapshot_dtm\"]",
            src,
            "Expected global sort after pd.concat in chunked-IN path.",
        )

    def test_r_cin_2_concat_should_have_empty_parts_guard(self):
        """R-CIN-2: concat should be guarded for empty objects."""
        src = _get_func_src("load_player_profile")
        self.assertTrue(
            ("if _parts else pd.DataFrame()" in src)
            or ("if _parts else pandas.DataFrame()" in src),
            "Expected explicit empty _parts guard before pd.concat.",
        )

    def test_r_cin_3_large_list_path_should_log_progress(self):
        """R-CIN-3: large chunked loop should expose progress logs for observability."""
        src = _get_func_src("load_player_profile")
        # We already have entry log for chunked mode; this guard asks for per-batch progress.
        self.assertRegex(
            src,
            r"logger\.info\([^)]*batch\s*%d/%d",
            "Expected batch progress logger.info in chunked-IN loop.",
        )

    def test_r_cin_4_per_batch_failure_should_be_logged(self):
        """R-CIN-4: each batch query failure should have stage-specific error logging."""
        src = _get_func_src("load_player_profile")
        self.assertIn("for _i in range(0, len(_cid_list), _IN_BATCH):", src)
        self.assertRegex(
            src,
            r'logger\.error\([^)]*batch[^)]*failed',
            "Expected per-batch logger.error in chunked-IN query loop.",
        )

    def test_r_cin_5_avoid_str_format_on_sql_template(self):
        """R-CIN-5: avoid .format SQL templating for cid_clause injection point."""
        src = _get_func_src("load_player_profile")
        self.assertNotIn(
            ".format(",
            src,
            "Expected explicit SQL strings instead of .format templating.",
        )


if __name__ == "__main__":
    unittest.main()
