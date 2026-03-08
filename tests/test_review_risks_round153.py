"""Minimal reproducible tests for Round 153 Review (Post-Load Normalizer Phase 2).

Tests-only: no production code changes. Covers apply_dq behavior when called
without prior normalizer (table_id dtype), categorical legacy columns skipped,
and bets without table_id.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import trainer.trainer as trainer_mod


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


class TestR153ApplyDqTableIdIntWithoutNormalizer(unittest.TestCase):
    """R153-1: apply_dq without prior normalizer; table_id as int64 passes through unchanged."""

    def test_apply_dq_with_table_id_int64_returns_table_id_unchanged(self):
        now = datetime(2026, 3, 1, 12, 0, 0)
        window_start = now - timedelta(hours=2)
        extended_end = now
        bets = pd.DataFrame({
            "bet_id": [1, 2],
            "session_id": [11, 12],
            "player_id": [1001, 1001],
            "table_id": np.array([1005, 1006], dtype=np.int64),
            "payout_complete_dtm": [now - timedelta(minutes=10), now - timedelta(minutes=5)],
            "wager": [100.0, 200.0],
            "status": ["LOSE", "WIN"],
        })
        sessions = _make_sessions(now)

        bets_clean, _ = trainer_mod.apply_dq(
            bets=bets,
            sessions=sessions,
            window_start=window_start,
            extended_end=extended_end,
        )

        self.assertIn("table_id", bets_clean.columns, "apply_dq should preserve table_id column")
        self.assertTrue(
            pd.api.types.is_integer_dtype(bets_clean["table_id"]) or pd.api.types.is_numeric_dtype(bets_clean["table_id"]),
            "table_id should remain integer/numeric when passed as int64 without normalizer",
        )
        self.assertEqual(len(bets_clean), 2)
        pd.testing.assert_series_equal(bets_clean["table_id"], bets["table_id"], check_names=True)


class TestR153ApplyDqSkipsCategoricalLegacyColumns(unittest.TestCase):
    """R153-3: apply_dq skips is_back_bet/position_idx when already categorical."""

    def test_apply_dq_skips_categorical_legacy_columns(self):
        now = datetime(2026, 3, 1, 12, 0, 0)
        window_start = now - timedelta(hours=2)
        extended_end = now
        is_back_bet_cat = pd.Series([0, 1], dtype="category")
        bets = pd.DataFrame({
            "bet_id": [1, 2],
            "session_id": [11, 12],
            "player_id": [1001, 1001],
            "table_id": [1005, 1006],
            "payout_complete_dtm": [now - timedelta(minutes=10), now - timedelta(minutes=5)],
            "wager": [100.0, 200.0],
            "status": ["LOSE", "WIN"],
            "is_back_bet": is_back_bet_cat,
        })
        sessions = _make_sessions(now)

        bets_clean, _ = trainer_mod.apply_dq(
            bets=bets,
            sessions=sessions,
            window_start=window_start,
            extended_end=extended_end,
        )

        self.assertEqual(
            bets_clean["is_back_bet"].dtype.name,
            "category",
            "apply_dq should not overwrite categorical is_back_bet with to_numeric",
        )
        np.testing.assert_array_equal(
            bets_clean["is_back_bet"].values,
            is_back_bet_cat.values,
            err_msg="apply_dq should not overwrite categorical is_back_bet values",
        )


class TestR153ApplyDqAcceptsBetsWithoutTableId(unittest.TestCase):
    """R153-5: apply_dq accepts bets without table_id; no KeyError."""

    def test_apply_dq_accepts_bets_without_table_id(self):
        now = datetime(2026, 3, 1, 12, 0, 0)
        window_start = now - timedelta(hours=2)
        extended_end = now
        bets = pd.DataFrame({
            "bet_id": [1, 2],
            "session_id": [11, 12],
            "player_id": [1001, 1001],
            "payout_complete_dtm": [now - timedelta(minutes=10), now - timedelta(minutes=5)],
            "wager": [100.0, 200.0],
            "status": ["LOSE", "WIN"],
        })
        sessions = _make_sessions(now)

        bets_clean, sess_clean = trainer_mod.apply_dq(
            bets=bets,
            sessions=sessions,
            window_start=window_start,
            extended_end=extended_end,
        )

        self.assertNotIn("table_id", bets_clean.columns)
        self.assertEqual(len(bets_clean), 2)
        self.assertTrue(bets_clean["bet_id"].notna().all())
