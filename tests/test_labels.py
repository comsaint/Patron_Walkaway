"""tests/test_labels.py
======================
Unit tests for trainer/labels.py — pure-pandas/numpy, no ClickHouse.

Coverage
--------
* G3  stable sort (same-ms bets ordered by bet_id)
* Gap-start detection (gap >= WALKAWAY_GAP_MIN)
* Label = 1 when gap_start in [t, t + ALERT_HORIZON_MIN]
* H1  terminal-bet censoring vs. determinable gap_start
* C1  extended-zone bets not themselves labelled (filtered by caller)
* Edge cases: empty input, single bet, all-censored, label=0 boundary
* Input validation: missing columns, extended_end < window_end
* Leakage check: next_bet_dtm / minutes_to_next_bet NOT in output
* Multi-canonical_id isolation: groups don't bleed into each other
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

# ---------------------------------------------------------------------------
# Import helper (same pattern as test_identity.py — no trainer/ in sys.path)
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _import_labels():
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.labels")


LABELS = _import_labels()
compute_labels = LABELS.compute_labels

try:
    from trainer.config import ALERT_HORIZON_MIN, WALKAWAY_GAP_MIN
except Exception:
    WALKAWAY_GAP_MIN = 30
    ALERT_HORIZON_MIN = 15

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

WINDOW_END = datetime(2025, 6, 1)
EXTENDED_END = datetime(2025, 6, 2)   # +1 day = plenty of C1 buffer


def _bets(rows, canonical_id="P1"):
    """Build a minimal bets DataFrame from (minutes_offset, bet_id) tuples.

    ``minutes_offset`` is minutes after an arbitrary epoch (2025-01-01).
    """
    base = datetime(2025, 1, 1)
    records = []
    for offset_min, bid in rows:
        records.append({
            "canonical_id": canonical_id,
            "bet_id": bid,
            "payout_complete_dtm": base + timedelta(minutes=offset_min),
        })
    return pd.DataFrame(records)


def _call(rows, canonical_id="P1", window_end=None, extended_end=None):
    df = _bets(rows, canonical_id=canonical_id)
    we = window_end or WINDOW_END
    ee = extended_end or EXTENDED_END
    return compute_labels(df, we, ee)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestInputValidation(unittest.TestCase):
    def test_missing_columns_raises_valueerror(self):
        df = pd.DataFrame({"canonical_id": ["P1"], "bet_id": [1]})
        with self.assertRaises(ValueError) as ctx:
            compute_labels(df, WINDOW_END, EXTENDED_END)
        self.assertIn("missing required columns", str(ctx.exception))

    def test_extended_end_before_window_end_raises(self):
        df = _bets([(0, 1)])
        with self.assertRaises(ValueError):
            compute_labels(df, WINDOW_END, WINDOW_END - timedelta(hours=1))

    def test_empty_input_returns_empty_with_columns(self):
        df = pd.DataFrame(columns=["canonical_id", "bet_id", "payout_complete_dtm"])
        result = compute_labels(df, WINDOW_END, EXTENDED_END)
        self.assertIn("label", result.columns)
        self.assertIn("censored", result.columns)
        self.assertEqual(len(result), 0)


# ---------------------------------------------------------------------------
# Leakage prevention
# ---------------------------------------------------------------------------

class TestNoLeakageColumns(unittest.TestCase):
    def test_next_bet_dtm_not_in_output(self):
        result = _call([(0, 1), (60, 2)])
        self.assertNotIn("next_bet_dtm", result.columns)

    def test_minutes_to_next_bet_not_in_output(self):
        result = _call([(0, 1), (60, 2)])
        self.assertNotIn("minutes_to_next_bet", result.columns)

    def test_internal_gap_start_not_in_output(self):
        result = _call([(0, 1), (60, 2)])
        self.assertNotIn("gap_start", result.columns)
        self.assertNotIn("_gap_start", result.columns)
        self.assertNotIn("_next_payout", result.columns)


# ---------------------------------------------------------------------------
# G3 — stable sort
# ---------------------------------------------------------------------------

class TestG3StableSort(unittest.TestCase):
    def test_output_sorted_by_payout_then_bet_id(self):
        """Same payout_complete_dtm → sorted by ascending bet_id."""
        base = datetime(2025, 1, 1)
        t = base + timedelta(hours=1)
        df = pd.DataFrame([
            {"canonical_id": "P1", "bet_id": 99, "payout_complete_dtm": t},
            {"canonical_id": "P1", "bet_id": 1,  "payout_complete_dtm": t},
            {"canonical_id": "P1", "bet_id": 50, "payout_complete_dtm": t},
        ])
        result = compute_labels(df, WINDOW_END, EXTENDED_END)
        self.assertEqual(list(result["bet_id"]), [1, 50, 99])

    def test_output_sorted_across_canonical_ids(self):
        base = datetime(2025, 1, 1)
        df = pd.DataFrame([
            {"canonical_id": "Z", "bet_id": 1, "payout_complete_dtm": base + timedelta(hours=2)},
            {"canonical_id": "A", "bet_id": 1, "payout_complete_dtm": base + timedelta(hours=1)},
        ])
        result = compute_labels(df, WINDOW_END, EXTENDED_END)
        self.assertEqual(list(result["canonical_id"]), ["A", "Z"])


# ---------------------------------------------------------------------------
# Gap detection and basic labelling
# ---------------------------------------------------------------------------

class TestGapDetectionAndLabel(unittest.TestCase):
    def test_no_gap_gives_label_zero(self):
        """Non-terminal bets with all explicit gaps < WALKAWAY_GAP_MIN get label=0.

        The terminal (last) bet does get label=1 via H1 (it IS a determinable
        gap start), but that is correct behaviour, not a test concern here.
        We verify only that the *non-terminal* bets—each more than
        ALERT_HORIZON_MIN away from the terminal gap start—carry label=0.
        """
        gap = WALKAWAY_GAP_MIN - 1  # 29 min; bets at t=0, 29, 58
        # Terminal gap_start at t=58; non-terminal bets at t=0 and t=29 are
        # 58 and 29 minutes away, both > ALERT_HORIZON_MIN=15 → label=0.
        result = _call([(0, 1), (gap, 2), (gap * 2, 3)])
        non_terminal = result[result["bet_id"].isin([1, 2])]
        self.assertTrue((non_terminal["label"] == 0).all())

    def test_exact_gap_boundary_inclusive(self):
        """Gap exactly equal to WALKAWAY_GAP_MIN IS a gap start."""
        gap = WALKAWAY_GAP_MIN   # exact boundary
        result = _call([(0, 1), (gap, 2)])
        # Bet at t=0: gap_start is at t=0 (b1 → b2 gap == X) and it's within
        # [0, ALERT_HORIZON_MIN] of itself → label = 1
        row0 = result[result["bet_id"] == 1].iloc[0]
        self.assertEqual(row0["label"], 1)

    def test_one_bet_below_gap_boundary_no_label(self):
        """Gap one minute short → no gap_start → label = 0."""
        gap = WALKAWAY_GAP_MIN - 1
        result = _call([(0, 1), (gap, 2)])
        row0 = result[result["bet_id"] == 1].iloc[0]
        self.assertEqual(row0["label"], 0)

    def test_label_one_when_gap_start_within_alert_horizon(self):
        """Bet at t=0; gap_start at t=X (WALKAWAY_GAP_MIN); X <= ALERT_HORIZON_MIN → label=1."""
        # WALKAWAY_GAP_MIN=30, ALERT_HORIZON_MIN=15
        # gap between bet1 and bet2 is WALKAWAY_GAP_MIN → gap_start at t=0
        # So bet1's label: gap_start at t=0 is within [0, 15min] → 1
        result = _call([(0, 1), (WALKAWAY_GAP_MIN, 2), (WALKAWAY_GAP_MIN + 1, 3)])
        row0 = result[result["bet_id"] == 1].iloc[0]
        self.assertEqual(row0["label"], 1)

    def test_label_zero_when_gap_start_beyond_alert_horizon(self):
        """Bet at t=0; gap_start well beyond ALERT_HORIZON_MIN → label=0."""
        # Bet at t=0, next bet at t=ALERT_HORIZON_MIN+5 (within alert window but not gap),
        # then a big gap.  The gap_start at t=ALERT_HORIZON_MIN+5 is > ALERT_HORIZON_MIN away.
        t_gap = ALERT_HORIZON_MIN + 5  # gap_start is here, which is outside the [0, 15] window
        result = _call([(0, 1), (t_gap, 2), (t_gap + WALKAWAY_GAP_MIN, 3)])
        row0 = result[result["bet_id"] == 1].iloc[0]
        # gap_start at t_gap=20, label horizon [0, 15] → 20 > 15 → label=0
        self.assertEqual(row0["label"], 0)

    def test_multiple_bets_only_near_ones_get_label_one(self):
        """Only bets where a gap_start falls within their alert window get label=1."""
        # Bets at 0, 5, 35 (gap between 5 and 35 = 30 = WALKAWAY_GAP_MIN → gap_start at t=5)
        # Bet at t=0:  gap_start at t=5,  5-0=5 <= 15 → label=1
        # Bet at t=5:  gap_start at t=5,  5-5=0 <= 15 → label=1
        # Bet at t=35: terminal, H1 (if covered): gap_start or censored
        result = _call([(0, 1), (5, 2), (35, 3)])
        labels = result.set_index("bet_id")["label"]
        self.assertEqual(labels[1], 1)
        self.assertEqual(labels[2], 1)


# ---------------------------------------------------------------------------
# H1 — terminal-bet censoring
# ---------------------------------------------------------------------------

class TestH1TerminalBet(unittest.TestCase):
    def test_terminal_covered_is_gap_start_not_censored(self):
        """Last bet with payout + WALKAWAY_GAP_MIN <= extended_end → determinable gap, censored=False."""
        # Make extended_end well beyond the last bet's WALKAWAY_GAP_MIN window
        extended_end = datetime(2025, 1, 1) + timedelta(hours=10)
        window_end = datetime(2025, 1, 1) + timedelta(hours=5)
        df = _bets([(0, 1)])  # single bet at t=0 (relative to base)
        result = compute_labels(df, window_end, extended_end)
        row = result.iloc[0]
        self.assertFalse(row["censored"])
        # gap_start = True → this bet is the last before a walkaway → label=1
        self.assertEqual(row["label"], 1)

    def test_terminal_not_covered_is_censored(self):
        """Last bet with payout + WALKAWAY_GAP_MIN > extended_end → censored=True."""
        base = datetime(2025, 1, 1)
        # Bet at t=0.  extended_end = t + (WALKAWAY_GAP_MIN - 1) min → NOT covered.
        window_end = base + timedelta(minutes=1)
        extended_end = base + timedelta(minutes=WALKAWAY_GAP_MIN - 1)
        df = pd.DataFrame([{
            "canonical_id": "P1",
            "bet_id": 1,
            "payout_complete_dtm": base,
        }])
        result = compute_labels(df, window_end, extended_end)
        row = result.iloc[0]
        self.assertTrue(row["censored"])

    def test_single_bet_covered_gives_label_one(self):
        """Single bet with enough C1 coverage → label=1, censored=False."""
        base = datetime(2025, 1, 1)
        window_end = base + timedelta(hours=1)
        extended_end = base + timedelta(hours=2)
        df = pd.DataFrame([{
            "canonical_id": "P1",
            "bet_id": 1,
            "payout_complete_dtm": base,
        }])
        result = compute_labels(df, window_end, extended_end)
        row = result.iloc[0]
        self.assertFalse(row["censored"])
        self.assertEqual(row["label"], 1)

    def test_non_terminal_bets_are_never_censored(self):
        """Only the last bet per canonical_id can be censored."""
        result = _call([(0, 1), (5, 2), (10, 3)])
        # Only the last bet (bet_id=3) could be censored; bets 1 and 2 have successors
        non_last = result[result["bet_id"].isin([1, 2])]
        self.assertTrue((~non_last["censored"]).all())


# ---------------------------------------------------------------------------
# Multi canonical_id isolation
# ---------------------------------------------------------------------------

class TestMultiCanonicalId(unittest.TestCase):
    def test_groups_do_not_bleed_into_each_other(self):
        """A gap in one canonical_id must not affect another's label.

        P1 has two bets separated by ALERT_HORIZON_MIN+2 minutes (just outside
        the alert horizon), so bet1 should be label=0 even though its terminal
        bet2 is a determinable H1 gap_start.
        P2 has a single well-covered bet → label=1 (H1 gap_start, not from P1).
        """
        base = datetime(2025, 1, 1)
        # Place bet2 at ALERT_HORIZON_MIN + 2 so it is *outside* the alert
        # horizon of bet1 (P2's gap must not be attributed to P1's bet1).
        gap_p1 = ALERT_HORIZON_MIN + 2  # 17 min > 15 → bet1 label=0
        df = pd.DataFrame([
            {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
            {"canonical_id": "P1", "bet_id": 2,
             "payout_complete_dtm": base + timedelta(minutes=gap_p1)},
            {"canonical_id": "P2", "bet_id": 3, "payout_complete_dtm": base},
        ])
        result = compute_labels(df, WINDOW_END, EXTENDED_END)
        p1 = result[result["canonical_id"] == "P1"]
        p2 = result[result["canonical_id"] == "P2"]

        # P1 bet1: terminal gap_start (bet2, H1) is gap_p1=17min away > 15min → label=0
        self.assertEqual(p1[p1["bet_id"] == 1]["label"].iloc[0], 0)
        # P2 terminal (well-covered) → label=1 (its own H1 gap_start, not P1's)
        self.assertEqual(p2.iloc[0]["label"], 1)

    def test_label_column_has_correct_length(self):
        base = datetime(2025, 1, 1)
        df = pd.DataFrame([
            {"canonical_id": "A", "bet_id": i, "payout_complete_dtm": base + timedelta(minutes=i)}
            for i in range(5)
        ] + [
            {"canonical_id": "B", "bet_id": i + 5, "payout_complete_dtm": base + timedelta(minutes=i)}
            for i in range(3)
        ])
        result = compute_labels(df, WINDOW_END, EXTENDED_END)
        self.assertEqual(len(result), 8)
        self.assertEqual(len(result["label"]), 8)


# ---------------------------------------------------------------------------
# Null payout_complete_dtm handling (E3 defensive guard)
# ---------------------------------------------------------------------------

class TestNullPayoutGuard(unittest.TestCase):
    def test_null_payout_rows_dropped(self):
        base = datetime(2025, 1, 1)
        df = pd.DataFrame([
            {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
            {"canonical_id": "P1", "bet_id": 2, "payout_complete_dtm": None},  # null
        ])
        result = compute_labels(df, WINDOW_END, EXTENDED_END)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["bet_id"], 1)


# ---------------------------------------------------------------------------
# C1 — no leakage from extended zone
# ---------------------------------------------------------------------------

class TestC1NoLeakageFromExtendedZone(unittest.TestCase):
    """Labels must not use information beyond extended_end (C1 extended pull boundary)."""

    def test_terminal_censored_when_extended_end_before_determinability(self):
        """When extended_end is strictly before payout + WALKAWAY_GAP_MIN, terminal is censored.

        Ensures we do not use future data beyond extended_end to assign labels.
        """
        base = datetime(2025, 1, 1)
        # Single bet at t=0; determinability at t=0 + WALKAWAY_GAP_MIN
        window_end = base  # must be <= extended_end
        extended_end = base + timedelta(minutes=WALKAWAY_GAP_MIN - 1)  # 1 min before we could know
        df = pd.DataFrame([
            {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
        ])
        result = compute_labels(df, window_end, extended_end)
        self.assertTrue(result.iloc[0]["censored"], "terminal must be censored when extended_end < payout + X")

    def test_terminal_not_censored_when_extended_end_at_determinability(self):
        """When extended_end equals payout + WALKAWAY_GAP_MIN, gap is determinable (no leakage)."""
        base = datetime(2025, 1, 1)
        window_end = base  # must be <= extended_end
        extended_end = base + timedelta(minutes=WALKAWAY_GAP_MIN)  # exactly at boundary
        df = pd.DataFrame([
            {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
        ])
        result = compute_labels(df, window_end, extended_end)
        self.assertFalse(result.iloc[0]["censored"], "terminal determinable when extended_end >= payout + X")


# ---------------------------------------------------------------------------
# Exact alert-horizon boundary
# ---------------------------------------------------------------------------

class TestAlertHorizonBoundary(unittest.TestCase):
    def test_gap_start_exactly_at_alert_horizon_boundary_inclusive(self):
        """Gap_start at t + ALERT_HORIZON_MIN is still within the window."""
        base = datetime(2025, 1, 1)
        # Bet at t=0; gap_start should be at t=ALERT_HORIZON_MIN
        # To achieve gap_start at t=ALERT_HORIZON_MIN:
        #   bet at ALERT_HORIZON_MIN, next bet at ALERT_HORIZON_MIN + WALKAWAY_GAP_MIN
        t_gap_start = ALERT_HORIZON_MIN
        df = pd.DataFrame([
            {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
            {"canonical_id": "P1", "bet_id": 2,
             "payout_complete_dtm": base + timedelta(minutes=t_gap_start)},
            {"canonical_id": "P1", "bet_id": 3,
             "payout_complete_dtm": base + timedelta(minutes=t_gap_start + WALKAWAY_GAP_MIN)},
        ])
        result = compute_labels(df, WINDOW_END, EXTENDED_END)
        # Bet 1 at t=0; gap_start at t=15 (bet2 → bet3 gap = 30 = X)
        # 15 - 0 = 15 = ALERT_HORIZON_MIN → inclusive boundary → label=1
        row1 = result[result["bet_id"] == 1].iloc[0]
        self.assertEqual(row1["label"], 1)

    def test_gap_start_one_minute_beyond_alert_horizon_gives_label_zero(self):
        """Gap_start one minute past ALERT_HORIZON_MIN → label=0."""
        base = datetime(2025, 1, 1)
        t_gap_start = ALERT_HORIZON_MIN + 1
        df = pd.DataFrame([
            {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
            {"canonical_id": "P1", "bet_id": 2,
             "payout_complete_dtm": base + timedelta(minutes=t_gap_start)},
            {"canonical_id": "P1", "bet_id": 3,
             "payout_complete_dtm": base + timedelta(minutes=t_gap_start + WALKAWAY_GAP_MIN)},
        ])
        result = compute_labels(df, WINDOW_END, EXTENDED_END)
        row1 = result[result["bet_id"] == 1].iloc[0]
        self.assertEqual(row1["label"], 0)


if __name__ == "__main__":
    unittest.main()
