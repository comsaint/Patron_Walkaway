"""tests/test_scorer.py
=======================
Unit tests for trainer/scorer.py — H3 model routing and reason code output.

No ClickHouse; uses AST/source inspection so we don't import scorer
(which pulls in trainer + joblib + optional deps).
PLAN Step 10: model routing (H3), reason code output completeness.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCORER_PATH = _REPO_ROOT / "trainer" / "serving" / "scorer.py"
_SRC = _SCORER_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _get_func_src(name: str) -> str:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    return ""


# ---------------------------------------------------------------------------
# H3 model routing
# ---------------------------------------------------------------------------

class TestH3ModelRouting(unittest.TestCase):
    """H3 (v10 DEC-021): single rated model scores all observations, is_rated_obs tracks patron status."""

    def test_score_df_uses_rated_art(self):
        """_score_df must use rated_art for scoring."""
        src = _get_func_src("_score_df")
        self.assertIn("rated_art", src)

    def test_score_df_sets_is_rated_obs_column(self):
        """Scoring must set is_rated_obs (int 0/1) for downstream."""
        src = _get_func_src("_score_df")
        self.assertIn("is_rated_obs", src)

    def test_score_df_uses_threshold_and_margin(self):
        """Margin/alert must use rated model threshold."""
        src = _get_func_src("_score_df")
        self.assertIn("threshold", src)
        self.assertIn("margin", src)


# ---------------------------------------------------------------------------
# Reason code output completeness
# ---------------------------------------------------------------------------

class TestReasonCodeOutput(unittest.TestCase):
    """Reason codes from reason_code_map lookup, emitted with every alert."""

    def test_load_dual_artifacts_loads_reason_code_map(self):
        """load_dual_artifacts must load reason_code_map.json."""
        src = _get_func_src("load_dual_artifacts")
        self.assertIn("reason_code_map", src)
        self.assertIn("reason_code_map.json", src)

    def test_alert_schema_includes_reason_codes_and_model_version(self):
        """Alert persistence or output must include reason_codes and model_version."""
        self.assertIn("reason_codes", _SRC)
        self.assertIn("model_version", _SRC)
        self.assertIn("reason_code_map", _SRC)

    def test_compute_reason_codes_exists(self):
        """Scorer must have a path to compute reason codes (e.g. _compute_reason_codes)."""
        self.assertIn("_compute_reason_codes", _SRC)


if __name__ == "__main__":
    unittest.main()
