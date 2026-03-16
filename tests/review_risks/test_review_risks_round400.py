"""Round 400 Review — 審查風險點轉成最小可重現測試（tests only，不改 production）。

STATUS.md Round 400 Review 各項建議新增測試之實作。僅新增測試，不修改 trainer/backtester/config。
Reference: PLAN § 閾值策略與 Precision-at-Recall 報告更新（DEC-026）, DECISION_LOG DEC-026.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.backtester as backtester_mod
import trainer.config as config_mod


# ---------------------------------------------------------------------------
# R400 Review #1 — Config：THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL 與 THRESHOLD_MIN_RECALL 一致
# ---------------------------------------------------------------------------
class TestR400_1_ConfigThresholdRecallContract(unittest.TestCase):
    """Review #1: 當 THRESHOLD_MIN_RECALL 不為 None 時，應與 THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL 相等（契約）。"""

    def test_optimize_precision_at_recall_equals_min_recall_when_min_recall_is_set(self):
        """THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL 與 THRESHOLD_MIN_RECALL 相等，避免文件與邏輯漂移."""
        min_recall = getattr(config_mod, "THRESHOLD_MIN_RECALL", None)
        optimize_at = getattr(config_mod, "THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL", None)
        if min_recall is not None and optimize_at is not None:
            self.assertEqual(
                optimize_at,
                min_recall,
                "THRESHOLD_OPTIMIZE_PRECISION_AT_RECALL must equal THRESHOLD_MIN_RECALL when both set (contract).",
            )


# ---------------------------------------------------------------------------
# R400 Review #2 — Backtester：最佳 precision=0 或無可行解時 fallback 至 model-default
# ---------------------------------------------------------------------------
class TestR400_2_BacktesterBestPrecisionZeroFallback(unittest.TestCase):
    """Review #2: 當所有 trial 的 precision 皆為 0（或無可行解）時，回傳 model-default threshold 且不拋錯."""

    def test_all_negative_rated_sub_returns_model_default_threshold(self):
        """rated_sub 全為負樣本時，recall 恆無法 >= 0.01，所有 trial 回傳 0.0，應 fallback 至 model default."""
        df = pd.DataFrame({
            "label": [0, 0, 0, 0, 0],
            "score": [0.2, 0.4, 0.5, 0.6, 0.8],
            "is_rated": [True] * 5,
        })
        artifacts = {"rated": {"threshold": 0.5}}
        t1, t2 = backtester_mod.run_optuna_threshold_search(
            df, artifacts, n_trials=3, window_hours=1.0,
        )
        self.assertEqual(t1, 0.5, "All-negative → model-default threshold")
        self.assertEqual(t2, 0.5)
        self.assertIsInstance(t1, float)
        self.assertIsInstance(t2, float)


# ---------------------------------------------------------------------------
# R400 Review #3 — Trainer：同 precision 多點時 tie-breaking 取高閾值（可選）
# ---------------------------------------------------------------------------
class TestR400_3_TrainerTieBreakHighestThreshold(unittest.TestCase):
    """Review #3: 多個 valid 點具相同最大 precision 時，選出之 threshold 為該 precision 對應之最高閾值."""

    def test_trainer_threshold_selection_returns_valid_metrics(self):
        """閾值選擇路徑執行後回傳之 threshold 與 val_precision 型別與範圍合理（tie-breaking 由 production 註解說明）。"""
        from trainer.trainer import _train_one_model

        np.random.seed(42)
        n = 30
        X_val = pd.DataFrame({"f1": np.random.randn(n).cumsum(), "f2": np.random.rand(n)})
        y_val = pd.Series(np.array([0] * 15 + [1] * 15, dtype=np.float64))
        X_tr = X_val.iloc[:5].copy()
        y_tr = pd.Series(np.array([0, 1, 0, 1, 0], dtype=np.float64))
        sw_rated = pd.Series(np.ones(5))
        _model, metrics = _train_one_model(
            X_tr, y_tr, X_val, y_val, sw_rated, {}, label="rated", log_results=False,
        )
        self.assertIn("threshold", metrics)
        self.assertIsInstance(metrics["threshold"], (float, np.floating))
        self.assertGreaterEqual(metrics["threshold"], 0.0)
        self.assertLessEqual(metrics["threshold"], 1.0)
        self.assertIn("val_precision", metrics)
        self.assertIn("val_recall", metrics)
        if metrics["val_recall"] is not None and metrics["val_recall"] >= 0.01:
            self.assertIsInstance(metrics["val_precision"], (float, np.floating))


# ---------------------------------------------------------------------------
# R400 Review #4 — Backtester：THRESHOLD_MIN_RECALL 為 None 時不因 recall 低而回傳 0.0
# ---------------------------------------------------------------------------
class TestR400_4_BacktesterMinRecallNoneNoRecallConstraint(unittest.TestCase):
    """Review #4: 當 THRESHOLD_MIN_RECALL 為 None 時，objective 不檢查 recall，低 recall 的 trial 仍回傳其 precision."""

    def test_when_min_recall_is_none_search_returns_without_raising(self):
        """Patch THRESHOLD_MIN_RECALL 為 None 時，run_optuna_threshold_search 不拋錯且回傳 (t, t) 在 [0.01, 0.99]."""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1, 0],
            "score": [0.2, 0.4, 0.5, 0.6, 0.9],
            "is_rated": [True] * 5,
        })
        artifacts = {"rated": {"threshold": 0.5}}
        with patch.object(backtester_mod, "THRESHOLD_MIN_RECALL", None):
            t1, t2 = backtester_mod.run_optuna_threshold_search(
                df, artifacts, n_trials=2, window_hours=1.0,
            )
        self.assertIsInstance(t1, float)
        self.assertIsInstance(t2, float)
        self.assertGreaterEqual(t1, 0.01)
        self.assertLessEqual(t1, 0.99)
        self.assertEqual(t1, t2)


if __name__ == "__main__":
    unittest.main()
