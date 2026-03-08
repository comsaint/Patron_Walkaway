"""Minimal reproducible tests for Round 159 Review (Post-Load Normalizer Phase 4 — Scorer).

Tests-only: no production code changes. Encodes Reviewer risk points as guards:
R159-1 fetch→normalize receives DataFrames, R159-2 empty sessions, R159-3 build_features
preserves categorical when normalized, R159-4 scorer imports normalize_bets_sessions.
"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd
from zoneinfo import ZoneInfo

import trainer.scorer as scorer_mod
import trainer.schema_io as schema_io_mod


def _minimal_bets():
    now = datetime(2026, 3, 1, 12, 0, 0)
    return pd.DataFrame({
        "bet_id": [1, 2],
        "session_id": [11, 12],
        "player_id": [1001, 1001],
        "table_id": [1005, 1006],
        "payout_complete_dtm": [now - timedelta(minutes=10), now - timedelta(minutes=5)],
        "wager": [100.0, 200.0],
        "status": ["LOSE", "WIN"],
        "payout_odds": [1.9, 2.0],
        "base_ha": [0.02, 0.02],
        "is_back_bet": [0, 1],
        "position_idx": [0, 1],
    })


def _minimal_sessions():
    now = datetime(2026, 3, 1, 12, 0, 0)
    return pd.DataFrame({
        "session_id": [11, 12],
        "player_id": [1001, 1001],
        "session_start_dtm": [now - timedelta(hours=1)] * 2,
        "session_end_dtm": [now - timedelta(minutes=20), now - timedelta(minutes=15)],
        "lud_dtm": [now - timedelta(minutes=20), now - timedelta(minutes=15)],
        "is_manual": [0, 0],
        "is_deleted": [0, 0],
        "is_canceled": [0, 0],
        "turnover": [1000, 1200],
        "num_games_with_wager": [5, 6],
    })


# ---------------------------------------------------------------------------
# R159-1: score_once passes DataFrames from fetch to normalize_bets_sessions
# ---------------------------------------------------------------------------

class TestR1591ScoreOnceNormalizeReceivesDataFrameFromFetch(unittest.TestCase):
    """R159-1: score_once calls normalize_bets_sessions with two DataFrames from fetch."""

    def test_score_once_normalize_receives_dataframe_from_fetch(self):
        bets = _minimal_bets()
        sessions = _minimal_sessions()
        mock_normalize = Mock(side_effect=lambda b, s: (b, s))

        artifacts = {
            "feature_list": ["wager"],
            "model_version": "test-v0",
            "feature_spec": None,
        }
        with (
            patch.object(scorer_mod, "fetch_recent_data", return_value=(bets, sessions)),
            patch.object(scorer_mod, "normalize_bets_sessions", mock_normalize),
            patch.object(
                scorer_mod,
                "build_canonical_mapping_from_df",
                return_value=pd.DataFrame({"player_id": [1001], "canonical_id": ["c1001"]}),
            ),
            patch.object(scorer_mod, "prune_old_state"),
            patch.object(scorer_mod, "refresh_alert_history"),
            patch.object(scorer_mod, "update_state_with_new_bets", return_value=bets),
            patch.object(
                scorer_mod,
                "build_features_for_scoring",
                return_value=pd.DataFrame({
                    "bet_id": [1, 2],
                    "player_id": [1001, 1001],
                    "wager": [100.0, 200.0],
                    "canonical_id": ["c1001", "c1001"],
                }),
            ),
            patch.object(scorer_mod, "compute_track_llm_features", side_effect=lambda df, **_: df),
            patch.object(scorer_mod, "_compute_reason_codes", return_value=["[]"]),
            patch.object(scorer_mod, "get_session_totals", return_value=(0, 0.0, None, None)),
            patch.object(scorer_mod, "get_session_count", return_value=0),
            patch.object(scorer_mod, "get_historical_avg", return_value=0.0),
            patch.object(scorer_mod, "append_alerts"),
        ):
            conn = sqlite3.connect(":memory:")
            scorer_mod.score_once(
                artifacts, lookback_hours=1, alert_history=set(), conn=conn, retention_hours=1
            )

        self.assertEqual(mock_normalize.call_count, 1, "normalize_bets_sessions should be called once")
        args = mock_normalize.call_args[0]
        self.assertIsInstance(args[0], pd.DataFrame, "normalize should receive DataFrame as first arg")
        self.assertIsInstance(args[1], pd.DataFrame, "normalize should receive DataFrame as second arg")


# ---------------------------------------------------------------------------
# R159-2: score_once with empty sessions does not crash
# ---------------------------------------------------------------------------

class TestR1592ScoreOnceWithEmptySessionsDoesNotCrash(unittest.TestCase):
    """R159-2: score_once with fetch returning (bets_nonempty, empty_sessions) does not crash."""

    def test_score_once_with_empty_sessions_does_not_crash(self):
        bets = _minimal_bets()
        empty_sessions = pd.DataFrame()

        artifacts = {
            "feature_list": ["wager"],
            "model_version": "test-v0",
            "feature_spec": None,
        }
        with (
            patch.object(scorer_mod, "fetch_recent_data", return_value=(bets, empty_sessions)),
            patch.object(
                scorer_mod,
                "build_canonical_mapping_from_df",
                return_value=pd.DataFrame(),
            ),
            patch.object(scorer_mod, "prune_old_state"),
            patch.object(scorer_mod, "refresh_alert_history"),
            patch.object(scorer_mod, "update_state_with_new_bets", return_value=bets),
            patch.object(
                scorer_mod,
                "build_features_for_scoring",
                return_value=pd.DataFrame({
                    "bet_id": [1, 2],
                    "player_id": [1001, 1001],
                    "wager": [100.0, 200.0],
                    "canonical_id": ["c1001", "c1001"],
                }),
            ),
            patch.object(scorer_mod, "compute_track_llm_features", side_effect=lambda df, **_: df),
            patch.object(scorer_mod, "_compute_reason_codes", return_value=["[]"]),
            patch.object(scorer_mod, "get_session_totals", return_value=(0, 0.0, None, None)),
            patch.object(scorer_mod, "get_session_count", return_value=0),
            patch.object(scorer_mod, "get_historical_avg", return_value=0.0),
            patch.object(scorer_mod, "append_alerts"),
        ):
            conn = sqlite3.connect(":memory:")
            scorer_mod.score_once(
                artifacts, lookback_hours=1, alert_history=set(), conn=conn, retention_hours=1
            )


# ---------------------------------------------------------------------------
# R159-3: build_features_for_scoring preserves categorical when input is normalized
# ---------------------------------------------------------------------------

class TestR1593BuildFeaturesPreservesCategoricalWhenNormalized(unittest.TestCase):
    """R159-3: build_features_for_scoring preserves position_idx/is_back_bet as category (Round 159 Review §3)."""

    def test_build_features_for_scoring_preserves_categorical_when_normalized(self):
        bets = _minimal_bets()
        sessions = _minimal_sessions()
        bets_norm, sessions_norm = schema_io_mod.normalize_bets_sessions(bets, sessions)

        self.assertEqual(bets_norm["position_idx"].dtype.name, "category")
        self.assertEqual(bets_norm["is_back_bet"].dtype.name, "category")

        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        canonical_map = pd.DataFrame({"player_id": [1001], "canonical_id": ["c1001"]})

        out = scorer_mod.build_features_for_scoring(
            bets_norm, sessions_norm, canonical_map, cutoff
        )

        self.assertEqual(
            out["position_idx"].dtype.name,
            "category",
            "build_features_for_scoring must not overwrite categorical position_idx (Round 159 Review §3).",
        )
        self.assertEqual(
            out["is_back_bet"].dtype.name,
            "category",
            "build_features_for_scoring must not overwrite categorical is_back_bet (Round 159 Review §3).",
        )


# ---------------------------------------------------------------------------
# R159-4: scorer module imports normalize_bets_sessions from schema_io
# ---------------------------------------------------------------------------

class TestR1594ScorerImportsNormalizeBetsSessions(unittest.TestCase):
    """R159-4: scorer module must expose normalize_bets_sessions (from schema_io)."""

    def test_scorer_imports_normalize_bets_sessions(self):
        self.assertTrue(
            hasattr(scorer_mod, "normalize_bets_sessions"),
            "scorer must import normalize_bets_sessions (Round 159 Review §5).",
        )
        fn = getattr(scorer_mod, "normalize_bets_sessions")
        self.assertIn(
            "schema_io",
            getattr(fn, "__module__", ""),
            "normalize_bets_sessions in scorer should come from schema_io.",
        )
