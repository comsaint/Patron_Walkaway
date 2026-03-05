"""tests/test_dq_guardrails.py
================================
Schema compliance guardrail tests for SQL queries across the trainer suite.

Per PLAN.md Step 1 (DQ rules embedded in each module's SQL):

  t_bet:
    - FINAL required (read-after-write consistency)
    - ``player_id != PLACEHOLDER_PLAYER_ID`` required (E4/F1 exclusion)
    - ``is_manual`` must NOT appear (that column does not exist on t_bet)

  t_session:
    - NO FINAL  (FND-01 ROW_NUMBER CTE is the dedup approach)
    - ROW_NUMBER() OVER ... PARTITION BY session_id required (FND-01)
    - ``is_deleted = 0`` required
    - ``is_canceled = 0`` required
    - ``is_manual = 0`` required

These tests operate entirely on source-code text / AST — no ClickHouse
connection or real data is required.
"""

from __future__ import annotations

import ast
import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCORER_PATH = _REPO_ROOT / "trainer" / "scorer.py"
_VALIDATOR_PATH = _REPO_ROOT / "trainer" / "validator.py"
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"

_SCORER_SRC = _SCORER_PATH.read_text(encoding="utf-8")
_VALIDATOR_SRC = _VALIDATOR_PATH.read_text(encoding="utf-8")
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")

_SCORER_TREE = ast.parse(_SCORER_SRC)
_VALIDATOR_TREE = ast.parse(_VALIDATOR_SRC)
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _func_src(tree: ast.Module, src: str, name: str) -> str:
    """Return source of the top-level function *name*, or '' if not found."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


# ── t_bet and t_session expected patterns ────────────────────────────────────

_BET_REQUIRED = [
    "FINAL",       # read consistency
    "player_id !=",  # placeholder exclusion
]

_SESSION_REQUIRED = [
    "ROW_NUMBER() OVER",  # FND-01 CTE dedup
    "is_deleted = 0",
    "is_canceled = 0",
    "is_manual = 0",
]

# Pattern that would indicate erroneous FINAL on t_session:
# looks for the TSESSION f-string expr directly followed by FINAL on the same line.
_TSESSION_FINAL_RE = re.compile(r"config\.TSESSION\}[^\n]*FINAL")


# ─────────────────────────────────────────────────────────────────────────────
class TestDQGuardrailsScorer(unittest.TestCase):
    """SQL guardrails for scorer.py fetch_recent_data."""

    def setUp(self) -> None:
        self.src = _func_src(_SCORER_TREE, _SCORER_SRC, "fetch_recent_data")
        self.assertNotEqual(self.src, "", "fetch_recent_data not found in scorer.py")

    # --- t_bet guardrails ---

    def test_bet_query_uses_final(self) -> None:
        """t_bet queries in scorer must use FINAL for read-after-write consistency."""
        self.assertIn("FINAL", self.src)

    def test_bet_query_excludes_placeholder_player_id(self) -> None:
        """t_bet queries in scorer must exclude PLACEHOLDER_PLAYER_ID rows."""
        self.assertIn("player_id !=", self.src)

    # --- t_session guardrails ---

    def test_session_query_no_final_on_tsession(self) -> None:
        """t_session table reference must NOT be followed by FINAL (wrong on sessions)."""
        match = _TSESSION_FINAL_RE.search(self.src)
        self.assertIsNone(match, "scorer.py: t_session query must not use FINAL")

    def test_session_query_uses_fnd01_row_number_cte(self) -> None:
        """t_session query must deduplicate via ROW_NUMBER() OVER (FND-01)."""
        self.assertIn("ROW_NUMBER() OVER", self.src)

    def test_session_query_filters_is_deleted(self) -> None:
        self.assertIn("is_deleted = 0", self.src)

    def test_session_query_filters_is_canceled(self) -> None:
        self.assertIn("is_canceled = 0", self.src)

    def test_session_query_filters_is_manual(self) -> None:
        self.assertIn("is_manual = 0", self.src)


# ─────────────────────────────────────────────────────────────────────────────
class TestDQGuardrailsValidatorBets(unittest.TestCase):
    """SQL guardrails for validator.py fetch_bets_by_canonical_id."""

    def setUp(self) -> None:
        self.src = _func_src(
            _VALIDATOR_TREE, _VALIDATOR_SRC, "fetch_bets_by_canonical_id"
        )
        self.assertNotEqual(
            self.src, "", "fetch_bets_by_canonical_id not found in validator.py"
        )

    def test_bet_query_uses_final(self) -> None:
        """t_bet fetch in validator must use FINAL."""
        self.assertIn("FINAL", self.src)

    def test_bet_query_excludes_placeholder_player_id(self) -> None:
        """t_bet fetch in validator must exclude PLACEHOLDER_PLAYER_ID rows."""
        self.assertIn("player_id !=", self.src)

    def test_bet_query_no_is_manual_column(self) -> None:
        """is_manual is not a column on t_bet; must not appear in bet fetch SQL."""
        self.assertNotIn("is_manual", self.src)


# ─────────────────────────────────────────────────────────────────────────────
class TestDQGuardrailsValidatorSessions(unittest.TestCase):
    """SQL guardrails for validator.py fetch_sessions_by_canonical_id."""

    def setUp(self) -> None:
        self.src = _func_src(
            _VALIDATOR_TREE, _VALIDATOR_SRC, "fetch_sessions_by_canonical_id"
        )
        self.assertNotEqual(
            self.src, "", "fetch_sessions_by_canonical_id not found in validator.py"
        )

    def test_session_query_no_final(self) -> None:
        """t_session fetch in validator must NOT use FINAL."""
        self.assertNotIn("FINAL", self.src)

    def test_session_query_no_final_on_tsession_ref(self) -> None:
        """t_session table reference must NOT be immediately followed by FINAL."""
        match = _TSESSION_FINAL_RE.search(self.src)
        self.assertIsNone(match, "validator.py: t_session query must not use FINAL")

    def test_session_query_uses_fnd01_row_number_cte(self) -> None:
        """t_session fetch in validator must deduplicate via ROW_NUMBER() OVER (FND-01)."""
        self.assertIn("ROW_NUMBER() OVER", self.src)

    def test_session_query_filters_is_deleted(self) -> None:
        self.assertIn("is_deleted = 0", self.src)

    def test_session_query_filters_is_canceled(self) -> None:
        self.assertIn("is_canceled = 0", self.src)

    def test_session_query_filters_is_manual(self) -> None:
        self.assertIn("is_manual = 0", self.src)


# ─────────────────────────────────────────────────────────────────────────────
class TestDQGuardrailsTrainer(unittest.TestCase):
    """SQL guardrails for trainer.py load_clickhouse_data (PLAN Step 1)."""

    def setUp(self) -> None:
        self.src = _func_src(_TRAINER_TREE, _TRAINER_SRC, "load_clickhouse_data")
        self.assertNotEqual(self.src, "", "load_clickhouse_data not found in trainer.py")

    def test_bet_query_uses_final(self) -> None:
        """t_bet queries in trainer must use FINAL (E5, consistency with scorer/validator)."""
        self.assertIn("FINAL", self.src)

    def test_bet_query_excludes_placeholder_player_id(self) -> None:
        """t_bet queries in trainer must exclude PLACEHOLDER_PLAYER_ID (E4/F1)."""
        self.assertIn("player_id !=", self.src)

    def test_bet_query_has_payout_complete_dtm_is_not_null(self) -> None:
        """t_bet queries in trainer must use payout_complete_dtm IS NOT NULL (E3)."""
        self.assertIn("payout_complete_dtm IS NOT NULL", self.src)

    def test_bet_query_no_is_manual_column(self) -> None:
        """is_manual is not a column on t_bet; must not appear in bet fetch SQL (E1)."""
        # Extract only the bets_query SQL string using regex for robustness.
        m = re.search(r'bets_query\s*=\s*f?"""(.*?)"""', self.src, re.DOTALL)
        self.assertIsNotNone(m, "bets_query triple-quoted string not found")
        bets_sql = m.group(1)
        self.assertNotIn("is_manual", bets_sql)

    # --- session guardrails ---

    def test_session_query_no_final_on_tsession_ref(self) -> None:
        """t_session table reference must NOT be immediately followed by FINAL."""
        # Check that TSESSION is not followed by FINAL in the same line
        lines = self.src.split('\n')
        for line in lines:
            if 'TSESSION' in line and 'FINAL' in line:
                self.fail(f"t_session query must not use FINAL: {line.strip()}")

    def test_session_query_uses_fnd01_row_number_cte(self) -> None:
        """t_session query must deduplicate via ROW_NUMBER() OVER (FND-01)."""
        self.assertIn("ROW_NUMBER() OVER", self.src)

    def test_session_query_filters_is_deleted(self) -> None:
        self.assertIn("is_deleted = 0", self.src)

    def test_session_query_filters_is_canceled(self) -> None:
        self.assertIn("is_canceled = 0", self.src)

    def test_session_query_filters_is_manual(self) -> None:
        self.assertIn("is_manual = 0", self.src)


# ─────────────────────────────────────────────────────────────────────────────
class TestDQGuardrailsCrossFile(unittest.TestCase):
    """Cross-file consistency checks for DQ guardrails."""

    def test_no_fetch_sessions_for_players_legacy_function_exists(self) -> None:
        """Legacy fetch_sessions_for_players (player_id keyed) must be fully removed."""
        self.assertNotIn("def fetch_sessions_for_players(", _VALIDATOR_SRC)

    def test_scorer_and_validator_both_exclude_placeholder(self) -> None:
        """Both scorer and validator bet queries must exclude PLACEHOLDER_PLAYER_ID."""
        self.assertIn("player_id !=", _SCORER_SRC)
        self.assertIn("player_id !=", _VALIDATOR_SRC)

    def test_scorer_and_validator_both_use_fnd01_cte_for_sessions(self) -> None:
        """Both scorer and validator session queries must include ROW_NUMBER() OVER."""
        self.assertIn("ROW_NUMBER() OVER", _SCORER_SRC)
        self.assertIn("ROW_NUMBER() OVER", _VALIDATOR_SRC)


if __name__ == "__main__":
    unittest.main()
