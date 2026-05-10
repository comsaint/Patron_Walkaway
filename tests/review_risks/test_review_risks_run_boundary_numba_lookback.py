"""compute_run_boundary regression tests (run-day + gap; no lookback window).

Legacy filename references numba lookback path, which was removed. These tests lock
wager NaN handling, non-negative minutes, and basic multi-row semantics.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trainer.features import compute_run_boundary  # noqa: E402

_BASE = datetime(2025, 1, 1)


def _bets(rows, canonical_id="P1", with_wager=True):
    """Minimal bets DataFrame: list of (offset_min, bet_id); optional wager column."""
    records = []
    for i, (offset_min, bid) in enumerate(rows):
        pcd = _BASE + timedelta(minutes=offset_min)
        rec = {
            "canonical_id": canonical_id,
            "bet_id": bid,
            "payout_complete_dtm": pcd,
            "gaming_day": pcd.date(),
        }
        if with_wager:
            rec["wager"] = 100.0 * (i + 1)
        records.append(rec)
    df = pd.DataFrame(records)
    if with_wager:
        assert "wager" in df.columns
    return df


class TestRunBoundaryWagerNanContract(unittest.TestCase):
    """wager 含 NaN 時 wager_sum_in_run_so_far 仍以缺失為 0 累加。"""

    def test_wager_nan_row_gets_finite_wager_sum_in_run_so_far(self) -> None:
        df = _bets([(0, 1), (30, 2), (45, 3)], canonical_id="P1")
        df.loc[df.index[1], "wager"] = np.nan
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=None)
        self.assertEqual(len(result), len(df))
        self.assertFalse(
            result["wager_sum_in_run_so_far"].isna().any(),
            "wager Sum must not contain NaN when wager has NaN (missing treated as 0)",
        )


class TestRunBoundaryMinutesSinceRunStartNonNegative(unittest.TestCase):
    """minutes_since_run_start ≥ 0 with lookback_hours=None."""

    def test_no_lookback_path_minutes_since_run_start_non_negative(self) -> None:
        df = _bets([(0, 1), (20, 2), (50, 3)], canonical_id="P1")
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=None)
        self.assertIn("minutes_since_run_start", result.columns)
        min_val = result["minutes_since_run_start"].min()
        self.assertGreaterEqual(min_val, 0.0)


class TestRunBoundaryNoWagerColumn(unittest.TestCase):
    """無 wager 欄時 wager_sum_in_run_so_far 為 0。"""

    def test_no_wager_column_zeros(self) -> None:
        df = _bets([(0, 1), (15, 2)], with_wager=False)
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=None)
        self.assertTrue((result["wager_sum_in_run_so_far"] == 0).all())


class TestDuckDbRunBoundaryParity(unittest.TestCase):
    """DuckDB window path should match pandas :func:`compute_run_boundary`."""

    def test_duckdb_matches_pandas_multi_scenario(self) -> None:
        from trainer.features import compute_run_boundary, compute_run_boundary_duckdb, run_boundary_frames_close

        frames = [
            _bets([(0, 1), (30, 2), (90, 3)], canonical_id="P1"),
            _bets([(0, 1), (15, 2)], canonical_id="P2", with_wager=False),
        ]
        p2 = _BASE + timedelta(minutes=120)
        frames.append(
            pd.DataFrame(
                [
                    {
                        "canonical_id": "PX",
                        "bet_id": 9,
                        "payout_complete_dtm": p2,
                        "gaming_day": p2.date(),
                        "wager": 50.0,
                        "casino_win": 1.0,
                    }
                ]
            )
        )
        for df in frames:
            with self.subTest(rows=len(df)):
                pan = compute_run_boundary(df, cutoff_time=None, lookback_hours=None)
                ddb = compute_run_boundary_duckdb(df, cutoff_time=None, lookback_hours=None)
                self.assertTrue(
                    run_boundary_frames_close(pan, ddb),
                    f"pandas vs duckdb mismatch on {len(df)} rows",
                )

    def test_duckdb_matches_pandas_with_cutoff(self) -> None:
        from trainer.features import compute_run_boundary, compute_run_boundary_duckdb, run_boundary_frames_close

        df1 = _bets([(0, 1), (20, 2), (60, 3)], canonical_id="C1")
        df2 = _bets([(0, 10), (25, 11)], canonical_id="C2")
        df = pd.concat([df1, df2], ignore_index=True)
        cut = _BASE + timedelta(minutes=40)
        pan = compute_run_boundary(df, cutoff_time=cut, lookback_hours=None)
        ddb = compute_run_boundary_duckdb(df, cutoff_time=cut, lookback_hours=None)
        self.assertTrue(run_boundary_frames_close(pan, ddb))


class TestRunBoundaryLookbackRejected(unittest.TestCase):
    """lookback_hours must be None."""

    def test_lookback_rejected(self) -> None:
        df = _bets([(0, 1)], with_wager=True)
        with self.assertRaises(ValueError) as ctx:
            compute_run_boundary(df, cutoff_time=None, lookback_hours=2.0)
        self.assertIn("lookback_hours", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
