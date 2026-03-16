"""Minimal reproducible tests for Round 221 Review — Train–Serve Parity 變更審查風險點.

Round 221 Review (STATUS.md) risk points are turned into contract/source or behavioral tests.
Tests-only: no production code changes.
"""

from __future__ import annotations

import inspect
import unittest

import pandas as pd

# Import backtester for source and _score_df behavioral test
try:
    import trainer.backtester as backtester_mod
except ImportError:
    import backtester as backtester_mod  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# R221 Review #1 — 邊界條件：feature_list_meta 條目缺 "name" 時 KeyError
# ---------------------------------------------------------------------------

class TestR221FeatureListMetaNameKeyError(unittest.TestCase):
    """R221 Review #1: backtester builds _profile_in_artifact from meta; entries without 'name' cause KeyError."""

    def test_backtest_source_uses_name_from_meta_for_profile_set(self):
        """Contract: backtest() builds _profile_in_artifact from _artifact_meta; current code uses e['name'].
        When production is fixed to e.get('name'), this test should be updated to require e.get('name')."""
        source = inspect.getsource(backtester_mod.backtest)
        self.assertIn(
            "_profile_in_artifact",
            source,
            "R221 #1: backtest must define _profile_in_artifact from artifact meta.",
        )
        # Current risky pattern: e["name"] in the set comprehension
        self.assertIn(
            'e["name"]',
            source,
            "R221 #1: Current implementation uses e['name'] (KeyError if 'name' missing). "
            "When fixed to e.get('name'), update this to assert e.get('name') or safe pattern.",
        )


# ---------------------------------------------------------------------------
# R221 Review #2 — 邊界條件：_score_df 缺欄時 KeyError 無明確契約
# ---------------------------------------------------------------------------

class TestR221ScoreDfMissingColumnsKeyError(unittest.TestCase):
    """R221 Review #2: _score_df uses df[model_features]; missing column causes KeyError (no explicit check)."""

    def test_score_df_raises_keyerror_when_feature_column_missing(self):
        """Behavioral: _score_df with df missing one of bundle['features'] raises KeyError (current behavior).
        When production adds explicit ValueError, update to expect ValueError with 'missing' in message."""
        # Mock model so that predict_proba is called; argument df[model_features] is evaluated first → KeyError.
        class _MockModel:
            def predict_proba(self, X):
                return None  # never reached when KeyError on df[model_features]
        artifacts = {
            "rated": {
                "model": _MockModel(),
                "features": ["feat_a", "feat_b"],
                "threshold": 0.5,
            },
        }
        # df has only feat_a; feat_b is missing
        df = pd.DataFrame({"feat_a": [0.0, 1.0]})
        with self.assertRaises(KeyError) as ctx:
            backtester_mod._score_df(df, artifacts)
        self.assertIn("feat_b", str(ctx.exception), "KeyError should mention the missing column.")


# ---------------------------------------------------------------------------
# R221 Review #4 — 安全性：CASINO_PLAYER_ID_CLEAN_SQL 不可含多語句
# ---------------------------------------------------------------------------

class TestR221CasinoPlayerIdCleanSqlSingleExpression(unittest.TestCase):
    """R221 Review #4: CASINO_PLAYER_ID_CLEAN_SQL must be a single expression (no semicolon)."""

    def test_config_casino_player_id_clean_sql_contains_no_semicolon(self):
        """Contract: config.CASINO_PLAYER_ID_CLEAN_SQL must not contain ';' (single expression only)."""
        try:
            import trainer.config as config
        except ImportError:
            import config  # type: ignore[no-redef]
        cid_sql = getattr(config, "CASINO_PLAYER_ID_CLEAN_SQL", "")
        self.assertIsInstance(cid_sql, str, "CASINO_PLAYER_ID_CLEAN_SQL must be a string.")
        self.assertNotIn(
            ";",
            cid_sql,
            "R221 #4: CASINO_PLAYER_ID_CLEAN_SQL must be single expression (no ';').",
        )


# ---------------------------------------------------------------------------
# R221 Review #6 — 正確性：Scorer bet 查詢含 gaming_day
# ---------------------------------------------------------------------------

class TestR221ScorerBetQueryContainsGamingDay(unittest.TestCase):
    """R221 Review #6: Scorer fetch_recent_data bet query must include gaming_day (train–serve parity)."""

    def test_scorer_fetch_recent_data_source_includes_gaming_day_in_bet_query(self):
        """Contract: scorer's bet SELECT includes gaming_day (e.g. COALESCE(gaming_day, toDate(...)))."""
        try:
            import trainer.scorer as scorer_mod
        except ImportError:
            import scorer as scorer_mod  # type: ignore[no-redef]
        source = inspect.getsource(scorer_mod.fetch_recent_data)
        self.assertIn(
            "gaming_day",
            source,
            "R221 #6: fetch_recent_data bet query must include gaming_day for train–serve parity.",
        )
        self.assertIn(
            "COALESCE(gaming_day",
            source,
            "R221 #6: fetch_recent_data should use COALESCE(gaming_day, ...) as in trainer _BET_SELECT_COLS.",
        )


if __name__ == "__main__":
    unittest.main()
