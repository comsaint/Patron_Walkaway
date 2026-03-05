"""tests/test_review_risks_round38.py
=====================================
Guardrail tests for Round 38 review findings (R55-R62).

Round 40: production code fixes applied for all 8 risks; decorators removed.
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


def _get_func_src(tree: ast.Module, full_src: str, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(full_src, node) or ""
    raise AssertionError(f"function {name!r} not found")


class TestReviewRisksRound38(unittest.TestCase):
    def test_r55_validator_legacy_bet_fetch_helper_should_be_removed(self):
        """R55: remove dead fetch_bets_for_players helper to avoid accidental reuse."""
        self.assertNotIn("def fetch_bets_for_players(", _VALIDATOR_SRC)

    def test_r56_api_score_should_use_cached_explainers(self):
        """R56: /score should consume rated/nonrated explainer cache instead of rebuilding."""
        score_src = _get_func_src(_API_TREE, _API_SRC, "score")
        shap_src = _get_func_src(_API_TREE, _API_SRC, "_compute_shap_reason_codes_batch")
        self.assertTrue(
            ("rated_explainer" in score_src) or ("nonrated_explainer" in score_src),
            msg="score() does not reference cached explainers",
        )
        self.assertNotIn(
            "TreeExplainer(model)",
            shap_src,
            msg="_compute_shap_reason_codes_batch still rebuilds TreeExplainer",
        )

    def test_r57_api_get_artifacts_should_read_version_inside_lock(self):
        """R57: version read and cache reload should be in the same lock scope."""
        src = _get_func_src(_API_TREE, _API_SRC, "_get_artifacts")
        idx_lock = src.find("with _artifacts_lock")
        idx_read = src.find("version_path.read_text")
        self.assertGreaterEqual(idx_lock, 0)
        self.assertGreaterEqual(idx_read, 0)
        self.assertLess(
            idx_lock,
            idx_read,
            msg="version_path.read_text occurs before lock acquisition",
        )

    def test_r58_api_artifact_loading_should_avoid_double_io_for_pkl(self):
        """R58: _load_artifacts should reuse already-read bytes when loading pickles."""
        src = _get_func_src(_API_TREE, _API_SRC, "_load_artifacts")
        self.assertIn("read_bytes()", src)
        self.assertIn("io.BytesIO", src)

    def test_r59_validator_should_handle_null_session_end_safely(self):
        """R59: validator should have explicit NULL/NaT guard for session_end_dtm."""
        fetch_src = _get_func_src(_VALIDATOR_TREE, _VALIDATOR_SRC, "fetch_sessions_by_canonical_id")
        row_src = _get_func_src(_VALIDATOR_TREE, _VALIDATOR_SRC, "validate_alert_row")
        self.assertTrue(
            any(
                token in fetch_src
                for token in ("COALESCE(session_end_dtm", "fillna(", "pd.notna(row[\"session_end_dtm\"])")
            ),
            msg="fetch_sessions_by_canonical_id has no explicit NULL session_end handling",
        )
        self.assertTrue(
            any(token in row_src for token in ("pd.isna(session_end)", "session_end is None")),
            msg="validate_alert_row has no NaT guard before session_end arithmetic",
        )

    def test_r60_validator_bet_query_should_explicitly_filter_null_payout_time(self):
        """R60: validator t_bet query should include explicit payout_complete_dtm IS NOT NULL."""
        src = _get_func_src(_VALIDATOR_TREE, _VALIDATOR_SRC, "fetch_bets_by_canonical_id")
        self.assertIn("payout_complete_dtm IS NOT NULL", src)

    def test_r61_api_model_info_should_not_reload_model_file_each_request(self):
        """R61: model_info should consume cached metrics instead of joblib.load per request."""
        src = _get_func_src(_API_TREE, _API_SRC, "model_info")
        self.assertNotIn("joblib.load", src)

    def test_r62_scorer_track_a_cutoff_should_align_timezone_with_bets(self):
        """R62: fetch_recent_data must normalize bets payout_complete_dtm to tz-aware
        HK time so that Track-A cutoff_time (now_hk, already tz-aware per R51) is
        consistent with the EntitySet time_index used by Featuretools.
        Checking score_once directly would conflict with R51 which prohibits
        replace(tzinfo=None) inside that function."""
        src = _get_func_src(_SCORER_TREE, _SCORER_SRC, "fetch_recent_data")
        self.assertTrue(
            "tz_localize(HK_TZ)" in src or "tz_convert(HK_TZ)" in src,
            msg="fetch_recent_data must normalize payout_complete_dtm to tz-aware HK time",
        )

    def test_r48_api_artifact_loading_should_include_integrity_check_signal(self):
        """R48: artifact loading should include integrity verification guardrails."""
        src = _get_func_src(_API_TREE, _API_SRC, "_load_artifacts")
        # Minimal signal: at least one common integrity-check keyword should exist.
        self.assertTrue(
            any(k in src for k in ("sha256", "hashlib", "signature", "hmac")),
            msg="artifact loading lacks visible integrity-check signal",
        )

    def test_r49_api_should_cache_tree_explainer_objects(self):
        """R49: /score should not rebuild TreeExplainer on every request (rated model cache)."""
        self.assertIn("rated_explainer", _API_SRC)

    def test_r50_api_and_scorer_shap_mode_should_be_consistent(self):
        """R50: API and scorer should use consistent SHAP perturbation behavior."""
        api_func_src = _get_func_src(_API_TREE, _API_SRC, "_compute_shap_reason_codes_batch")
        scorer_func_src = _get_func_src(_SCORER_TREE, _SCORER_SRC, "_compute_reason_codes")
        # Guardrail: avoid explicit perturbation mode divergence between endpoints.
        self.assertNotIn("feature_perturbation=", api_func_src)
        self.assertNotIn("feature_perturbation=", scorer_func_src)

    def test_r51_scorer_track_a_cutoff_time_must_not_strip_timezone(self):
        """R51: scorer Track-A cutoff_time should keep timezone semantics."""
        src = _get_func_src(_SCORER_TREE, _SCORER_SRC, "score_once")
        self.assertNotIn("replace(tzinfo=None)", src)

    def test_r52_api_get_artifacts_should_be_lock_protected(self):
        """R52: artifact cache reads/writes should be protected by a lock."""
        self.assertIn("threading.Lock()", _API_SRC)
        get_artifacts_src = _get_func_src(_API_TREE, _API_SRC, "_get_artifacts")
        self.assertIn("with _artifacts_lock", get_artifacts_src)

    def test_r54_api_score_should_guard_empty_feature_list_before_predict(self):
        """R54: /score should reject empty feature_list artifact before predict_proba."""
        score_src = _get_func_src(_API_TREE, _API_SRC, "score")
        self.assertIn("feature_list is empty", score_src)


if __name__ == "__main__":
    unittest.main()
