"""Guardrail tests for chunked-IN profile-load strategy in load_player_profile.

Replaces the old temp-table guardrails (temp-table approach dropped because
CREATE TEMPORARY TABLE permission is unavailable in production ClickHouse).

Strategy: large canonical_id lists are split into chunks of _IN_BATCH and
queried with IN (...), results merged with pd.concat.  No CREATE TABLE
permission required.
"""

from __future__ import annotations

import ast
import pathlib
import re
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


def _extract_in_batch_value(func_src: str) -> int | None:
    """Parse `_IN_BATCH = <int>` from load_player_profile source.

    Handles Python numeric literal underscores (e.g. 4_000).
    """
    m = re.search(r"_IN_BATCH\s*=\s*([\d_]+)", func_src)
    if not m:
        return None
    return int(m.group(1).replace("_", ""))


class TestTrainerChunkedInProfileLoad(unittest.TestCase):
    """Guardrail tests for chunked-IN load_player_profile strategy."""

    def _src(self) -> str:
        return _get_func_src("load_player_profile")

    # ------------------------------------------------------------------
    # Sanity: strategy markers present
    # ------------------------------------------------------------------

    def test_chunked_in_strategy_markers_present(self):
        """Regression guard: chunked-IN path must exist and must NOT use temp tables."""
        src = self._src()
        # Chunked path markers
        self.assertIn("_IN_BATCH", src)
        self.assertIn("pd.concat", src)
        # Temp-table artefacts must be gone
        self.assertNotIn("CREATE TEMPORARY TABLE", src)
        self.assertNotIn("session_client", src)

    # ------------------------------------------------------------------
    # Risk-1 (was: connection leak) → now: shared client reuse
    # ------------------------------------------------------------------

    def test_uses_shared_get_clickhouse_client_not_session_client(self):
        """Risk-1 equivalent: must use the shared cached client, not a per-call session client."""
        src = self._src()
        self.assertIn("get_clickhouse_client()", src)
        self.assertNotIn("session_client", src)

    # ------------------------------------------------------------------
    # Risk-2 (was: JOIN type mismatch) → now: no type ambiguity needed
    # ------------------------------------------------------------------

    def test_no_cast_or_join_in_chunked_path(self):
        """Risk-2 equivalent: chunked-IN avoids JOIN entirely, so no CAST/type-mismatch risk."""
        src = self._src()
        # The old INNER JOIN _tmp_profile_cids must be gone.
        self.assertNotIn("INNER JOIN _tmp_profile_cids", src)

    # ------------------------------------------------------------------
    # Risk-3 (was: insert-stage logging) → now: batch progress logging
    # ------------------------------------------------------------------

    def test_large_id_path_emits_info_log(self):
        """Risk-3 equivalent: large-list branch should emit an info log with batch size."""
        src = self._src()
        self.assertRegex(
            src,
            r'logger\.info\([^)]*canonical_ids[^)]*chunked',
            msg="Large-list branch should log 'chunked' strategy with ID count.",
        )

    # ------------------------------------------------------------------
    # Risk-4 (was: INSERT batch size) → now: IN batch size is safe
    # ------------------------------------------------------------------

    def test_in_batch_size_is_within_safe_range(self):
        """Risk-4 equivalent: _IN_BATCH must keep each SQL well under 256 KB max_query_size."""
        src = self._src()
        value = _extract_in_batch_value(src)
        self.assertIsNotNone(value, "Expected _IN_BATCH assignment in load_player_profile.")
        # Upper bound: 10_000 IDs × ~20 chars each ≈ 200 KB — still under 256 KB limit.
        # Lower bound: 1_000 — anything smaller means too many round-trips for 323k IDs.
        self.assertGreaterEqual(int(value), 1_000, "_IN_BATCH too small (too many round-trips).")
        self.assertLessEqual(int(value), 10_000, "_IN_BATCH too large (risks exceeding max_query_size).")

    # ------------------------------------------------------------------
    # Boundary: three-branch logic (no filter / small list / large list)
    # ------------------------------------------------------------------

    def test_three_branch_logic_present(self):
        """Structural guardrail: function must handle no-filter, small, and large lists."""
        src = self._src()
        # no-filter branch — either the old cid_clause marker or the new explicit query variable
        no_filter_present = (
            'cid_clause=""' in src.replace(" ", "").replace("\n", "")
            or "_query_no_filter" in src
        )
        self.assertTrue(no_filter_present, "Expected a no-filter query path in load_player_profile.")
        # small-list branch
        self.assertIn("canonical_ids", src)
        # large-list branch (the concat)
        self.assertIn("pd.concat", src)


if __name__ == "__main__":
    unittest.main()
