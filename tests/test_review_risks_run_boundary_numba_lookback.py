"""Code Review：Phase 2 compute_run_boundary lookback numba 向量化 — 風險點轉成最小可重現測試。

對應 STATUS.md「Code Review：Phase 2 compute_run_boundary lookback numba 向量化（2026-03-11）」各項
「希望新增的測試」。僅新增 tests，不修改 production code。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trainer.features import compute_run_boundary  # noqa: E402

_BASE = datetime(2025, 1, 1)


def _bets(rows, canonical_id="P1", with_wager=True):
    """Minimal bets DataFrame: list of (offset_min, bet_id); optional wager column."""
    records = []
    for i, (offset_min, bid) in enumerate(rows):
        rec = {
            "canonical_id": canonical_id,
            "bet_id": bid,
            "payout_complete_dtm": _BASE + timedelta(minutes=offset_min),
        }
        if with_wager:
            rec["wager"] = 100.0 * (i + 1)
        records.append(rec)
    df = pd.DataFrame(records)
    if with_wager:
        assert "wager" in df.columns
    return df


# --- Review #1: wager 欄位含 NaN 時，numba 路徑會傳播 NaN ---------------------------------

class TestRunBoundaryLookbackWagerNanContract(unittest.TestCase):
    """STATUS Code Review #1：wager 含 NaN 時契約為 wager_sum_in_run_so_far 不為 NaN（視缺失為 0）。"""

    def test_wager_nan_row_gets_finite_wager_sum_in_run_so_far(self):
        """單一 canonical_id、lookback_hours=1、多筆 bet 其中一筆 wager=NaN；契約：該列 wager_sum_in_run_so_far 不為 NaN。"""
        df = _bets([(0, 1), (30, 2), (45, 3)], canonical_id="P1")
        df.loc[df.index[1], "wager"] = np.nan
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=1.0)
        self.assertEqual(len(result), len(df))
        self.assertFalse(
            result["wager_sum_in_run_so_far"].isna().any(),
            "wager_sum_in_run_so_far must not contain NaN when wager has NaN (contract: treat missing as 0)",
        )

    def test_wager_finite_numba_vs_python_fallback_parity(self):
        """同一輸入、wager 全為有限值時，numba 路徑與 Python fallback 四欄一致。"""
        df = _bets([(0, 1), (30, 2), (45, 3)], canonical_id="P1")
        with patch("trainer.features.features._run_boundary_lookback_numba", None):
            result_fallback = compute_run_boundary(df, cutoff_time=None, lookback_hours=1.0)
        result_numba = compute_run_boundary(df, cutoff_time=None, lookback_hours=1.0)
        for col in ("run_id", "minutes_since_run_start", "bets_in_run_so_far", "wager_sum_in_run_so_far"):
            self.assertIn(col, result_numba.columns)
            if result_fallback[col].dtype.kind == "f":
                np.testing.assert_allclose(
                    result_fallback[col].values,
                    result_numba[col].values,
                    rtol=1e-9,
                    equal_nan=True,
                    err_msg=f"{col} numba vs fallback",
                )
            else:
                pd.testing.assert_series_equal(
                    result_fallback[col],
                    result_numba[col],
                    check_names=False,
                    check_index=True,
                )


# --- Review #2: run_break_min_ns 過大時可能造成 int64 溢出 ---------------------------------

class TestRunBoundaryLookbackRunBreakMinOverflowContract(unittest.TestCase):
    """STATUS Code Review #2：RUN_BREAK_MIN 極大時契約為 raise ValueError 且訊息提及 RUN_BREAK_MIN 或範圍。"""

    def test_run_break_min_huge_raises_value_error(self):
        """RUN_BREAK_MIN 設為極大值時，compute_run_boundary(..., lookback_hours=8) 應拋 ValueError 且訊息提及 RUN_BREAK_MIN 或範圍。"""
        df = _bets([(0, 1)], with_wager=True)
        with patch("trainer.features.features.RUN_BREAK_MIN", 1e10):  # 2.2: 實作在 features.features
            with self.assertRaises(ValueError) as ctx:
                compute_run_boundary(df, cutoff_time=None, lookback_hours=8.0)
        msg = str(ctx.exception).lower()
        self.assertTrue(
            "run_break" in msg or "run_break_min" in msg or "overflow" in msg or "range" in msg or "10000" in msg,
            f"ValueError message should mention RUN_BREAK_MIN or range: {ctx.exception}",
        )


# --- Review #3: numba 與 Python lookback 路徑逐 row 對照 ---------------------------------

class TestRunBoundaryLookbackNumbaVsPythonParity(unittest.TestCase):
    """STATUS Code Review #3：同一輸入下 numba 路徑與 Python fallback 四欄逐列一致。"""

    def _assert_four_columns_equal(self, a: pd.DataFrame, b: pd.DataFrame, msg: str = ""):
        for col in ("run_id", "minutes_since_run_start", "bets_in_run_so_far", "wager_sum_in_run_so_far"):
            self.assertIn(col, a.columns, f"missing {col} in A {msg}")
            self.assertIn(col, b.columns, f"missing {col} in B {msg}")
            if a[col].dtype.kind == "f" or b[col].dtype.kind == "f":
                np.testing.assert_allclose(
                    a[col].values,
                    b[col].values,
                    rtol=1e-9,
                    equal_nan=True,
                    err_msg=f"{col} {msg}",
                )
            else:
                pd.testing.assert_series_equal(a[col], b[col], check_names=False, check_index=True)

    def test_single_group_with_new_run_and_same_run_parity(self):
        """單一 canonical_id、多筆 bet、含 wager、間隔涵蓋新 run 與同 run；numba 與 fallback 四欄一致。"""
        # 0, 20, 40, 100 min → gap 20,20,60；若 RUN_BREAK_MIN=30 則 run: 0,0,0,1
        df = _bets([(0, 1), (20, 2), (40, 3), (100, 4)], canonical_id="P1")
        with patch("trainer.features.features._run_boundary_lookback_numba", None):
            result_fallback = compute_run_boundary(df, cutoff_time=None, lookback_hours=2.0)
        result_numba = compute_run_boundary(df, cutoff_time=None, lookback_hours=2.0)
        self._assert_four_columns_equal(result_fallback, result_numba, "single group 2h lookback")

    def test_two_groups_parity(self):
        """兩 canonical_id、不同筆數；numba 與 fallback 四欄一致。"""
        p1 = _bets([(0, 1), (10, 2), (20, 3)], canonical_id="P1")
        p2 = _bets([(0, 4), (60, 5)], canonical_id="P2")
        df = pd.concat([p1, p2], ignore_index=True)
        with patch("trainer.features.features._run_boundary_lookback_numba", None):
            result_fallback = compute_run_boundary(df, cutoff_time=None, lookback_hours=2.0)
        result_numba = compute_run_boundary(df, cutoff_time=None, lookback_hours=2.0)
        self._assert_four_columns_equal(result_fallback, result_numba, "two groups 2h lookback")

    def test_no_wager_column_parity(self):
        """無 wager 欄時 numba 與 fallback 四欄一致（wager_sum_in_run_so_far 皆 0）。"""
        df = _bets([(0, 1), (15, 2)], with_wager=False)
        with patch("trainer.features.features._run_boundary_lookback_numba", None):
            result_fallback = compute_run_boundary(df, cutoff_time=None, lookback_hours=1.0)
        result_numba = compute_run_boundary(df, cutoff_time=None, lookback_hours=1.0)
        self._assert_four_columns_equal(result_fallback, result_numba, "no wager column")
        self.assertTrue((result_numba["wager_sum_in_run_so_far"] == 0).all())


# --- Review #6: minutes_since_run_start ≥ 0 ---------------------------------

class TestRunBoundaryMinutesSinceRunStartNonNegative(unittest.TestCase):
    """STATUS Code Review #6：契約 minutes_since_run_start ≥ 0；lookback 與非 lookback 皆測。"""

    def test_lookback_path_minutes_since_run_start_non_negative(self):
        """lookback_hours 設定時，整欄 minutes_since_run_start 最小值 ≥ 0。"""
        df = _bets([(0, 1), (20, 2), (50, 3), (90, 4)], canonical_id="P1")
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=2.0)
        self.assertIn("minutes_since_run_start", result.columns)
        min_val = result["minutes_since_run_start"].min()
        self.assertGreaterEqual(
            min_val,
            0.0,
            "minutes_since_run_start must be >= 0 (spec §3.3)",
        )

    def test_no_lookback_path_minutes_since_run_start_non_negative(self):
        """lookback_hours=None 時，整欄 minutes_since_run_start 最小值 ≥ 0。"""
        df = _bets([(0, 1), (20, 2), (50, 3)], canonical_id="P1")
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=None)
        self.assertIn("minutes_since_run_start", result.columns)
        min_val = result["minutes_since_run_start"].min()
        self.assertGreaterEqual(
            min_val,
            0.0,
            "minutes_since_run_start must be >= 0 (spec §3.3)",
        )


if __name__ == "__main__":
    unittest.main()
