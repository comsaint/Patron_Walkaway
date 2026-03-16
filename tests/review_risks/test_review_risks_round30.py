"""tests/test_review_risks_round30.py
=====================================
Guardrail tests for Round 30 review findings (R41-R45).

Production code fixes were applied in Round 32; ``@unittest.expectedFailure``
decorators have been removed and all five tests now pass as regular assertions.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_VALIDATOR_PATH = _REPO_ROOT / "trainer" / "serving" / "validator.py"
_SCORER_PATH = _REPO_ROOT / "trainer" / "serving" / "scorer.py"
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"

_VALIDATOR_SRC = _VALIDATOR_PATH.read_text(encoding="utf-8")
_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")

_VALIDATOR_TREE = ast.parse(_VALIDATOR_SRC)
_SCORER_TREE = ast.parse(_SCORER_SRC)
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_func_node(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


class TestReviewRisksRound30(unittest.TestCase):
    def test_r41_validator_fetch_bets_must_not_silently_swallow_db_errors(self):
        """R41: DB query failures should not return empty cache silently."""
        src = ast.get_source_segment(
            _VALIDATOR_SRC, _get_func_node(_VALIDATOR_TREE, "fetch_bets_by_canonical_id")
        ) or ""

        # Guardrail: an exception from query_df/get_clickhouse_client should surface
        # to caller, so validation does not misclassify alerts as MISS due to empty data.
        self.assertNotIn(
            "except Exception:\n        return {}",
            src,
            msg=(
                "fetch_bets_by_canonical_id currently swallows DB exceptions and "
                "returns empty data; this can cause false MISS classifications."
            ),
        )

    def test_r42_validator_session_cache_should_be_canonical_id_based(self):
        """R42: session lookup should follow canonical_id, not only player_id."""
        src = ast.get_source_segment(
            _VALIDATOR_SRC, _get_func_node(_VALIDATOR_TREE, "validate_alert_row")
        ) or ""
        self.assertIn(
            "session_cache.get(canonical_id",
            src,
            msg=(
                "validate_alert_row should look up sessions by canonical_id "
                "to support player card swaps."
            ),
        )

    def test_r43_scorer_session_query_contains_etl_insert_tiebreaker(self):
        """R43: scorer FND-01 dedup should include __etl_insert_Dtm tiebreaker."""
        src = ast.get_source_segment(
            _SCORER_SRC, _get_func_node(_SCORER_TREE, "fetch_recent_data")
        ) or ""
        self.assertIn(
            "__etl_insert_Dtm",
            src,
            msg=(
                "scorer session_query should include __etl_insert_Dtm in ORDER BY "
                "to match FND-01 tie-break semantics."
            ),
        )

    def test_r44_validator_fetch_bets_should_chunk_large_player_id_lists(self):
        """R44: large IN lists should be chunked to avoid oversized ClickHouse query."""
        src = ast.get_source_segment(
            _VALIDATOR_SRC, _get_func_node(_VALIDATOR_TREE, "fetch_bets_by_canonical_id")
        ) or ""
        # Minimal structural signal for chunking implementation.
        self.assertIn(
            "for i in range(0, len(all_pids)",
            src,
            msg=(
                "fetch_bets_by_canonical_id should batch all_pids into chunks "
                "instead of issuing one giant IN query."
            ),
        )

    def test_r45_pipeline_must_integrate_track_llm_duckdb_calls(self):
        """R45: trainer/scorer pipelines should include Track LLM DuckDB execution."""
        self.assertIn(
            "compute_track_llm_features",
            _TRAINER_SRC,
            msg="trainer.py should invoke compute_track_llm_features for production pipeline.",
        )
        self.assertIn(
            "compute_track_llm_features",
            _SCORER_SRC,
            msg="scorer.py should invoke compute_track_llm_features for online parity.",
        )


if __name__ == "__main__":
    unittest.main()
