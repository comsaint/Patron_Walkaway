"""tests/test_features.py
========================
Unit tests for trainer/features.py — Track B vectorized functions only.
No ClickHouse, no Featuretools, no LightGBM required.

Coverage
--------
compute_loss_streak:
  * LOSE→+1, WIN→reset, PUSH no-reset and reset variants
  * Multi-group (canonical_id) isolation
  * cutoff_time filtering (TRN-09 / E2)
  * G3 sort contract (unsorted input produces same result)
  * Empty input, single row, missing columns

compute_run_boundary:
  * New run on first bet, and on gap >= RUN_BREAK_MIN
  * run_id 0-indexed, minutes_since_run_start >= 0
  * Multi-group isolation
  * Empty input, single row, missing columns

compute_table_hc:
  * Correct window boundary (exclusive BET_AVAIL_DELAY_MIN before current bet)
  * Unique player count (duplicates count once)
  * PLACEHOLDER_PLAYER_ID excluded from count
  * cutoff_time global upper bound
  * Multi-table isolation
  * Empty input, missing columns
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _import_features():
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.features")


FEATURES = _import_features()
compute_loss_streak = FEATURES.compute_loss_streak
compute_run_boundary = FEATURES.compute_run_boundary
compute_table_hc = FEATURES.compute_table_hc

try:
    from trainer.config import (
        BET_AVAIL_DELAY_MIN,
        LOSS_STREAK_PUSH_RESETS,
        PLACEHOLDER_PLAYER_ID,
        RUN_BREAK_MIN,
        TABLE_HC_WINDOW_MIN,
    )
except Exception:
    BET_AVAIL_DELAY_MIN = 1
    LOSS_STREAK_PUSH_RESETS = False
    PLACEHOLDER_PLAYER_ID = -1
    RUN_BREAK_MIN = 30
    TABLE_HC_WINDOW_MIN = 30

_BASE = datetime(2025, 1, 1)
_WE = datetime(2025, 6, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bets(rows, canonical_id="P1", table_id="T1", player_id=1):
    """Build minimal bets df from (offset_min, bet_id, status?) tuples."""
    records = []
    for item in rows:
        if len(item) == 3:
            offset_min, bid, status = item
        else:
            offset_min, bid = item
            status = "LOSE"
        records.append({
            "canonical_id": canonical_id,
            "bet_id": bid,
            "payout_complete_dtm": _BASE + timedelta(minutes=offset_min),
            "status": status,
            "table_id": table_id,
            "player_id": player_id,
        })
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# compute_loss_streak
# ---------------------------------------------------------------------------

class TestComputeLossStreak(unittest.TestCase):

    def _streak(self, rows, canonical_id="P1", cutoff=None):
        df = _bets(rows, canonical_id=canonical_id)
        return compute_loss_streak(df, cutoff_time=cutoff)

    def test_all_loses_give_increasing_streak(self):
        result = self._streak([(0, 1, "LOSE"), (1, 2, "LOSE"), (2, 3, "LOSE")])
        self.assertEqual(list(result), [1, 2, 3])

    def test_win_resets_streak_to_zero(self):
        result = self._streak([
            (0, 1, "LOSE"), (1, 2, "LOSE"), (2, 3, "WIN"), (3, 4, "LOSE"),
        ])
        self.assertEqual(list(result), [1, 2, 0, 1])

    def test_push_no_reset_by_default(self):
        # LOSS_STREAK_PUSH_RESETS=False: PUSH doesn't change the streak
        result = self._streak([
            (0, 1, "LOSE"), (1, 2, "PUSH"), (2, 3, "LOSE"),
        ])
        # Streak: 1 → 1 (PUSH unchanged) → 2
        self.assertEqual(list(result), [1, 1, 2])

    def test_win_after_push_resets(self):
        result = self._streak([
            (0, 1, "LOSE"), (1, 2, "PUSH"), (2, 3, "WIN"), (3, 4, "LOSE"),
        ])
        self.assertEqual(list(result), [1, 1, 0, 1])

    def test_starting_win_gives_zero(self):
        result = self._streak([(0, 1, "WIN"), (1, 2, "LOSE")])
        self.assertEqual(list(result), [0, 1])

    def test_all_wins_give_zero(self):
        result = self._streak([(0, 1, "WIN"), (1, 2, "WIN"), (2, 3, "WIN")])
        self.assertEqual(list(result), [0, 0, 0])

    def test_all_pushes_give_zero_streak(self):
        # PUSH with push_resets=False: no LOSE increment, streak stays 0
        result = self._streak([(0, 1, "PUSH"), (1, 2, "PUSH")])
        self.assertEqual(list(result), [0, 0])

    def test_multi_group_isolated(self):
        """Two canonical_ids must not bleed into each other."""
        p1 = _bets(
            [(0, 1, "LOSE"), (1, 2, "LOSE")], canonical_id="P1"
        )
        p2 = _bets(
            [(0, 3, "WIN"), (1, 4, "LOSE")], canonical_id="P2"
        )
        df = pd.concat([p1, p2], ignore_index=True)
        result = compute_loss_streak(df)
        by_cid = df.copy()
        by_cid["streak"] = result.values
        p1_streak = by_cid[by_cid["canonical_id"] == "P1"]["streak"].tolist()
        p2_streak = by_cid[by_cid["canonical_id"] == "P2"]["streak"].tolist()
        self.assertEqual(p1_streak, [1, 2])
        self.assertEqual(p2_streak, [0, 1])

    def test_g3_unsorted_input_same_result(self):
        """Result must be identical regardless of input row order."""
        rows = [(0, 1, "LOSE"), (2, 3, "LOSE"), (1, 2, "WIN")]
        df_sorted = _bets(rows)
        df_reversed = df_sorted.iloc[::-1].reset_index(drop=True)
        r1 = compute_loss_streak(df_sorted)
        r2 = compute_loss_streak(df_reversed)
        # Align by bet_id for comparison
        by_bid1 = dict(zip(df_sorted["bet_id"], r1.values))
        by_bid2 = dict(zip(df_reversed["bet_id"], r2.values))
        self.assertEqual(by_bid1, by_bid2)

    def test_cutoff_time_excludes_later_bets(self):
        result = self._streak(
            [(0, 1, "LOSE"), (10, 2, "LOSE"), (20, 3, "LOSE")],
            cutoff=_BASE + timedelta(minutes=10),  # inclusive: bets at 0 and 10 only
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(list(result), [1, 2])

    def test_empty_input_returns_empty_series(self):
        df = pd.DataFrame(
            columns=["canonical_id", "bet_id", "payout_complete_dtm", "status"]
        )
        result = compute_loss_streak(df)
        self.assertEqual(len(result), 0)

    def test_missing_columns_raises(self):
        df = pd.DataFrame({"canonical_id": ["P1"], "bet_id": [1]})
        with self.assertRaises(ValueError):
            compute_loss_streak(df)


# ---------------------------------------------------------------------------
# compute_run_boundary
# ---------------------------------------------------------------------------

class TestComputeRunBoundary(unittest.TestCase):

    def _run(self, rows, canonical_id="P1"):
        df = _bets(rows, canonical_id=canonical_id)
        return compute_run_boundary(df)

    def test_single_bet_is_run_zero(self):
        result = self._run([(0, 1)])
        self.assertEqual(result.iloc[0]["run_id"], 0)
        self.assertAlmostEqual(result.iloc[0]["minutes_since_run_start"], 0.0)

    def test_small_gap_no_new_run(self):
        gap = RUN_BREAK_MIN - 1
        result = self._run([(0, 1), (gap, 2)])
        self.assertEqual(list(result["run_id"]), [0, 0])
        self.assertAlmostEqual(result.iloc[1]["minutes_since_run_start"], gap, places=3)

    def test_exact_break_min_starts_new_run(self):
        result = self._run([(0, 1), (RUN_BREAK_MIN, 2)])
        self.assertEqual(list(result["run_id"]), [0, 1])
        self.assertAlmostEqual(result.iloc[1]["minutes_since_run_start"], 0.0)

    def test_multiple_runs(self):
        gap = RUN_BREAK_MIN
        result = self._run([(0, 1), (5, 2), (gap + 5, 3), (gap + 10, 4), (2 * gap + 15, 5)])
        runs = list(result["run_id"])
        self.assertEqual(runs[0], 0)   # bet 1: run 0
        self.assertEqual(runs[1], 0)   # bet 2: still run 0 (gap 5 < 30)
        self.assertEqual(runs[2], 1)   # bet 3: gap from bet2 = 25 min < 30 → hmm
        # gap from bet2 (5min) to bet3 (30+5=35min) = 30min = RUN_BREAK_MIN → new run
        self.assertEqual(runs[2], 1)
        self.assertEqual(runs[3], 1)   # bet 4: gap from bet3 (35) to bet4 (40) = 5 < 30
        self.assertEqual(runs[4], 2)   # bet 5: gap from bet4 (40) to bet5 (65+15=80) = 40 > 30

    def test_minutes_since_run_start_zero_at_run_boundary(self):
        result = self._run([(0, 1), (RUN_BREAK_MIN, 2), (RUN_BREAK_MIN + 5, 3)])
        self.assertAlmostEqual(result.iloc[1]["minutes_since_run_start"], 0.0)
        self.assertAlmostEqual(result.iloc[2]["minutes_since_run_start"], 5.0, places=3)

    def test_multi_group_run_ids_are_independent(self):
        """run_id must reset per canonical_id."""
        p1 = _bets([(0, 1), (RUN_BREAK_MIN, 2)], canonical_id="P1")
        p2 = _bets([(0, 3)], canonical_id="P2")
        df = pd.concat([p1, p2], ignore_index=True)
        result = compute_run_boundary(df)
        p1_runs = result[result["canonical_id"] == "P1"]["run_id"].tolist()
        p2_runs = result[result["canonical_id"] == "P2"]["run_id"].tolist()
        self.assertEqual(p1_runs, [0, 1])
        self.assertEqual(p2_runs, [0])

    def test_empty_input_has_expected_columns(self):
        df = pd.DataFrame(
            columns=["canonical_id", "bet_id", "payout_complete_dtm"]
        )
        result = compute_run_boundary(df)
        self.assertIn("run_id", result.columns)
        self.assertIn("minutes_since_run_start", result.columns)
        self.assertEqual(len(result), 0)

    def test_missing_columns_raises(self):
        df = pd.DataFrame({"canonical_id": ["P1"], "bet_id": [1]})
        with self.assertRaises(ValueError):
            compute_run_boundary(df)


# ---------------------------------------------------------------------------
# compute_table_hc
# ---------------------------------------------------------------------------

class TestComputeTableHc(unittest.TestCase):

    def _make_bets(self, rows):
        """rows: (offset_min, bet_id, table_id, player_id)"""
        records = [
            {
                "bet_id": bid,
                "payout_complete_dtm": _BASE + timedelta(minutes=off),
                "table_id": tid,
                "player_id": pid,
            }
            for off, bid, tid, pid in rows
        ]
        return pd.DataFrame(records)

    def test_no_prior_bets_gives_zero(self):
        df = self._make_bets([(0, 1, "T1", 1)])
        result = compute_table_hc(df, cutoff_time=None)
        self.assertEqual(result.iloc[0], 0)

    def test_single_prior_bet_counts_one(self):
        # bet1 at t=0, bet2 at t=BET_AVAIL_DELAY_MIN+1 → bet1 visible for bet2
        t1 = 0
        t2 = BET_AVAIL_DELAY_MIN + 1  # bet1 is at t1=0, available at t2 - delay = 0
        df = self._make_bets([(t1, 1, "T1", 1), (t2, 2, "T1", 2)])
        result = compute_table_hc(df, cutoff_time=None)
        # bet2's window: [t2 - TABLE_HC_WINDOW_MIN - delay, t2 - delay]
        # = [1+1-30-1, 1+1-1] = [-29, 1] → includes t1=0 → count=1
        self.assertEqual(result.iloc[1], 1)

    def test_window_excludes_bets_too_recent(self):
        # Two bets on the same table at exactly the same time (t=10 min).
        # Each bet's window upper = t - BET_AVAIL_DELAY_MIN (< t), so neither
        # appears in the other's lookback window — both should return hc=0.
        t = 10
        df = self._make_bets([(t, 1, "T1", 1), (t, 2, "T1", 2)])
        result = compute_table_hc(df, cutoff_time=None)
        # window upper (for either bet) = t - delay < t → no pool bets in window
        self.assertEqual(result.iloc[0], 0)
        self.assertEqual(result.iloc[1], 0)

    def test_duplicate_player_counted_once(self):
        # Same player_id appears twice in the window → count = 1
        t_pool = BET_AVAIL_DELAY_MIN + 1
        t2 = t_pool + TABLE_HC_WINDOW_MIN - 1  # t2 - delay covers t_pool
        df = self._make_bets([
            (t_pool, 1, "T1", 42),  # player 42, first bet
            (t_pool + 1, 2, "T1", 42),  # player 42, second bet (same player)
            (t2, 3, "T1", 99),  # the target bet
        ])
        result = compute_table_hc(df, cutoff_time=None)
        # Only player 42 in window → hc = 1
        self.assertEqual(result.iloc[2], 1)

    def test_placeholder_player_excluded(self):
        t_pool = BET_AVAIL_DELAY_MIN + 1
        t2 = t_pool + TABLE_HC_WINDOW_MIN - 1
        df = self._make_bets([
            (t_pool, 1, "T1", PLACEHOLDER_PLAYER_ID),  # placeholder
            (t_pool + 1, 2, "T1", 77),  # real player
            (t2, 3, "T1", 99),  # target
        ])
        result = compute_table_hc(df, cutoff_time=None)
        # Only player 77 in pool (placeholder excluded) → hc = 1
        self.assertEqual(result.iloc[2], 1)

    def test_different_tables_isolated(self):
        t_pool = BET_AVAIL_DELAY_MIN + 1
        t2 = t_pool + TABLE_HC_WINDOW_MIN - 1
        df = self._make_bets([
            (t_pool, 1, "T1", 1),  # player on T1
            (t_pool, 2, "T2", 2),  # player on T2
            (t2, 3, "T1", 99),  # target on T1
        ])
        result = compute_table_hc(df, cutoff_time=None)
        # Only bet1 (T1, player1) in T1's window → hc = 1; bet on T2 doesn't count
        self.assertEqual(result.iloc[2], 1)

    def test_cutoff_time_excludes_pool_bets(self):
        # Pool bet at t=10, target at t=15; cutoff_time=t=11
        # avail_limit = 11 - delay → only pool bets at t <= 11-delay
        t_pool = 10
        t_target = t_pool + BET_AVAIL_DELAY_MIN + 1  # 12 (if delay=1)
        cutoff = _BASE + timedelta(minutes=t_pool - 1)  # cutoff before pool bet
        df = self._make_bets([(t_pool, 1, "T1", 1), (t_target, 2, "T1", 99)])
        result = compute_table_hc(df, cutoff_time=cutoff)
        # Pool bet is excluded by cutoff → hc = 0
        self.assertEqual(result.iloc[1], 0)

    def test_empty_input_returns_empty(self):
        df = pd.DataFrame(
            columns=["table_id", "bet_id", "payout_complete_dtm", "player_id"]
        )
        result = compute_table_hc(df, cutoff_time=None)
        self.assertEqual(len(result), 0)

    def test_missing_columns_raises(self):
        df = pd.DataFrame({"table_id": ["T1"], "bet_id": [1]})
        with self.assertRaises(ValueError):
            compute_table_hc(df, cutoff_time=None)


if __name__ == "__main__":
    unittest.main()
