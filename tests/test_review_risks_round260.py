"""Minimal reproducible tests for Round 72 review risks (R1200-R1205).

Tests-only round: we intentionally do not change production code here.
Unfixed risks are encoded as expected failures so they stay visible.
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path

import trainer.config as config_mod
import trainer.etl_player_profile as profile_mod
import trainer.validator as validator_mod


_SCORER_POLL_SQL = (
    Path(__file__).resolve().parents[1]
    / "trainer"
    / "scripts"
    / "scorer_poll_queries.sql"
).read_text(encoding="utf-8")


class TestR1200ProfileFnd01Parity(unittest.TestCase):
    """R1200: profile ETL should use full FND-01 ORDER BY."""

    def test_etl_profile_session_query_has_fnd01_full_order_by(self):
        src = inspect.getsource(profile_mod._load_sessions)
        self.assertTrue(
            "NULLS LAST" in src and "__etl_insert_Dtm" in src,
            "_load_sessions FND-01 should include NULLS LAST + __etl_insert_Dtm tiebreaker.",
        )


class TestR1201ScorerPollBetsGuard(unittest.TestCase):
    """R1201: scorer_poll SQL bets query should explicitly guard NULL player_id."""

    def test_scorer_poll_sql_bets_has_player_id_is_not_null(self):
        self.assertIn(
            "AND player_id IS NOT NULL",
            _SCORER_POLL_SQL,
            "scorer_poll_queries.sql bets query should include explicit player_id IS NOT NULL.",
        )


class TestR1202ScorerPollSessionFnd04(unittest.TestCase):
    """R1202: scorer_poll SQL session query should include FND-04 activity filter."""

    def test_scorer_poll_sql_sessions_has_fnd04_filter(self):
        self.assertIn(
            "COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0",
            _SCORER_POLL_SQL,
            "scorer_poll_queries.sql sessions query should include FND-04 activity filter.",
        )


class TestR1203ValidatorSessionFnd04(unittest.TestCase):
    """R1203: validator session query should include FND-04 activity filter."""

    def test_validator_session_query_has_fnd04_filter(self):
        src = inspect.getsource(validator_mod.fetch_sessions_by_canonical_id)
        self.assertTrue(
            "COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0" in src
            and "turnover" in src
            and "num_games_with_wager" in src,
            "validator session query should include turnover columns + FND-04 filter.",
        )


class TestR1204ValidatorBetsGuard(unittest.TestCase):
    """R1204: validator bets query should explicitly guard NULL player_id."""

    def test_validator_bets_query_has_player_id_is_not_null(self):
        src = inspect.getsource(validator_mod.fetch_bets_by_canonical_id)
        self.assertIn(
            "player_id IS NOT NULL",
            src,
            "validator bets query should include explicit player_id IS NOT NULL.",
        )


class TestR1205ConfigCommentFreshness(unittest.TestCase):
    """R1205: config comment should reflect 1-D F1 threshold search."""

    def test_config_should_not_keep_2d_threshold_comment(self):
        src = inspect.getsource(config_mod)
        self.assertNotIn(
            "2-D threshold search",
            src,
            "config comment is stale: threshold search should be described as F1-based (single threshold).",
        )


if __name__ == "__main__":
    unittest.main()
