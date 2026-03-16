"""Minimal reproducible tests for Round 162 Review (Post-Load Normalizer Phase 5 — ETL).

Tests-only: no production code changes. Encodes Reviewer risk points as guards:
R162-1 pandas path calls normalize_bets_sessions once with DataFrame; R162-2 ETL imports normalize_bets_sessions.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd

import trainer.etl_player_profile as etl_mod


def _minimal_sessions():
    """Minimal sessions DataFrame for pandas path (session_id, player_id, num_games_with_wager, etc.)."""
    base = datetime(2026, 2, 28, 0, 0, 0)
    return pd.DataFrame({
        "session_id": [1, 2],
        "player_id": [101, 101],
        "session_start_dtm": [base - timedelta(hours=2)] * 2,
        "session_end_dtm": [base - timedelta(minutes=30), base - timedelta(minutes=20)],
        "lud_dtm": [base - timedelta(minutes=30), base - timedelta(minutes=20)],
        "num_games_with_wager": [2, 3],
        "turnover": [100.0, 150.0],
        "is_manual": [0, 0],
        "is_deleted": [0, 0],
        "is_canceled": [0, 0],
    })


# ---------------------------------------------------------------------------
# R162-1: pandas path calls normalize_bets_sessions once with DataFrame
# ---------------------------------------------------------------------------

class TestR1621PandasPathCallsNormalizeOnceWithDataFrame(unittest.TestCase):
    """R162-1: build_player_profile (pandas path) calls normalize_bets_sessions once with sessions as DataFrame."""

    def test_build_player_profile_pandas_path_calls_normalize_once_with_dataframe(self):
        sessions = _minimal_sessions()
        canonical_map = pd.DataFrame({"player_id": ["101"], "canonical_id": ["c101"]})
        mock_normalize = Mock(side_effect=lambda bets, sess: (bets, sess))
        snapshot_date = date(2026, 2, 28)

        # Force pandas path: no DuckDB (PROFILE_USE_DUCKDB=False), and provide sessions via _load_sessions_local.
        with (
            patch.object(etl_mod.config, "PROFILE_USE_DUCKDB", False),
            patch.object(etl_mod, "_load_sessions_local", return_value=sessions),
            patch.object(etl_mod, "normalize_bets_sessions", mock_normalize),
            patch.object(etl_mod, "_compute_profile", return_value=pd.DataFrame({"canonical_id": ["c101"], "col": [1]})),
            patch.object(etl_mod, "_persist_local_parquet"),
        ):
            result = etl_mod.build_player_profile(
                snapshot_date,
                use_local_parquet=True,
                canonical_map=canonical_map,
                preloaded_sessions=None,
            )

        self.assertEqual(mock_normalize.call_count, 1, "normalize_bets_sessions should be called once (Round 162 Review §1)")
        args = mock_normalize.call_args[0]
        self.assertIsInstance(args[1], pd.DataFrame, "normalize_bets_sessions second arg should be DataFrame")
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# R162-2: ETL module imports normalize_bets_sessions from schema_io
# ---------------------------------------------------------------------------

class TestR1622EtlPlayerProfileImportsNormalizeBetsSessions(unittest.TestCase):
    """R162-2: etl_player_profile module must expose normalize_bets_sessions (from schema_io)."""

    def test_etl_player_profile_imports_normalize_bets_sessions(self):
        self.assertTrue(
            hasattr(etl_mod, "normalize_bets_sessions"),
            "etl_player_profile must import normalize_bets_sessions (Round 162 Review §5).",
        )
        fn = getattr(etl_mod, "normalize_bets_sessions")
        self.assertIn(
            "schema_io",
            getattr(fn, "__module__", ""),
            "normalize_bets_sessions in etl_player_profile should come from schema_io.",
        )
