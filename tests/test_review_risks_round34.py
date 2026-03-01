"""tests/test_review_risks_round34.py
=====================================
Guardrail tests for Round 34 review findings (R46-R54).

Round 36: production-code fixes applied for all 9 risks.
``unittest.expectedFailure`` decorators removed — tests now pass normally.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VALIDATOR_PATH = _REPO_ROOT / "trainer" / "validator.py"
_SCORER_PATH = _REPO_ROOT / "trainer" / "scorer.py"
_API_PATH = _REPO_ROOT / "trainer" / "api_server.py"

_VALIDATOR_SRC = _VALIDATOR_PATH.read_text(encoding="utf-8")
_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")
_API_SRC = _API_PATH.read_text(encoding="utf-8")

_VALIDATOR_TREE = ast.parse(_VALIDATOR_SRC)
_SCORER_TREE = ast.parse(_SCORER_SRC)
_API_TREE = ast.parse(_API_SRC)


def _get_func_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _all_called_names(func_node: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


class TestReviewRisksRound34(unittest.TestCase):
    def test_r46_validator_sessions_query_must_apply_fnd01_and_dq_filters(self):
        """R46: session fetch by canonical_id should include FND-01 + DQ guardrails."""
        src = ast.get_source_segment(
            _VALIDATOR_SRC, _get_func_node(_VALIDATOR_TREE, "fetch_sessions_by_canonical_id")
        ) or ""
        self.assertIn("ROW_NUMBER() OVER", src)
        self.assertIn("is_deleted = 0", src)
        self.assertIn("is_canceled = 0", src)
        self.assertIn("is_manual = 0", src)

    def test_r47_validator_bets_query_must_apply_final_and_placeholder_filter(self):
        """R47: t_bet fetch should include FINAL and player_id placeholder exclusion."""
        src = ast.get_source_segment(
            _VALIDATOR_SRC, _get_func_node(_VALIDATOR_TREE, "fetch_bets_by_canonical_id")
        ) or ""
        self.assertIn("FINAL", src)
        self.assertIn("player_id !=", src)

    def test_r48_api_artifact_loading_should_include_integrity_check_signal(self):
        """R48: artifact loading should include integrity verification guardrails."""
        src = ast.get_source_segment(
            _API_SRC, _get_func_node(_API_TREE, "_load_artifacts")
        ) or ""
        # Minimal signal: at least one common integrity-check keyword should exist.
        self.assertTrue(
            any(k in src for k in ("sha256", "hashlib", "signature", "hmac")),
            msg="artifact loading lacks visible integrity-check signal",
        )

    def test_r49_api_should_cache_tree_explainer_objects(self):
        """R49: /score should not rebuild TreeExplainer on every request."""
        self.assertIn("rated_explainer", _API_SRC)
        self.assertIn("nonrated_explainer", _API_SRC)

    def test_r50_api_and_scorer_shap_mode_should_be_consistent(self):
        """R50: API and scorer should use consistent SHAP perturbation behavior."""
        api_func_src = ast.get_source_segment(
            _API_SRC, _get_func_node(_API_TREE, "_compute_shap_reason_codes_batch")
        ) or ""
        scorer_func_src = ast.get_source_segment(
            _SCORER_SRC, _get_func_node(_SCORER_TREE, "_compute_reason_codes")
        ) or ""
        # Guardrail: avoid explicit perturbation mode divergence between endpoints.
        self.assertNotIn("feature_perturbation=", api_func_src)
        self.assertNotIn("feature_perturbation=", scorer_func_src)

    def test_r51_scorer_track_a_cutoff_time_must_not_strip_timezone(self):
        """R51: scorer Track-A cutoff_time should keep timezone semantics."""
        src = ast.get_source_segment(
            _SCORER_SRC, _get_func_node(_SCORER_TREE, "score_once")
        ) or ""
        self.assertNotIn("replace(tzinfo=None)", src)

    def test_r52_api_get_artifacts_should_be_lock_protected(self):
        """R52: artifact cache reads/writes should be protected by a lock."""
        self.assertIn("threading.Lock()", _API_SRC)
        get_artifacts_src = ast.get_source_segment(
            _API_SRC, _get_func_node(_API_TREE, "_get_artifacts")
        ) or ""
        self.assertIn("with _artifacts_lock", get_artifacts_src)

    def test_r53_validator_deprecated_session_fetch_helper_should_be_removed(self):
        """R53: unused legacy fetch_sessions_for_players should be removed."""
        self.assertNotIn("def fetch_sessions_for_players(", _VALIDATOR_SRC)

    def test_r54_api_score_should_guard_empty_feature_list_before_predict(self):
        """R54: /score should reject empty feature_list artifact before predict_proba."""
        score_src = ast.get_source_segment(
            _API_SRC, _get_func_node(_API_TREE, "score")
        ) or ""
        self.assertIn("feature_list is empty", score_src)


if __name__ == "__main__":
    unittest.main()
