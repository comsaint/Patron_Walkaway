"""tests/test_review_risks_round50.py
=====================================
Minimal reproducible guardrail tests for Round 5 review findings (R68–R73).

Tests-only — no production code changes.

Uses AST / source inspection to verify code structure without importing
trainer.py (which pulls heavy dependencies like clickhouse_connect).
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_BACKTESTER_PATH = _REPO_ROOT / "trainer" / "backtester.py"

_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_BACKTESTER_SRC = _BACKTESTER_PATH.read_text(encoding="utf-8")

_TRAINER_TREE = ast.parse(_TRAINER_SRC)
_BACKTESTER_TREE = ast.parse(_BACKTESTER_SRC)


def _get_func_src(tree: ast.Module, src: str, name: str) -> str:
    """Return source segment for a module-level function."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _get_nested_func_src(tree: ast.Module, src: str, outer: str, inner: str) -> str:
    """Return source segment for a function nested inside *outer*."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == outer:
            for child in ast.walk(node):
                if isinstance(child, ast.FunctionDef) and child.name == inner:
                    return ast.get_source_segment(src, child) or ""
    return ""


def _func_names_defined(tree: ast.Module) -> set[str]:
    """Collect all module-level function names."""
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


# ===================================================================
# R68 — _train_one_model alert-count guard should be vectorised
# ===================================================================

class TestR68AlertCountVectorised(unittest.TestCase):
    """R68: The minimum-alert guard in _train_one_model must not use a
    per-threshold Python loop.  searchsorted (or equivalent) is expected."""

    def setUp(self) -> None:
        self.src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "_train_one_model")
        self.assertGreater(len(self.src), 0, "_train_one_model not found")

    def test_no_per_threshold_loop(self):
        """Must not contain `for t in pr_thresholds` or similar O(N) loop."""
        self.assertNotIn("for t in pr_thresholds", self.src)
        # Also guard against slight variants
        self.assertNotRegex(
            self.src,
            r"\[\s*\(val_scores\s*>=\s*t\)\.sum\(\)",
            "alert_counts must not be built with a list-comprehension loop over thresholds",
        )

    def test_uses_searchsorted(self):
        """searchsorted is the expected O(N log N) replacement."""
        self.assertIn("searchsorted", self.src)


# ===================================================================
# R69 — _apply_session_dq is dead code (DRY violation)
# ===================================================================

class TestR69NoDeadSessionDQ(unittest.TestCase):
    """R69: If _apply_session_dq exists, it must be called somewhere in
    trainer.py.  Otherwise it is dead code — a DRY maintenance hazard."""

    def test_apply_session_dq_not_dead_code(self):
        all_func_names = _func_names_defined(_TRAINER_TREE)
        if "_apply_session_dq" not in all_func_names:
            # Not defined → no dead code → pass.
            return

        # It exists — make sure it is actually *called* somewhere other than
        # its own definition.
        definition_src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "_apply_session_dq")
        remaining = _TRAINER_SRC.replace(definition_src, "", 1)
        self.assertIn(
            "_apply_session_dq(",
            remaining,
            "_apply_session_dq is defined but never called — dead code / DRY violation (R69)",
        )


# ===================================================================
# R70 — _assign_split must not use a per-row Python loop
# ===================================================================

class TestR70AssignSplitVectorised(unittest.TestCase):
    """R70: The train/valid/test split assignment in run_pipeline should be
    vectorised (dict.map) rather than a list comprehension over 23M rows."""

    def setUp(self) -> None:
        self.pipeline_src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "run_pipeline")
        self.assertGreater(len(self.pipeline_src), 0, "run_pipeline not found")

    def test_no_row_level_list_comprehension(self):
        """Must not contain `[_label((y, m)) for y, m in zip(…)]` pattern."""
        self.assertNotRegex(
            self.pipeline_src,
            r"\[_label\(",
            "run_pipeline should not use [_label(...) for ...] row-level loop (R70)",
        )

    def test_no_zip_year_month_loop(self):
        """Must not iterate via `for y, m in zip(year_s, month_s)`."""
        self.assertNotRegex(
            self.pipeline_src,
            r"for\s+y\s*,\s*m\s+in\s+zip\(",
            "run_pipeline should not iterate rows with for y, m in zip() (R70)",
        )


# ===================================================================
# R71 — _chunk_cache_key must incorporate config constants
# ===================================================================

class TestR71CacheKeyIncludesConfig(unittest.TestCase):
    """R71: _chunk_cache_key must include at least WALKAWAY_GAP_MIN (or a
    collective config hash) so that config changes invalidate the cache."""

    def setUp(self) -> None:
        self.src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "_chunk_cache_key")
        self.assertGreater(len(self.src), 0, "_chunk_cache_key not found")

    def test_cache_key_references_config_constants(self):
        has_walkaway = "WALKAWAY_GAP_MIN" in self.src
        has_history = "HISTORY_BUFFER_DAYS" in self.src
        has_generic_cfg = re.search(r"cfg[_\s]*(hash|str|key)", self.src, re.IGNORECASE)

        self.assertTrue(
            has_walkaway or has_history or has_generic_cfg,
            "_chunk_cache_key must include config constants (WALKAWAY_GAP_MIN, "
            "HISTORY_BUFFER_DAYS, or a cfg_hash) to avoid stale cache on config change (R71)",
        )


# ===================================================================
# R72 — backtester should not define compute_macro_by_visit_metrics
# ===================================================================

class TestR72MacroFunctionRename(unittest.TestCase):
    """R72: DEC-013 unified terminology to 'run'.  The macro metric function
    should not be named 'compute_macro_by_visit_metrics'."""

    def test_no_visit_named_macro_function(self):
        names = _func_names_defined(_BACKTESTER_TREE)
        self.assertNotIn(
            "compute_macro_by_visit_metrics",
            names,
            "backtester still defines compute_macro_by_visit_metrics — "
            "rename to compute_macro_by_gaming_day_metrics (or per-run when Phase 2 ready) (R72)",
        )


# ===================================================================
# R73 — _STATIC_REASON_CODES should not list removed features
# ===================================================================

class TestR73ReasonCodeCleanup(unittest.TestCase):
    """R73: run_id was removed from TRACK_B_FEATURE_COLS (R67).  Its entry
    in _STATIC_REASON_CODES is dead code that misleads readers."""

    @unittest.skip("Removed in PLAN Step 3 (hardcoded _STATIC_REASON_CODES deleted)")
    def test_static_reason_codes_does_not_contain_run_id(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "save_artifact_bundle")
        self.assertGreater(len(src), 0, "save_artifact_bundle not found")

        # Extract the _STATIC_REASON_CODES dict literal only (between { … })
        match = re.search(r"_STATIC_REASON_CODES.*?=\s*(\{.*?\})", src, re.DOTALL)
        self.assertIsNotNone(match, "_STATIC_REASON_CODES dict not found in save_artifact_bundle")
        dict_src = match.group(1)  # type: ignore[union-attr]
        self.assertNotIn(
            '"run_id"',
            dict_src,
            '_STATIC_REASON_CODES contains "run_id" which was removed from features (R67/R73)',
        )


if __name__ == "__main__":
    unittest.main()
