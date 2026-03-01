"""tests/test_review_risks_round26.py
=====================================
Guardrail tests for Round 26 review findings (R36-R40).

Constraint in this round: tests-only (no production code changes). Therefore
known issues are captured with ``unittest.expectedFailure`` and should be
converted to normal passing assertions once implementation fixes are applied.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_BACKTESTER_PATH = _REPO_ROOT / "trainer" / "backtester.py"
_SCORER_PATH = _REPO_ROOT / "trainer" / "scorer.py"

_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_BACKTESTER_SRC = _BACKTESTER_PATH.read_text(encoding="utf-8")
_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")

_TRAINER_TREE = ast.parse(_TRAINER_SRC)
_BACKTESTER_TREE = ast.parse(_BACKTESTER_SRC)
_SCORER_TREE = ast.parse(_SCORER_SRC)


def _get_func_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


class TestReviewRisksRound26(unittest.TestCase):
    def test_r36_h3_routing_must_not_depend_on_missing_casino_player_id_column(self):
        """R36: H3 rated routing should use canonical_id mapping, not a missing column."""
        trainer_src = ast.get_source_segment(
            _TRAINER_SRC, _get_func_node(_TRAINER_TREE, "process_chunk")
        ) or ""
        backtester_src = ast.get_source_segment(
            _BACKTESTER_SRC, _get_func_node(_BACKTESTER_TREE, "backtest")
        ) or ""
        scorer_src = ast.get_source_segment(
            _SCORER_SRC, _get_func_node(_SCORER_TREE, "score_once")
        ) or ""

        # canonical_map from identity.build_canonical_mapping_from_df only has
        # [player_id, canonical_id]. Routing should not check for a missing
        # "casino_player_id" column.
        self.assertNotIn('"casino_player_id" in canonical_map.columns', trainer_src)
        self.assertNotIn('"casino_player_id" in canonical_map.columns', backtester_src)
        self.assertNotIn('"casino_player_id" in canonical_map.columns', scorer_src)

        # Guardrail: routing should derive rated ids from canonical_id directly.
        self.assertIn('canonical_map["canonical_id"]', trainer_src)
        self.assertIn('canonical_map["canonical_id"]', backtester_src)
        self.assertIn('canonical_map["canonical_id"]', scorer_src)

    def test_r37_apply_dq_must_drop_placeholder_player_id(self):
        """R37: trainer apply_dq should enforce E4/F1 placeholder player filter."""
        src = ast.get_source_segment(
            _TRAINER_SRC, _get_func_node(_TRAINER_TREE, "apply_dq")
        ) or ""
        self.assertIn(
            "PLACEHOLDER_PLAYER_ID",
            src,
            msg="apply_dq should reference PLACEHOLDER_PLAYER_ID when filtering bets.",
        )
        self.assertIn(
            'bets["player_id"] != PLACEHOLDER_PLAYER_ID',
            src,
            msg="apply_dq should drop placeholder player rows per SSOT E4/F1.",
        )

    def test_r38_trainer_session_query_must_filter_is_manual_zero(self):
        """R38: trainer load_clickhouse_data session query should include is_manual=0."""
        src = ast.get_source_segment(
            _TRAINER_SRC, _get_func_node(_TRAINER_TREE, "load_clickhouse_data")
        ) or ""
        self.assertIn(
            "AND is_manual = 0",
            src,
            msg="trainer session query should filter manual sessions for parity with scorer.",
        )

    def test_r39_apply_dq_session_dedup_needs_etl_tiebreaker(self):
        """R39: apply_dq dedup should use __etl_insert_Dtm as FND-01 tiebreaker."""
        src = ast.get_source_segment(
            _TRAINER_SRC, _get_func_node(_TRAINER_TREE, "apply_dq")
        ) or ""
        self.assertIn(
            "__etl_insert_Dtm",
            src,
            msg=(
                "apply_dq should include __etl_insert_Dtm in session dedup sort keys "
                "to match FND-01 tie-break rule."
            ),
        )

    def test_r40_scorer_config_import_should_support_package_execution(self):
        """R40: scorer should support both script and package import paths for config."""
        self.assertIn(
            "except ModuleNotFoundError",
            _SCORER_SRC,
            msg="scorer should have ModuleNotFoundError fallback for config import.",
        )
        self.assertIn(
            "import trainer.config as config",
            _SCORER_SRC,
            msg="scorer should fallback to trainer.config when run as package.",
        )


if __name__ == "__main__":
    unittest.main()

