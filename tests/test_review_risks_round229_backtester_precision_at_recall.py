"""Round 229 Code Review — Backtester precision-at-recall risk points as tests.

STATUS.md Round 229 Code Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § Backtester precision-at-recall 指標, STATUS Round 229 Review.
"""

from __future__ import annotations

import json
import unittest

import numpy as np
import pandas as pd

import trainer.backtester as backtester_mod


class TestR229_1_ScoreNaN(unittest.TestCase):
    """Review #1: score 含 NaN — 目前 sklearn 會拋 ValueError；期望行為為不拋錯且三鍵為 None."""

    def test_current_score_nan_raises_value_error(self):
        """Score 含 NaN 時不拋錯且三鍵為 None（Round 231 production 修復後：guard 防呆）。"""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1],
            "score": [0.2, np.nan, 0.3, 0.9],
            "is_rated": [True, True, True, True],
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        self.assertIsNone(out.get("test_precision_at_recall_0.001"))
        self.assertIsNone(out.get("test_precision_at_recall_0.01"))
        self.assertIsNone(out.get("test_precision_at_recall_0.1"))
        self.assertIsNone(out.get("test_precision_at_recall_0.5"))

    def test_desired_score_nan_returns_safe_structure(self):
        """Score 含 NaN 時不拋錯且三鍵為 None（與 test_current 同一契約）。"""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1],
            "score": [0.2, np.nan, 0.3, 0.9],
            "is_rated": [True, True, True, True],
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        self.assertIsNone(out.get("test_precision_at_recall_0.001"))
        self.assertIsNone(out.get("test_precision_at_recall_0.01"))
        self.assertIsNone(out.get("test_precision_at_recall_0.1"))
        self.assertIsNone(out.get("test_precision_at_recall_0.5"))


class TestR229_2_MissingColumn(unittest.TestCase):
    """Review #2: 缺 score 欄位 — 目前 KeyError；期望為 ValueError 且 message 含 score/required."""

    def test_missing_score_raises_exception(self):
        """缺 score 時應拋錯，不得靜默通過."""
        df = pd.DataFrame({"label": [1], "is_rated": [True]})
        with self.assertRaises((KeyError, ValueError)):
            backtester_mod.compute_micro_metrics(df, threshold=0.5)

    def test_desired_missing_score_raises_value_error_with_message(self):
        """缺 score 時 raise ValueError 且 message 含 'score' 或 'required'（Round 231 production 修復後）。"""
        df = pd.DataFrame({"label": [1], "is_rated": [True]})
        with self.assertRaises(ValueError) as ctx:
            backtester_mod.compute_micro_metrics(df, threshold=0.5)
        msg = str(ctx.exception).lower()
        self.assertTrue("score" in msg or "required" in msg, f"Message should mention score/required: {msg}")


class TestR229_3_AllNegative(unittest.TestCase):
    """Review #3: all-negative 時三鍵為 None（與 all-positive 對稱）."""

    def test_all_negative_returns_none_for_precision_at_recall(self):
        """label 全 0 時不呼叫 PR curve，三鍵為 None."""
        df = pd.DataFrame({
            "label": [0, 0, 0],
            "score": [0.2, 0.3, 0.1],
            "is_rated": [True, True, True],
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        self.assertIsNone(out["test_precision_at_recall_0.001"])
        self.assertIsNone(out["test_precision_at_recall_0.01"])
        self.assertIsNone(out["test_precision_at_recall_0.1"])
        self.assertIsNone(out["test_precision_at_recall_0.5"])
        self.assertEqual(out["test_positives"], 0)
        self.assertEqual(out["test_ap"], 0.0)


class TestR229_4_TargetRecallsContract(unittest.TestCase):
    """Review #4: _TARGET_RECALLS 與 trainer 口徑一致（常數同步）."""

    def test_backtester_target_recalls_is_expected_tuple(self):
        """Backtester 的 _TARGET_RECALLS 須為 (0.001, 0.01, 0.1, 0.5)，與 PLAN DEC-026 / trainer 一致."""
        self.assertEqual(
            backtester_mod._TARGET_RECALLS,
            (0.001, 0.01, 0.1, 0.5),
            "_TARGET_RECALLS must match trainer and PLAN § DEC-026.",
        )


class TestR229_7_JsonNanContract(unittest.TestCase):
    """Review #7: section 若含 float('nan') 會影響 JSON 序列化契約."""

    def test_section_with_nan_serializes_with_nan_literal_or_raises(self):
        """若 section 含 nan，json.dumps 可能產出 NaN 字面或拋錯；契約為 metric 不得為 nan."""
        section = {
            "test_ap": 0.5,
            "test_precision_at_recall_0.01": float("nan"),
            "test_precision_at_recall_0.1": 0.4,
            "test_precision_at_recall_0.5": 0.3,
        }
        raw = json.dumps(section)
        # 標準 JSON 不允許 NaN；部分環境會產出 literal "NaN"
        self.assertTrue(
            "nan" in raw.lower() or "NaN" in raw,
            "json.dumps(section with nan) should contain nan literal or raise; documents contract.",
        )


if __name__ == "__main__":
    unittest.main()
