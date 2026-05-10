"""Reviewer risk tests aligned with run-day boundary contract (no lookback window).

``lookback_hours`` is rejected for ``compute_loss_streak``, ``compute_run_boundary``,
and ``add_run_state_machine_features``. Run boundaries use gap + ``gaming_day`` only.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trainer.features import compute_loss_streak, compute_run_boundary  # noqa: E402

_BASE = datetime(2025, 1, 1)


def _bets(
    rows: list,
    canonical_id: str = "P1",
    table_id: str = "T1",
    player_id: int = 1,
) -> pd.DataFrame:
    """Minimal bets df from (offset_min, bet_id, status?) tuples."""
    records = []
    for item in rows:
        if len(item) == 3:
            offset_min, bid, status = item
        else:
            offset_min, bid = item
            status = "LOSE"
        pcd = _BASE + timedelta(minutes=offset_min)
        records.append({
            "canonical_id": canonical_id,
            "bet_id": bid,
            "payout_complete_dtm": pcd,
            "gaming_day": pcd.date(),
            "status": status,
            "table_id": table_id,
            "player_id": player_id,
        })
    return pd.DataFrame(records)


class TestLookbackHoursRejected(unittest.TestCase):
    """Non-None lookback_hours must raise (run-day contract)."""

    def test_loss_streak_lookback_hours_rejected(self) -> None:
        df = _bets([(0, 1, "LOSE")])
        with self.assertRaises(ValueError) as ctx:
            compute_loss_streak(df, cutoff_time=None, lookback_hours=0)
        self.assertIn("lookback_hours", str(ctx.exception))

    def test_run_boundary_lookback_hours_rejected(self) -> None:
        df = _bets([(0, 1)])
        with self.assertRaises(ValueError) as ctx:
            compute_run_boundary(df, cutoff_time=None, lookback_hours=0)
        self.assertIn("lookback_hours", str(ctx.exception))

    def test_run_boundary_large_lookback_still_rejected(self) -> None:
        df = _bets([(0, 1)])
        df["wager"] = 1.0
        with self.assertRaises(ValueError) as ctx:
            compute_run_boundary(df, cutoff_time=None, lookback_hours=1e10)
        msg = str(ctx.exception).lower()
        self.assertIn("lookback", msg)
        self.assertIn("none", msg)

    def test_add_run_state_machine_lookback_rejected(self) -> None:
        from trainer.trainer import add_run_state_machine_features

        df = _bets([(0, 1, "LOSE"), (10, 2, "LOSE")])
        df["wager"] = 1.0
        canonical_map = pd.DataFrame({"player_id": [1], "canonical_id": ["P1"]})
        window_end = _BASE + timedelta(minutes=10)
        with self.assertRaises(ValueError) as ctx:
            add_run_state_machine_features(
                df, canonical_map, window_end, lookback_hours=8.0
            )
        self.assertIn("lookback_hours", str(ctx.exception))


class TestAddTrackBRunBeyondCutoff(unittest.TestCase):
    """Rows beyond window_end get zeroed run columns (no NaN)."""

    def test_beyond_cutoff_rows_get_zero_for_run_cols(self) -> None:
        from trainer.trainer import add_run_state_machine_features

        df = _bets([(0, 1, "LOSE"), (10, 2, "LOSE"), (20, 3, "LOSE")])
        df["wager"] = 1.0
        window_end = _BASE + timedelta(minutes=10)
        canonical_map = pd.DataFrame({"player_id": [1], "canonical_id": ["P1"]})
        out = add_run_state_machine_features(
            df, canonical_map, window_end, lookback_hours=None
        )
        beyond = out[out["payout_complete_dtm"] > window_end]
        self.assertGreater(len(beyond), 0)
        self.assertEqual(out.loc[beyond.index, "loss_streak"].iloc[0], 0)
        for col in (
            "run_id",
            "minutes_since_run_start",
            "bets_in_run_so_far",
            "wager_sum_in_run_so_far",
        ):
            self.assertFalse(
                out.loc[beyond.index, col].isna().any(),
                f"{col} beyond cutoff should be 0 not NaN",
            )
            val = out.loc[beyond.index, col].iloc[0]
            self.assertTrue(val == 0 or val == 0.0, f"{col} beyond cutoff should be 0 or 0.0")


class TestLossStreakNoLookbackLargeGroupSmoke(unittest.TestCase):
    """Full-sequence streak without lookback window."""

    def test_compute_loss_streak_none_large_group_smoke(self) -> None:
        n = 500
        rows = [(i, i + 1, "LOSE" if i % 2 == 0 else "WIN") for i in range(n)]
        df = _bets(rows)
        result = compute_loss_streak(df, cutoff_time=None, lookback_hours=None)
        self.assertEqual(len(result), n)
        self.assertTrue((result >= 0).all())


class TestRunBoundaryGamingDayRequired(unittest.TestCase):
    """compute_run_boundary requires parsable gaming_day."""

    def test_missing_gaming_day_raises(self) -> None:
        df = _bets([(0, 1)])
        df = df.drop(columns=["gaming_day"])
        with self.assertRaises(ValueError) as ctx:
            compute_run_boundary(df, cutoff_time=None, lookback_hours=None)
        self.assertIn("gaming_day", str(ctx.exception).lower())


class TestRunBoundaryNoLookbackSemantics(unittest.TestCase):
    """Sanity checks with lookback_hours=None."""

    def test_minutes_since_run_start_non_negative(self) -> None:
        df = _bets([(0, 1), (20, 2), (40, 3)], canonical_id="P1")
        df["wager"] = 1.0
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=None)
        self.assertTrue((result["minutes_since_run_start"] >= 0).all())

    def test_same_day_same_run_increments_bets_in_run(self) -> None:
        df = _bets([(0, 1), (20, 2), (40, 3)], canonical_id="P1")
        df["wager"] = 1.0
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=None)
        self.assertEqual(result["bets_in_run_so_far"].tolist(), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
