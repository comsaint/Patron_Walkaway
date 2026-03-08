"""Minimal reproducible tests for Round 156 Review (Post-Load Normalizer Phase 3 — Backtester).

Tests-only: no production code changes. Encodes Reviewer risk points as guards:
R156-1 docstring contract, R156-2 empty sessions boundary, R156-3 normalize→apply_dq categorical,
R156-4 backtester imports normalize_bets_sessions.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

import trainer.backtester as backtester_mod
import trainer.schema_io as schema_io_mod
import trainer.trainer as trainer_mod


# ---------------------------------------------------------------------------
# R156-1: backtest() docstring must document "caller must pass normalized data"
# ---------------------------------------------------------------------------

class TestR1561BacktestDocstringRequiresNormalizedInput(unittest.TestCase):
    """R156-1: backtest() docstring should state that caller must pass already-normalized data."""

    def test_backtest_docstring_requires_normalized_input(self):
        doc = backtester_mod.backtest.__doc__ or ""
        # At least one keyword that encodes the contract (Review §1).
        keywords = ("normalized", "normalizer", "already")
        self.assertTrue(
            any(k in doc for k in keywords),
            f"backtest() docstring must contain one of {keywords} to document that "
            "caller must pass already-normalized bets/sessions (Round 156 Review §1).",
        )


# ---------------------------------------------------------------------------
# R156-2: main() path with empty sessions does not crash
# ---------------------------------------------------------------------------

def _minimal_bets_df():
    now = datetime(2026, 2, 1, 12, 0, 0)
    return pd.DataFrame({
        "bet_id": [1],
        "session_id": [10],
        "player_id": [100],
        "payout_complete_dtm": [now - timedelta(minutes=5)],
        "wager": [100.0],
    })


class TestR1562BacktesterMainAcceptsEmptySessionsWithoutCrash(unittest.TestCase):
    """R156-2: main() with mocked load returning (bets_nonempty, empty_sessions) does not crash."""

    def test_backtester_main_accepts_empty_sessions_without_crash(self):
        bets = _minimal_bets_df()
        empty_sessions = pd.DataFrame()

        with patch.object(backtester_mod, "load_local_parquet", return_value=(bets, empty_sessions)), \
             patch.object(backtester_mod, "load_clickhouse_data", return_value=(bets, empty_sessions)), \
             patch.object(backtester_mod, "load_dual_artifacts", return_value={}), \
             patch.object(backtester_mod, "backtest", return_value={"micro": {}, "macro": {}}):
            argv = [
                "backtester",
                "--use-local-parquet",
                "--start", "2026-02-01",
                "--end", "2026-02-02",
                "--skip-optuna",
            ]
            with patch.object(sys, "argv", argv):
                try:
                    backtester_mod.main()
                except SystemExit as e:
                    self.fail(f"main() raised SystemExit (e.g. no bets): {e}")


# ---------------------------------------------------------------------------
# R156-3: normalize → apply_dq preserves categorical (backtester path)
# ---------------------------------------------------------------------------

def _make_sessions(now: datetime):
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


class TestR1563NormalizeThenApplyDqPreservesCategorical(unittest.TestCase):
    """R156-3: normalize_bets_sessions → apply_dq preserves categorical (backtester path)."""

    def test_backtest_path_normalize_then_apply_dq_preserves_categorical(self):
        now = datetime(2026, 3, 1, 12, 0, 0)
        window_start = now - timedelta(hours=2)
        extended_end = now
        bets = pd.DataFrame({
            "bet_id": [1, 2],
            "session_id": [11, 12],
            "player_id": [1001, 1001],
            "table_id": [1005, 1006],
            "payout_complete_dtm": [now - timedelta(minutes=10), now - timedelta(minutes=5)],
            "wager": [100.0, 200.0],
            "status": ["LOSE", "WIN"],
            "is_back_bet": [0, 1],
        })
        sessions = _make_sessions(now)

        bets_norm, sessions_norm = schema_io_mod.normalize_bets_sessions(bets, sessions)

        self.assertEqual(bets_norm["table_id"].dtype.name, "category")
        self.assertEqual(bets_norm["is_back_bet"].dtype.name, "category")

        bets_clean, _ = trainer_mod.apply_dq(
            bets=bets_norm,
            sessions=sessions_norm,
            window_start=window_start,
            extended_end=extended_end,
        )

        self.assertEqual(
            bets_clean["table_id"].dtype.name,
            "category",
            "apply_dq must not overwrite categorical table_id (Round 156 Review §5).",
        )
        self.assertEqual(
            bets_clean["is_back_bet"].dtype.name,
            "category",
            "apply_dq must not overwrite categorical is_back_bet (Round 156 Review §5).",
        )


# ---------------------------------------------------------------------------
# R156-4: backtester module imports normalize_bets_sessions
# ---------------------------------------------------------------------------

class TestR1564BacktesterImportsNormalizeBetsSessions(unittest.TestCase):
    """R156-4: backtester module must expose normalize_bets_sessions (from schema_io)."""

    def test_backtester_imports_normalize_bets_sessions(self):
        self.assertTrue(
            hasattr(backtester_mod, "normalize_bets_sessions"),
            "backtester must import normalize_bets_sessions (Round 156 Review §6).",
        )
        fn = getattr(backtester_mod, "normalize_bets_sessions")
        self.assertIn(
            "schema_io",
            getattr(fn, "__module__", ""),
            "normalize_bets_sessions in backtester should come from schema_io.",
        )
