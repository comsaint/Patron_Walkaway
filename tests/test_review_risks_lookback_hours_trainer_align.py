"""Reviewer 風險點（Trainer 對齊 SCORER_LOOKBACK_HOURS）轉成最小可重現測試。

僅新增 tests，不修改 production code。對應 STATUS.md「Trainer 對齊 SCORER_LOOKBACK_HOURS
— Code Review」各項「希望新增的測試」。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trainer.features import compute_loss_streak, compute_run_boundary  # noqa: E402

try:
    from trainer.config import RUN_BREAK_MIN
except Exception:
    RUN_BREAK_MIN = 30

_BASE = datetime(2025, 1, 1)


def _bets(rows, canonical_id="P1", table_id="T1", player_id=1):
    """Minimal bets df from (offset_min, bet_id, status?) tuples."""
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


# --- Review #1: lookback_hours ≤ 0 靜默走向量化路徑 ---------------------------------

class TestLookbackHoursZeroOrNegativeReproduceRisk(unittest.TestCase):
    """#1 重現風險：lookback_hours=0 或負數時目前走全歷史，與 scorer 視窗不一致."""

    def test_loss_streak_lookback_hours_zero_raises(self):
        """lookback_hours=0 應 raise ValueError（Review #1 已實作）."""
        df = _bets([(0, 1, "LOSE")])
        with self.assertRaises(ValueError) as ctx:
            compute_loss_streak(df, cutoff_time=None, lookback_hours=0)
        self.assertIn("lookback_hours", str(ctx.exception))

    def test_run_boundary_lookback_hours_zero_raises(self):
        """lookback_hours=0 應 raise ValueError（Review #1 已實作）."""
        df = _bets([(0, 1)])
        with self.assertRaises(ValueError) as ctx:
            compute_run_boundary(df, cutoff_time=None, lookback_hours=0)
        self.assertIn("lookback_hours", str(ctx.exception))


# --- Review #2: add_track_b_features 超出 cutoff 列 run_* 為 NaN ---------------------

class TestAddTrackBRunBeyondCutoff(unittest.TestCase):
    """#2 超出 window_end 的列 loss_streak 與 run_* 均為 0（Review #2 已實作）."""

    def test_beyond_cutoff_rows_get_zero_for_run_cols(self):
        """超出 cutoff 的列 run_* 與 loss_streak 一致為 0，無 NaN（Review #2）."""
        from trainer.trainer import add_track_b_features

        df = _bets([(0, 1, "LOSE"), (10, 2, "LOSE"), (20, 3, "LOSE")])
        df["wager"] = 1.0
        window_end = _BASE + timedelta(minutes=10)
        canonical_map = pd.DataFrame({"player_id": [1], "canonical_id": ["P1"]})
        out = add_track_b_features(df, canonical_map, window_end, lookback_hours=8.0)
        beyond = out[out["payout_complete_dtm"] > window_end]
        self.assertGreater(len(beyond), 0)
        self.assertEqual(out.loc[beyond.index, "loss_streak"].iloc[0], 0)
        for col in ("run_id", "minutes_since_run_start", "bets_in_run_so_far", "wager_sum_in_run_so_far"):
            self.assertFalse(
                out.loc[beyond.index, col].isna().any(),
                f"{col} beyond cutoff should be 0 not NaN",
            )
            val = out.loc[beyond.index, col].iloc[0]
            self.assertTrue(val == 0 or val == 0.0, f"{col} beyond cutoff should be 0 or 0.0")


# --- Review #3（可選）: 大組 smoke ----------------------------------------------------

class TestLookbackLargeGroupSmoke(unittest.TestCase):
    """#3 可選：lookback 路徑大組不崩潰、結果形狀合理."""

    def test_compute_loss_streak_lookback_large_group_smoke(self):
        """單一 canonical_id、約 500 列、lookback_hours=2，不崩潰且結果長度正確."""
        n = 500
        rows = [(i, i + 1, "LOSE" if i % 2 == 0 else "WIN") for i in range(n)]
        df = _bets(rows)
        result = compute_loss_streak(df, cutoff_time=None, lookback_hours=2.0)
        self.assertEqual(len(result), n)
        self.assertTrue((result >= 0).all())
        self.assertTrue((result <= 120).all(), "streak in 2h window at most ~120 bets at 1/min")


# --- Review #4（可選）: lookback_hours 極大 --------------------------------------------------

class TestLookbackHoursLarge(unittest.TestCase):
    """#4 可選：lookback_hours 極大不崩潰."""

    def test_compute_loss_streak_lookback_hours_large_no_crash(self):
        """lookback_hours=1e3，小 DataFrame，不崩潰且結果有限."""
        df = _bets([(0, 1, "LOSE"), (60, 2, "LOSE")])
        result = compute_loss_streak(df, cutoff_time=None, lookback_hours=1000.0)
        self.assertEqual(len(result), 2)
        self.assertTrue(result.notna().all())
        self.assertTrue((result >= 0).all())


# --- Review #5（可選）: NaT 在 lookback 路徑 --------------------------------------------------

class TestLookbackWithNaT(unittest.TestCase):
    """#5 可選：payout_complete_dtm 含 NaT 時 lookback 路徑不崩潰."""

    def test_lookback_with_nat_does_not_crash(self):
        """一列 NaT，lookback_hours=1，不崩潰；結果長度與輸入一致."""
        df = _bets([(0, 1, "LOSE"), (30, 2, "LOSE")])
        df.loc[df.index[1], "payout_complete_dtm"] = pd.NaT
        result = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        self.assertEqual(len(result), 2)
        self.assertTrue((result >= 0).all() if result.dtype != "object" else True)


# --- Review #6: lookback 路徑語義（左開右閉、canonical_id 隔離、lookback vs None） ----

class TestLookbackPathSemantics(unittest.TestCase):
    """#6 lookback 路徑專用：左開右閉 (t-h, t]、canonical_id 隔離、與 None 邊界差異."""

    def test_compute_loss_streak_with_lookback_left_open_right_closed(self):
        """lookback_hours=1，邊界 (t-1h, t] 左開右閉與 canonical_id 隔離."""
        # P1: 0, 30, 60 min。窗 (t-1h,t]：0→(-60,0] 僅自己=1；30→(-30,30] 含 0,30=2；60→(0,60] 含 30,60=2（0 在左開外）
        p1 = _bets([(0, 1, "LOSE"), (30, 2, "LOSE"), (60, 3, "LOSE")], canonical_id="P1")
        # P2: 0, 60 min。0→(-60,0] 僅自己=1；60→(0,60] 僅 60（0 在左開外）=1
        p2 = _bets([(0, 4, "LOSE"), (60, 5, "LOSE")], canonical_id="P2")
        df = pd.concat([p1, p2], ignore_index=True)
        result = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        by_bid = dict(zip(df["bet_id"], result.values))
        self.assertEqual(by_bid[1], 1)
        self.assertEqual(by_bid[2], 2)
        self.assertEqual(by_bid[3], 2)
        self.assertEqual(by_bid[4], 1)
        self.assertEqual(by_bid[5], 1)

    def test_compute_run_boundary_with_lookback_same_semantics(self):
        """lookback_hours=1，run_id / minutes_since_run_start / bets_in_run 在窗內一致."""
        df = _bets([(0, 1), (20, 2), (40, 3)], canonical_id="P1")
        df["wager"] = 1.0
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=1.0)
        self.assertEqual(len(result), 3)
        self.assertIn("run_id", result.columns)
        self.assertIn("minutes_since_run_start", result.columns)
        self.assertIn("bets_in_run_so_far", result.columns)
        self.assertIn("wager_sum_in_run_so_far", result.columns)
        self.assertTrue((result["run_id"] >= 0).all())
        self.assertTrue((result["minutes_since_run_start"] >= 0).all())
        self.assertTrue((result["bets_in_run_so_far"] >= 0).all())
        # 同一 run（gap 20 < RUN_BREAK_MIN）：bets_in_run 1, 2, 3
        self.assertEqual(result["bets_in_run_so_far"].tolist(), [1, 2, 3])

    def test_compute_loss_streak_lookback_vs_none_differ_at_boundary(self):
        """lookback_hours=1 時窗 (t-1h,t] 較小；lookback_hours=None 時全歷史，最後列 streak 較大."""
        # 0, 30, 60, 61 min 四筆 LOSE。61 min 的 1h 窗 = (1, 61] min → 30, 60, 61 = streak 3；全歷史 = 4
        df = _bets([(0, 1, "LOSE"), (30, 2, "LOSE"), (60, 3, "LOSE"), (61, 4, "LOSE")])
        cutoff = _BASE + timedelta(minutes=61)
        r_1h = compute_loss_streak(df, cutoff_time=cutoff, lookback_hours=1.0)
        r_none = compute_loss_streak(df, cutoff_time=cutoff, lookback_hours=None)
        self.assertEqual(int(r_1h.iloc[-1]), 3, "at 61min with 1h lookback (1,61] has 30,60,61")
        self.assertEqual(int(r_none.iloc[-1]), 4, "at 61min with full history all four")
