"""tests/test_review_risks_round60.py
=====================================
Minimal reproducible guardrail tests for Round 6 review findings (R74–R82).

Tests-only — no production code changes.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_FEATURES_PATH = _REPO_ROOT / "trainer" / "features.py"
_SCORER_PATH = _REPO_ROOT / "trainer" / "scorer.py"

_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_FEATURES_SRC = _FEATURES_PATH.read_text(encoding="utf-8")
_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")

_TRAINER_TREE = ast.parse(_TRAINER_SRC)
_FEATURES_TREE = ast.parse(_FEATURES_SRC)


def _get_func_src(tree: ast.Module, src: str, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _extract_str_list_from_assign(src: str, assign_name: str) -> list[str]:
    """Parse a top-level `NAME = [ ... ]` and return string elements."""
    tree = ast.parse(src)
    for node in tree.body:
        target_name = None
        value_node = None
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                target_name = node.target.id
                value_node = node.value

        if target_name != assign_name or not isinstance(value_node, ast.List):
            continue

        vals: list[str] = []
        for elt in value_node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                vals.append(elt.value)
        return vals
    return []


class TestR74ProfileMissingShouldRemainNull(unittest.TestCase):
    """R74: Missing profile should not be forced to 0."""

    def test_join_function_does_not_fill_profile_nan_with_zero(self):
        src = _get_func_src(_FEATURES_TREE, _FEATURES_SRC, "join_player_profile")
        self.assertGreater(len(src), 0, "join_player_profile not found")
        self.assertNotIn(
            ".fillna(0.0)",
            src,
            "join_player_profile should keep NaN for unmatched profile rows (R74)",
        )

    def test_process_chunk_does_not_fillna_zero_all_features(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "process_chunk")
        self.assertGreater(len(src), 0, "process_chunk not found")
        self.assertNotIn(
            "labeled[ALL_FEATURE_COLS] = labeled[ALL_FEATURE_COLS].fillna(0)",
            src,
            "process_chunk should not force-fill profile feature NaN to 0 (R74)",
        )


class TestR75CanonicalIdTypeAlignment(unittest.TestCase):
    """R75: canonical_id dtype should be aligned before merge_asof."""

    def test_join_casts_both_sides_canonical_id_to_str(self):
        src = _get_func_src(_FEATURES_TREE, _FEATURES_SRC, "join_player_profile")
        self.assertRegex(
            src,
            r'bets_work\["canonical_id"\]\s*=\s*bets_work\["canonical_id"\]\.astype\(\s*str\s*\)',
            "bets side canonical_id should be cast to str before merge_asof (R75)",
        )
        self.assertRegex(
            src,
            r'profile_work\["canonical_id"\]\s*=\s*profile_work\["canonical_id"\]\.astype\(\s*str\s*\)',
            "profile side canonical_id should be cast to str before merge_asof (R75)",
        )


class TestR76ArtifactMetadataForProfileFeatures(unittest.TestCase):
    """R76: profile features should have explicit track/reason-code semantics."""

    def test_feature_list_labels_profile_track(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "save_artifact_bundle")
        self.assertIn(
            "PROFILE_FEATURE_COLS",
            src,
            "save_artifact_bundle should classify profile features explicitly (R76)",
        )
        self.assertIn(
            '"profile"',
            src.lower(),
            'feature_list track labels should include "profile" (R76)',
        )

    def test_reason_code_map_uses_profile_prefix(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "save_artifact_bundle")
        self.assertIn(
            "PROFILE_",
            src,
            "reason_code_map fallback should use PROFILE_ prefix for profile features (R76)",
        )


class TestR77CacheKeyIncludesProfileState(unittest.TestCase):
    """R77: chunk cache should invalidate on profile snapshot changes."""

    def test_chunk_cache_key_or_process_chunk_references_profile(self):
        key_src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "_chunk_cache_key")
        proc_src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "process_chunk")
        self.assertTrue(
            ("profile" in key_src.lower()) or re.search(r"_chunk_cache_key\([^)]*profile", proc_src),
            "_chunk cache key must include profile-related state (R77)",
        )


class TestR78ProfileFeatureColsCoverage(unittest.TestCase):
    """R78: PROFILE_FEATURE_COLS should cover spec-listed Phase 1 columns."""

    def test_profile_feature_cols_include_round6_missing_columns(self):
        cols = set(_extract_str_list_from_assign(_FEATURES_SRC, "PROFILE_FEATURE_COLS"))
        self.assertTrue(cols, "PROFILE_FEATURE_COLS not found or empty")
        missing_expected = {
            "sessions_365d",
            "active_days_365d",
            "turnover_sum_365d",
            "player_win_sum_90d",
            "player_win_sum_365d",
            "theo_win_sum_180d",
            "num_bets_sum_180d",
            "num_games_with_wager_sum_180d",
            "distinct_table_cnt_90d",
            "distinct_gaming_area_cnt_30d",
            "top_table_share_90d",
        }
        self.assertTrue(
            missing_expected.issubset(cols),
            f"PROFILE_FEATURE_COLS missing spec-listed columns: {sorted(missing_expected - cols)} (R78)",
        )


class TestR79ScorerProfileParity(unittest.TestCase):
    """R79: scorer should implement/declare profile PIT parity."""

    def test_scorer_has_profile_join_or_profile_feature_import(self):
        has_join = "join_player_profile" in _SCORER_SRC
        has_cols = "PROFILE_FEATURE_COLS" in _SCORER_SRC
        has_profile_table = "player_profile" in _SCORER_SRC
        self.assertTrue(
            has_join or has_cols or has_profile_table,
            "scorer.py lacks player_profile PIT parity signals (R79)",
        )


class TestR80NonratedProfileFeatureExclusion(unittest.TestCase):
    """R80: nonrated model should exclude profile-only features."""

    def test_train_dual_model_nonrated_excludes_profile_features(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "train_dual_model")
        self.assertRegex(
            src,
            r'name\s*==\s*"nonrated".*PROFILE_FEATURE_COLS',
            "train_dual_model should exclude PROFILE_FEATURE_COLS for nonrated model (R80)",
        )


class TestR81LocalParquetBranchDeadCode(unittest.TestCase):
    """R81: remove always-false parent.parent.parent.exists condition."""

    def test_no_parent_parent_parent_exists_condition(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "load_player_profile")
        self.assertGreater(len(src), 0, "load_player_profile not found")
        self.assertNotIn(
            "parent.parent.parent.exists()",
            src,
            "load_player_profile contains dead-code branch condition (R81)",
        )


class TestR82LoadProfileMemoryGuard(unittest.TestCase):
    """R82: profile loading should be filterable by canonical_id."""

    def test_load_profile_filters_by_canonical_id(self):
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "load_player_profile")
        has_cid_filter = re.search(r"canonical_id\s+IN|canonical_id.*filters", src)
        has_param = "canonical_ids" in src
        self.assertTrue(
            bool(has_cid_filter) or has_param,
            "load_player_profile should include canonical_id filtering/memory guard (R82)",
        )


if __name__ == "__main__":
    unittest.main()
