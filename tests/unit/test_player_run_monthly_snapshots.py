"""Unit tests for Section A monthly snapshot bookkeeping in ``player_run_layer``."""
from __future__ import annotations

import unittest

import pandas as pd

from trainer.features import player_run_layer as prl


class TestMonthlySnapshotLogic(unittest.TestCase):
    """PIT scaffolding for monthly grids and merge."""

    def test_section_a_subset_detected(self):
        subset = {"player_run_count_30d"}
        self.assertTrue(prl._monthly_snapshot_mode_requested(subset))

    def test_non_section_a_turns_off_monthly_gate(self):
        wanted = {"player_run_count_30d", "player_run_theo_sum_30d"}
        self.assertFalse(prl._monthly_snapshot_mode_requested(wanted))

    def test_snap_grid_covers_calendar_month_bounds(self):
        evt = pd.DataFrame(
            {
                "canonical_id": ["x", "x"],
                "t": pd.to_datetime(["2024-01-15", "2024-03-20"]),
            }
        )
        grid = prl._build_monthly_snap_event_table(evt)
        mons = {pd.Timestamp(ts).month for ts in grid["snapshot_ts"].tolist()}
        self.assertEqual(mons, {1, 2, 3})
        self.assertEqual(len(grid), 3)

    def test_merge_asof_backward_attaches_snapshot(self):
        labeled_evt = pd.DataFrame(
            {
                "bet_id": ["b1"],
                "canonical_id": ["c"],
                "t": pd.to_datetime(["2024-02-15"]),
                "payout_complete_dtm": pd.to_datetime(["2024-02-15"]),
            }
        )
        feats = pd.DataFrame(
            {
                "canonical_id": ["c"],
                "snapshot_ts": pd.to_datetime(["2024-02-01"]),
                "player_run_count_30d": [42.0],
            }
        )
        merged = prl._merge_snapshot_features_onto_labeled(labeled_evt, feats)
        self.assertAlmostEqual(float(merged["player_run_count_30d"].iloc[0]), 42.0)

    def test_merge_asof_when_bet_times_not_globally_sorted(self):
        """Pandas ``merge_asof(..., by=...)`` still validates ``on`` globally in some builds."""
        labeled_evt = pd.DataFrame(
            {
                "bet_id": ["b later", "b earlier"],
                "canonical_id": ["patron_a", "patron_b"],
                "t": pd.to_datetime(["2024-03-15", "2024-01-15"]),
            }
        )
        feats = pd.DataFrame(
            {
                "canonical_id": ["patron_a", "patron_a", "patron_b"],
                "snapshot_ts": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-01-01"]),
                "player_run_count_30d": [1.0, 2.0, 9.0],
            }
        )
        merged = prl._merge_snapshot_features_onto_labeled(labeled_evt, feats)
        by_bid = merged.set_index("bet_id")["player_run_count_30d"].to_dict()
        self.assertAlmostEqual(float(by_bid["b later"]), 2.0)
        self.assertAlmostEqual(float(by_bid["b earlier"]), 9.0)


if __name__ == "__main__":
    unittest.main()
