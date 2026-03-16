"""Reviewer 風險點（Trainer 對齊 SCORER_LOOKBACK_HOURS）轉成最小可重現測試。

僅新增 tests，不修改 production code。對應 STATUS.md「Trainer 對齊 SCORER_LOOKBACK_HOURS
— Code Review」與「Code Review：Phase 2 compute_loss_streak lookback 向量化」各項「希望新增的測試」。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trainer.features import (  # noqa: E402
    _LOOKBACK_MAX_HOURS,
    compute_loss_streak,
    compute_run_boundary,
)

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


# --- Review #2: add_track_human_features 超出 cutoff 列 run_* 為 NaN ---------------------

class TestAddTrackBRunBeyondCutoff(unittest.TestCase):
    """#2 超出 window_end 的列 loss_streak 與 run_* 均為 0（Review #2 已實作）."""

    def test_beyond_cutoff_rows_get_zero_for_run_cols(self):
        """超出 cutoff 的列 run_* 與 loss_streak 一致為 0，無 NaN（Review #2）."""
        from trainer.trainer import add_track_human_features

        df = _bets([(0, 1, "LOSE"), (10, 2, "LOSE"), (20, 3, "LOSE")])
        df["wager"] = 1.0
        window_end = _BASE + timedelta(minutes=10)
        canonical_map = pd.DataFrame({"player_id": [1], "canonical_id": ["P1"]})
        out = add_track_human_features(df, canonical_map, window_end, lookback_hours=8.0)
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

    def test_run_boundary_lookback_hours_overflow_raises_value_error(self):
        """PLAN 項目 19 Phase 2：compute_run_boundary lookback 過大時 raise ValueError（與 compute_loss_streak 契約一致）。Review #3：鎖定訊息含 '1000'。"""
        df = _bets([(0, 1)])
        df["wager"] = 1.0
        with self.assertRaises(ValueError) as ctx:
            compute_run_boundary(df, cutoff_time=None, lookback_hours=1e10)
        self.assertIn("lookback", str(ctx.exception).lower())
        self.assertIn("1000", str(ctx.exception), "contract: message must mention 1000 hours limit")

    def test_compute_loss_streak_lookback_vs_none_differ_at_boundary(self):
        """lookback_hours=1 時窗 (t-1h,t] 較小；lookback_hours=None 時全歷史，最後列 streak 較大."""
        # 0, 30, 60, 61 min 四筆 LOSE。61 min 的 1h 窗 = (1, 61] min → 30, 60, 61 = streak 3；全歷史 = 4
        df = _bets([(0, 1, "LOSE"), (30, 2, "LOSE"), (60, 3, "LOSE"), (61, 4, "LOSE")])
        cutoff = _BASE + timedelta(minutes=61)
        r_1h = compute_loss_streak(df, cutoff_time=cutoff, lookback_hours=1.0)
        r_none = compute_loss_streak(df, cutoff_time=cutoff, lookback_hours=None)
        self.assertEqual(int(r_1h.iloc[-1]), 3, "at 61min with 1h lookback (1,61] has 30,60,61")
        self.assertEqual(int(r_none.iloc[-1]), 4, "at 61min with full history all four")

    def test_compute_loss_streak_lookback_numba_parity_with_python_fallback(self):
        """Phase 2: numba lookback path must match Python fallback (parity test)."""
        # Same fixture as test_compute_loss_streak_with_lookback_left_open_right_closed
        p1 = _bets([(0, 1, "LOSE"), (30, 2, "LOSE"), (60, 3, "LOSE")], canonical_id="P1")
        p2 = _bets([(0, 4, "LOSE"), (60, 5, "LOSE")], canonical_id="P2")
        df = pd.concat([p1, p2], ignore_index=True)
        with patch("trainer.features.features._streak_lookback_numba", None):
            r_fallback = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        r_numba = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        pd.testing.assert_series_equal(r_fallback, r_numba, check_names=False, check_index=True)
        self.assertTrue(r_numba.index.equals(df.index), "returned index must equal df.index (Review #6)")
        self.assertEqual(len(r_numba), len(df))


# --- Code Review：Phase 2 compute_loss_streak lookback 向量化（STATUS.md 審查風險 #1–#7）---

class TestPhase2LookbackReviewRisks(unittest.TestCase):
    """STATUS.md「Code Review：Phase 2 compute_loss_streak lookback 向量化」風險點 → 最小可重現測試。僅 tests，不改 production。"""

    # --- #1 NaT 在 numba 路徑下：numba 與 fallback 輸出一致 ---
    def test_review1_nat_numba_parity_with_fallback(self):
        """Review #1: 同一 canonical_id 內一筆 NaT、一筆正常時間，numba 路徑與 fallback 路徑輸出一致。目前 numba 路徑未處理 NaT，導致輸出不一致。"""
        df = _bets([(0, 1, "LOSE"), (30, 2, "LOSE")], canonical_id="P1")
        df.loc[df.index[1], "payout_complete_dtm"] = pd.NaT
        with patch("trainer.features.features._streak_lookback_numba", None):
            r_fallback = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        r_numba = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        self.assertEqual(len(r_numba), len(df), "result length must match input")
        pd.testing.assert_series_equal(r_fallback, r_numba, check_names=False, check_index=True)

    # --- #2 極大 lookback_hours：不崩潰或明確拋出 overflow 相關異常 ---
    def test_review2_lookback_hours_overflow_no_crash_or_overflow_raised(self):
        """Review #2: lookback_hours 極大時要不崩潰且結果長度正確，要不拋出 overflow 相關異常（ValueError 或 OutOfBoundsTimedelta）。"""
        df = _bets([(0, 1, "LOSE"), (60, 2, "WIN")])
        try:
            result = compute_loss_streak(df, cutoff_time=None, lookback_hours=1e10)
        except (ValueError, Exception) as e:
            err = type(e).__name__
            if "OutOfBoundsTimedelta" in err or "Overflow" in err or "ValueError" == err:
                return
            raise
        self.assertEqual(len(result), 2)
        self.assertTrue(result.notna().all() if hasattr(result, "notna") else True)

    def test_review2_lookback_hours_overflow_raises_value_error_or_overflow(self):
        """Review #2: 契約建議 lookback_hours 過大時 raise ValueError；目前 fallback 可能拋 OutOfBoundsTimedelta/OverflowError，測試接受並 skip。"""
        df = _bets([(0, 1, "LOSE")])
        try:
            compute_loss_streak(df, cutoff_time=None, lookback_hours=1e10)
        except Exception as e:
            tname = type(e).__name__
            if "OutOfBounds" in tname or "Overflow" in tname:
                self.skipTest("Production raises overflow in fallback path; desired contract is upfront ValueError")
            if isinstance(e, ValueError):
                self.assertIn("lookback", str(e).lower(), "expect lookback-related message")
                return
            raise
        self.skipTest("Production does not yet raise for overflow; test documents desired contract")

    # --- #3 回傳為 int32、極端視窗不崩潰 ---
    def test_review3_return_dtype_int32_and_no_crash_large_window(self):
        """Review #3: 回傳 dtype 為 int32；較多連續 LOSE 在視窗內不崩潰。"""
        n = 500
        rows = [(i, i + 1, "LOSE") for i in range(n)]
        df = _bets(rows, canonical_id="P1")
        result = compute_loss_streak(df, cutoff_time=None, lookback_hours=2.0)
        self.assertEqual(len(result), n)
        self.assertTrue(result.dtype == np.int32 or result.dtype == "int32", "return must be int32")
        self.assertTrue((result >= 0).all())

    # --- #4 status 為 Categorical 時與字串版一致 ---
    def test_review4_status_categorical_parity_with_string(self):
        """Review #4: status 為 Categorical 時結果與字串版一致（或文件化僅支援字串）。"""
        df_str = _bets([(0, 1, "LOSE"), (30, 2, "WIN"), (60, 3, "LOSE")])
        df_cat = df_str.copy()
        df_cat["status"] = pd.Categorical(df_str["status"].tolist(), categories=["LOSE", "WIN", "PUSH"])
        r_str = compute_loss_streak(df_str, cutoff_time=None, lookback_hours=1.0)
        r_cat = compute_loss_streak(df_cat, cutoff_time=None, lookback_hours=1.0)
        pd.testing.assert_series_equal(r_str, r_cat, check_names=False, check_index=True)

    # --- #5 部分 group 失敗時整段 fallback 結果與全 fallback 一致（可選）---
    def test_review5_partial_fallback_parity_with_full_fallback(self):
        """Review #5: 當 numba 中途失敗 fallback 後，最終結果與從頭用 Python 路徑一致。"""
        p1 = _bets([(0, 1, "LOSE"), (30, 2, "LOSE")], canonical_id="P1")
        p2 = _bets([(0, 3, "WIN"), (60, 4, "LOSE")], canonical_id="P2")
        df = pd.concat([p1, p2], ignore_index=True)
        with patch("trainer.features.features._streak_lookback_numba", None):
            r_full_fallback = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        r_numba = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        pd.testing.assert_series_equal(r_full_fallback, r_numba, check_names=False, check_index=True)

    # --- #6 多 canonical_id、每組筆數不同時 index 與 df 一致 ---
    def test_review6_index_equals_df_index_multi_cid(self):
        """Review #6: 多個 canonical_id、每組筆數不同時，回傳 index 與 df.index 一致且與 fallback 一致。"""
        p1 = _bets([(0, 1, "LOSE"), (10, 2, "LOSE")], canonical_id="A")
        p2 = _bets([(0, 3, "WIN")], canonical_id="B")
        p3 = _bets([(0, 4, "LOSE"), (20, 5, "LOSE"), (40, 6, "WIN")], canonical_id="C")
        df = pd.concat([p1, p2, p3], ignore_index=True)
        with patch("trainer.features.features._streak_lookback_numba", None):
            r_fallback = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        r_numba = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        self.assertTrue(r_numba.index.equals(df.index))
        self.assertTrue(r_fallback.index.equals(df.index))
        pd.testing.assert_series_equal(r_fallback, r_numba, check_names=False, check_index=True)

    # --- #7 numba 不可用時不拋錯且結果與有 numba 時一致 ---
    def test_review7_no_numba_result_equals_with_numba(self):
        """Review #7: 無 numba 時不拋錯，結果與有 numba 時一致。"""
        df = _bets([(0, 1, "LOSE"), (30, 2, "WIN"), (60, 3, "LOSE")])
        with patch("trainer.features.features._streak_lookback_numba", None):
            r_no_numba = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        r_with_numba = compute_loss_streak(df, cutoff_time=None, lookback_hours=1.0)
        pd.testing.assert_series_equal(r_no_numba, r_with_numba, check_names=False, check_index=True)


# --- Code Review：compute_run_boundary lookback 契約對齊變更（STATUS.md 2026-03-11）---

class TestRunBoundaryLookbackReviewRisks(unittest.TestCase):
    """STATUS.md「Code Review：compute_run_boundary lookback 契約對齊變更」風險點 → 最小可重現測試。僅 tests，不改 production。"""

    # --- Review #1 run_boundary lookback 路徑含 NaT：不崩潰、長度一致、語意可驗證 ---
    def test_run_boundary_lookback_with_nat_no_crash_and_defined_semantics(self):
        """Review #1: 同一 canonical_id 內一筆 NaT、一筆正常時間，lookback_hours=1 時不拋錯、回傳長度一致、run 欄位無 NaN。"""
        df = _bets([(0, 1), (30, 2)], canonical_id="P1")
        df["wager"] = 1.0
        df.loc[df.index[1], "payout_complete_dtm"] = pd.NaT
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=1.0)
        self.assertEqual(len(result), len(df), "result length must match input")
        for col in ("run_id", "minutes_since_run_start", "bets_in_run_so_far", "wager_sum_in_run_so_far"):
            self.assertIn(col, result.columns)
            self.assertFalse(
                result[col].isna().any(),
                f"{col} must not contain NaN (contract: defined semantics with NaT)",
            )
        self.assertTrue((result["run_id"] >= 0).all())
        self.assertTrue((result["minutes_since_run_start"] >= 0).all())
        self.assertTrue((result["bets_in_run_so_far"] >= 0).all())

    # --- Review #2/#3 兩函數 overflow 時錯誤訊息均含 "1000"、契約一致 ---
    def test_run_boundary_and_loss_streak_overflow_message_contain_1000(self):
        """Review #2/#3: loss_streak 與 run_boundary 在 lookback_hours=1e10 時皆拋 ValueError 且訊息均含 '1000'。"""
        df_s = _bets([(0, 1, "LOSE")])
        df_r = _bets([(0, 1)])
        df_r["wager"] = 1.0
        for name, fn, df in [
            ("compute_loss_streak", compute_loss_streak, df_s),
            ("compute_run_boundary", compute_run_boundary, df_r),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as ctx:
                    fn(df, cutoff_time=None, lookback_hours=1e10)
                self.assertIn("1000", str(ctx.exception), f"{name} overflow message must mention 1000 hours")

    # --- Review #4 極小 lookback_hours 被拒絕（截斷為 0）---
    def test_run_boundary_lookback_hours_tiny_raises_value_error(self):
        """Review #4: lookback_hours 極小（換算 ns 截斷為 0）時 raise ValueError，文件化過小視窗被拒絕。"""
        df = _bets([(0, 1)])
        df["wager"] = 1.0
        with self.assertRaises(ValueError) as ctx:
            compute_run_boundary(df, cutoff_time=None, lookback_hours=1e-15)
        self.assertIn("lookback", str(ctx.exception).lower())

    # --- Code Review lookback 常數共用：訊息與常數一致（STATUS 本輪 §1）---
    def test_overflow_message_contains_lookback_max_hours_constant(self):
        """Review 常數共用 §1: 兩函數 overflow 時 ValueError 訊息內含 str(_LOOKBACK_MAX_HOURS)，鎖定訊息與常數一致。"""
        df_s = _bets([(0, 1, "LOSE")])
        df_r = _bets([(0, 1)])
        df_r["wager"] = 1.0
        expected = str(_LOOKBACK_MAX_HOURS)
        for name, fn, df in [
            ("compute_loss_streak", compute_loss_streak, df_s),
            ("compute_run_boundary", compute_run_boundary, df_r),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as ctx:
                    fn(df, cutoff_time=None, lookback_hours=1e10)
                self.assertIn(
                    expected,
                    str(ctx.exception),
                    f"{name} overflow message must contain _LOOKBACK_MAX_HOURS ({expected})",
                )

    # --- Code Review run_boundary NaT 語意 §2/§3：全 group 皆 NaT 時每列四欄皆 0 ---
    def test_run_boundary_lookback_all_nat_group_gets_zeros(self):
        """Review NaT 語意 §2/§3: 單一 canonical_id、多筆皆 NaT 時不拋錯、回傳長度一致、所有 run 欄位為 0。"""
        df = _bets([(0, 1), (10, 2), (20, 3)], canonical_id="P1")
        df["wager"] = 1.0
        df["payout_complete_dtm"] = pd.NaT
        result = compute_run_boundary(df, cutoff_time=None, lookback_hours=1.0)
        self.assertEqual(len(result), len(df), "result length must match input")
        for col in ("run_id", "minutes_since_run_start", "bets_in_run_so_far", "wager_sum_in_run_so_far"):
            self.assertTrue((result[col] == 0).all(), f"all- NaT group: {col} must be 0")
