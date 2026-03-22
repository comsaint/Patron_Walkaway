"""tests/test_review_risks_round40.py
=====================================
Minimal reproducible guardrail tests for Round 3 review findings (R63-R67).

Scope in this round: tests-only. These tests are intended to surface the
current gaps in `trainer/backtester.py` and `trainer/trainer.py`.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import sys
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FEATURE_SPEC_PATH = _REPO_ROOT / "trainer" / "feature_spec" / "features_candidates.yaml"
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_BACKTESTER_PATH = _REPO_ROOT / "trainer" / "backtester.py"

_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_BACKTESTER_SRC = _BACKTESTER_PATH.read_text(encoding="utf-8")

_TRAINER_TREE = ast.parse(_TRAINER_SRC)
_BACKTESTER_TREE = ast.parse(_BACKTESTER_SRC)


def _features_mod():
    """Import trainer.features (repo root must be on path). Used for R67 YAML-driven guard."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    return importlib.import_module("trainer.features")


def _get_func_src(tree: ast.Module, src: str, name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _get_assign_src(tree: ast.Module, src: str, name: str) -> str:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return ast.get_source_segment(src, node) or ""
    return ""


class TestReviewRisksRound40(unittest.TestCase):
    def test_r63_backtester_optuna_objective_does_not_use_g1_constraints(self):
        """R63: DEC-010 requires F-beta objective without G1 precision/alert gates."""
        src = _get_func_src(_BACKTESTER_TREE, _BACKTESTER_SRC, "run_optuna_threshold_search")
        self.assertNotIn("G1_PRECISION_MIN", src)
        self.assertNotIn("G1_ALERT_VOLUME_MIN_PER_HOUR", src)

    def test_r63_backtester_does_not_import_deprecated_g1_constants(self):
        """R63: backtester should not import deprecated G1 constants from config."""
        self.assertNotIn("G1_PRECISION_MIN =", _BACKTESTER_SRC)
        self.assertNotIn("G1_ALERT_VOLUME_MIN_PER_HOUR =", _BACKTESTER_SRC)

    def test_r64_apply_dq_has_sessions_only_guard_for_empty_bets(self):
        """R64: apply_dq should guard `bets.empty` to avoid KeyError in local parquet path."""
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "apply_dq")
        self.assertIn("if bets.empty", src)

    def test_r65_train_threshold_selection_uses_precision_recall_curve(self):
        """R65: threshold selection uses shared PR-curve helper (vectorized scan inside it)."""
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "_train_one_model")
        self.assertIn("pick_threshold_dec026", src)
        self.assertNotIn("for t in thresholds", src)
        ts_path = _REPO_ROOT / "trainer" / "training" / "threshold_selection.py"
        self.assertIn("precision_recall_curve", ts_path.read_text(encoding="utf-8"))

    def test_r66_process_chunk_actually_uses_chunk_cache_key(self):
        """R66: TRN-07 cache key function should be invoked by process_chunk."""
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "process_chunk")
        self.assertIn("_chunk_cache_key(", src)

    def test_r67_run_id_not_used_as_model_feature(self):
        """R67: run_id must not be a model feature (YAML SSOT); trainer must not redefine
        TRACK_B_FEATURE_COLS (Round 141 Review P0)."""
        # 1) Training candidate list from YAML must not include run_id (R67 guardrail).
        features = _features_mod()
        spec = features.load_feature_spec(_FEATURE_SPEC_PATH)
        candidates = features.get_all_candidate_feature_ids(spec, screening_only=True)
        self.assertNotIn(
            "run_id",
            candidates,
            msg="run_id must not be in YAML-driven training candidates (sample weighting only).",
        )
        # 2) Trainer must not re-introduce hardcoded TRACK_B_FEATURE_COLS (feat-consolidation Step 3).
        self.assertNotIn(
            "TRACK_B_FEATURE_COLS =",
            _TRAINER_SRC,
            msg="trainer must not define TRACK_B_FEATURE_COLS; use YAML get_candidate_feature_ids.",
        )

    def test_r141_process_chunk_fallback_when_feature_spec_is_none(self):
        """Round 141 Review P1: when feature_spec is None, process_chunk must use
        PROFILE_FEATURE_COLS fallback so it does not crash; guard against removing the branch."""
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "process_chunk")
        self.assertIn(
            "PROFILE_FEATURE_COLS",
            src,
            msg="process_chunk must reference PROFILE_FEATURE_COLS for feature_spec=None fallback.",
        )
        self.assertIn(
            "else list(PROFILE_FEATURE_COLS)",
            src,
            msg="process_chunk must set _all_candidate_cols from PROFILE_FEATURE_COLS when feature_spec is falsy.",
        )
        self.assertIn(
            "else set(PROFILE_FEATURE_COLS)",
            src,
            msg="process_chunk must set _yaml_profile_set from PROFILE_FEATURE_COLS when feature_spec is falsy.",
        )


if __name__ == "__main__":
    unittest.main()
