"""tests/test_scorer_review_risks_round22.py
================================================
Guardrail tests for Round 22 review findings (R31-R35).

Constraint in this round: tests-only (no production code changes).  Therefore
known issues are captured with ``unittest.expectedFailure`` and should be
converted to normal passing assertions once implementation fixes are applied.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import sys
import unittest


def _features_mod():
    _root = pathlib.Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    return importlib.import_module("trainer.features")


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FEATURE_SPEC_PATH = _REPO_ROOT / "trainer" / "feature_spec" / "features_candidates.yaml"
_SCORER_PATH = _REPO_ROOT / "trainer" / "scorer.py"
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"

_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")

_SCORER_TREE = ast.parse(_SCORER_SRC)
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_func_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _get_list_constant(tree: ast.Module, var_name: str) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == var_name:
                    if isinstance(node.value, ast.List):
                        out: list[str] = []
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                out.append(elt.value)
                        return out
        if isinstance(node, ast.AnnAssign):
            tgt = node.target
            if isinstance(tgt, ast.Name) and tgt.id == var_name:
                if isinstance(node.value, ast.List):
                    out = []
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            out.append(elt.value)
                    return out
    raise AssertionError(f"list assignment {var_name!r} not found")


class TestScorerReviewRisksRound22(unittest.TestCase):
    def test_r31_feature_list_json_supports_list_of_dicts(self):
        """R31: scorer should normalize feature_list.json list-of-dicts to List[str]."""
        func = _get_func_node(_SCORER_TREE, "load_dual_artifacts")
        src = ast.get_source_segment(_SCORER_SRC, func) or ""

        # Guardrail:
        # trainer writes feature_list.json as [{"name": "...", "track": "..."}].
        # scorer must normalize that format to pure feature-name strings.
        self.assertIn(
            'isinstance(entry, dict)',
            src,
            msg="load_dual_artifacts should branch on dict entries from feature_list.json.",
        )
        self.assertIn(
            'entry["name"]',
            src,
            msg="load_dual_artifacts should extract feature name from dict entries.",
        )

    def test_r32_online_features_do_not_use_session_delayed_duration_features(self):
        """R32 (updated by R2300): session_duration_min and bets_per_minute are computed
        dynamically by the scorer for train-serve parity; they must NOT appear in the
        training feature candidate list (YAML SSOT, feat-consolidation) to avoid
        double-counting, but MUST be computed inside build_features_for_scoring (R2300).
        Depends on repo spec features_candidates.yaml as SSOT (Round 141 Review P2)."""
        self.assertTrue(
            _FEATURE_SPEC_PATH.exists(),
            "Spec YAML required for R32: trainer/feature_spec/features_candidates.yaml",
        )
        features = _features_mod()
        spec = features.load_feature_spec(_FEATURE_SPEC_PATH)
        training_candidates = features.get_all_candidate_feature_ids(spec, screening_only=True)
        self.assertNotIn(
            "minutes_since_session_start",
            training_candidates,
            msg="Training candidate list (YAML) should not include session-delayed duration features.",
        )
        self.assertNotIn(
            "bets_per_minute",
            training_candidates,
            msg="Training candidate list (YAML) should not include bets_per_minute as a static constant.",
        )

        scorer_func = _get_func_node(_SCORER_TREE, "build_features_for_scoring")
        scorer_src = ast.get_source_segment(_SCORER_SRC, scorer_func) or ""
        # R2300: scorer must compute these for train-serve parity (docstring step 3).
        self.assertIn(
            'bets_df["session_duration_min"] =',
            scorer_src,
            msg="scorer online feature builder must compute session_duration_min (R2300 parity).",
        )
        self.assertIn(
            'bets_df["bets_per_minute"] =',
            scorer_src,
            msg="scorer online feature builder must compute bets_per_minute (R2300 parity).",
        )

    def test_r33_session_timestamps_convert_to_hk_before_tz_strip(self):
        """R33: session_start/end timezone normalization should tz_convert(HK_TZ) first."""
        func = _get_func_node(_SCORER_TREE, "build_features_for_scoring")
        src = ast.get_source_segment(_SCORER_SRC, func) or ""

        self.assertIn(
            'bets_df[col] = bets_df[col].dt.tz_convert(HK_TZ).dt.tz_localize(None)',
            src,
            msg=(
                "build_features_for_scoring should convert session timestamps to HK "
                "before stripping tz info."
            ),
        )

    def test_r34_append_alerts_s_helper_uses_pd_isna(self):
        """R34: append_alerts _s helper should handle pd.NA/pd.NaT via pd.isna."""
        func = _get_func_node(_SCORER_TREE, "append_alerts")
        src = ast.get_source_segment(_SCORER_SRC, func) or ""

        self.assertIn(
            "pd.isna(v)",
            src,
            msg="_s helper should use pd.isna(v) to handle pd.NA/pd.NaT safely.",
        )

    def test_r35_fetch_recent_data_parameterizes_bet_and_session_avail_cutoffs(self):
        """R35: fetch_recent_data should parameterize bet_avail/sess_avail cutoffs."""
        func = _get_func_node(_SCORER_TREE, "fetch_recent_data")
        src = ast.get_source_segment(_SCORER_SRC, func) or ""

        self.assertIn(
            "%(bet_avail)s",
            src,
            msg="fetch_recent_data should parameterize bet_avail in SQL.",
        )
        self.assertIn(
            "%(sess_avail)s",
            src,
            msg="fetch_recent_data should parameterize sess_avail in SQL.",
        )
        self.assertNotIn(
            ".isoformat()",
            src,
            msg="fetch_recent_data SQL should avoid inline .isoformat() f-string interpolation.",
        )


if __name__ == "__main__":
    unittest.main()

