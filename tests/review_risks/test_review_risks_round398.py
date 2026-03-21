"""Round 398 Review — 審查風險點轉成最小可重現測試（tests only，不改 production）。

STATUS.md Round 398 Review 各項建議新增測試之實作。僅新增測試，不修改 trainer/backtester。
Reference: PLAN § 閾值策略與 Precision-at-Recall 報告更新（DEC-026）, DECISION_LOG DEC-026.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

import trainer.backtester as backtester_mod

# Canonical precision@recall key suffixes (DEC-026); no float formatting drift.
_RECALL_STRINGS = ("0.001", "0.01", "0.1", "0.5")

# Backtester flat metrics: precision@recall keys only (for key-name and contract tests).
_EXPECTED_PRECISION_AT_RECALL_KEYS_BACKTESTER = frozenset(
    {f"test_precision_at_recall_{r}" for r in _RECALL_STRINGS}
    | {f"threshold_at_recall_{r}" for r in _RECALL_STRINGS}
    | {f"alerts_per_minute_at_recall_{r}" for r in _RECALL_STRINGS}
)

# Trainer test metrics: same + n_alerts + prod_adjusted precision@recall (for contract / type tests).
_EXPECTED_PRECISION_AT_RECALL_KEYS_TRAINER = _EXPECTED_PRECISION_AT_RECALL_KEYS_BACKTESTER | frozenset(
    {f"n_alerts_at_recall_{r}" for r in _RECALL_STRINGS}
    | {f"test_precision_at_recall_{r}_prod_adjusted" for r in _RECALL_STRINGS}
)


# ---------------------------------------------------------------------------
# R398 Review #1 — 鍵名僅限預期字串，無 float 格式化漂移
# ---------------------------------------------------------------------------
class TestR398_1_KeyNamesCanonical(unittest.TestCase):
    """Review #1: precision@recall 相關鍵僅限 0.001/0.01/0.1/0.5，無 0.10000000000000001 等."""

    def test_backtester_precision_at_recall_keys_exactly_expected_set(self):
        """compute_micro_metrics 回傳的 precision@recall 鍵集合等於預期，無多餘鍵."""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1, 0, 1],
            "score": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9],
            "is_rated": [True] * 6,
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        par_keys = {k for k in out if "recall" in k and ("precision" in k or "threshold" in k or "alerts_per_minute" in k)}
        self.assertEqual(
            par_keys,
            _EXPECTED_PRECISION_AT_RECALL_KEYS_BACKTESTER,
            "precision@recall keys must be exactly the canonical set (no float drift).",
        )

    def test_no_float_drift_key_in_backtester_result(self):
        """任一鍵名不得包含 0.10000000000000001 等非預期字串."""
        df = pd.DataFrame({
            "label": [0, 1],
            "score": [0.2, 0.8],
            "is_rated": [True, True],
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        for key in out:
            self.assertNotIn(
                "0.10000000000000001",
                key,
                "Key names must not contain float repr drift.",
            )


# ---------------------------------------------------------------------------
# R398 Review #2 — PR 曲線僅一點或空：全部分數相同時不崩潰、鍵存在且值為 None 或數字
# ---------------------------------------------------------------------------
class TestR398_2_AllSameScoreEdgeNoCrash(unittest.TestCase):
    """Review #2: 全部分數相同時（PR 曲線至多一點），不拋錯且產出為預期鍵、值為 None 或數字."""

    def test_all_same_score_returns_expected_keys_and_none_or_numeric(self):
        """所有 sample 同分時，回傳含預期鍵；各 recall 之 precision/threshold/apm 為 None 或數字（不崩潰）."""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1],
            "score": [0.5, 0.5, 0.5, 0.5],
            "is_rated": [True] * 4,
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        for r in _RECALL_STRINGS:
            for key in (f"test_precision_at_recall_{r}", f"threshold_at_recall_{r}", f"alerts_per_minute_at_recall_{r}"):
                v = out.get(key)
                self.assertTrue(
                    v is None or isinstance(v, (int, float, np.integer, np.floating)),
                    f"Same score → {key} must be None or numeric, got {type(v)}.",
                )
        self.assertTrue(
            _EXPECTED_PRECISION_AT_RECALL_KEYS_BACKTESTER.issubset(set(out.keys())),
            "Must contain all precision@recall keys (edge case must not drop keys).",
        )


# ---------------------------------------------------------------------------
# R398 Review #4 — 型別：threshold/precision 為 float 或 None，n_alerts 為 int 或 None
# ---------------------------------------------------------------------------
class TestR398_4_MetricsValueTypes(unittest.TestCase):
    """Review #4: metrics 中 precision@recall 相關值型別符合契約（float/int/None）."""

    def test_backtester_threshold_and_precision_and_apm_are_float_or_none(self):
        """Backtester 回傳之 threshold_at_recall_* / test_precision_at_recall_* / apm 為 float 或 None."""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1, 0, 1],
            "score": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9],
            "is_rated": [True] * 6,
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        for r in _RECALL_STRINGS:
            v = out.get(f"threshold_at_recall_{r}")
            self.assertTrue(v is None or isinstance(v, (float, np.floating)), f"threshold_at_recall_{r}")
            v = out.get(f"test_precision_at_recall_{r}")
            self.assertTrue(v is None or isinstance(v, (float, np.floating)), f"test_precision_at_recall_{r}")
            v = out.get(f"alerts_per_minute_at_recall_{r}")
            self.assertTrue(v is None or isinstance(v, (float, np.floating)), f"alerts_per_minute_at_recall_{r}")

    def test_trainer_n_alerts_at_recall_is_int_or_none(self):
        """Trainer _compute_test_metrics_from_scores 回傳之 n_alerts_at_recall_* 為 int 或 None."""
        from trainer.trainer import _compute_test_metrics_from_scores

        # Enough rows to pass MIN_VALID_TEST_ROWS and get non-zeroed metrics.
        n = 60
        y = np.array([0] * 30 + [1] * 30, dtype=np.float64)
        scores = np.linspace(0.1, 0.9, n)
        out = _compute_test_metrics_from_scores(
            y, scores, threshold=0.5, log_results=False,
        )
        for r in _RECALL_STRINGS:
            v = out.get(f"n_alerts_at_recall_{r}")
            self.assertTrue(
                v is None or isinstance(v, (int, np.integer)),
                f"n_alerts_at_recall_{r} must be int or None, got {type(v)}.",
            )
            v = out.get(f"test_precision_at_recall_{r}_prod_adjusted")
            self.assertTrue(
                v is None or isinstance(v, (float, np.floating)),
                f"test_precision_at_recall_{r}_prod_adjusted must be float or None, got {type(v)}.",
            )


# ---------------------------------------------------------------------------
# R398 Review #5 — 下游契約：metrics 必含所有 precision@recall 相關鍵
# ---------------------------------------------------------------------------
class TestR398_5_DownstreamContractKeys(unittest.TestCase):
    """Review #5: metrics dict 必含四個 recall 與 threshold/apm（及 trainer 之 n_alerts）鍵."""

    def test_backtester_flat_metrics_contain_all_precision_at_recall_keys(self):
        """compute_micro_metrics 回傳必含 _EXPECTED_PRECISION_AT_RECALL_KEYS_BACKTESTER."""
        df = pd.DataFrame({
            "label": [0, 1],
            "score": [0.2, 0.8],
            "is_rated": [True, True],
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        missing = _EXPECTED_PRECISION_AT_RECALL_KEYS_BACKTESTER - set(out.keys())
        self.assertFalse(missing, f"Backtester flat metrics must contain: {missing}")

    def test_trainer_test_metrics_contain_all_precision_at_recall_keys(self):
        """_compute_test_metrics_from_scores 回傳必含 trainer 的 precision@recall 鍵集（含 n_alerts）."""
        from trainer.trainer import _compute_test_metrics_from_scores

        n = 60
        y = np.array([0] * 30 + [1] * 30, dtype=np.float64)
        scores = np.linspace(0.1, 0.9, n)
        out = _compute_test_metrics_from_scores(
            y, scores, threshold=0.5, log_results=False,
        )
        missing = _EXPECTED_PRECISION_AT_RECALL_KEYS_TRAINER - set(out.keys())
        self.assertFalse(missing, f"Trainer test metrics must contain: {missing}")


# ---------------------------------------------------------------------------
# R398 Review #6 — window_hours=0 / None 時 alerts_per_minute_at_recall_* 為 None
# ---------------------------------------------------------------------------
class TestR398_6_WindowHoursZeroOrNoneApmNone(unittest.TestCase):
    """Review #6: window_hours 為 0 或 None 時，所有 alerts_per_minute_at_recall_* 為 None."""

    def test_window_hours_zero_all_apm_at_recall_none(self):
        """window_hours=0 時，alerts_per_minute_at_recall_* 皆為 None."""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1],
            "score": [0.2, 0.4, 0.6, 0.8],
            "is_rated": [True] * 4,
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=0)
        for r in _RECALL_STRINGS:
            self.assertIsNone(
                out.get(f"alerts_per_minute_at_recall_{r}"),
                f"window_hours=0 → alerts_per_minute_at_recall_{r} must be None.",
            )

    def test_window_hours_none_all_apm_at_recall_none(self):
        """window_hours=None 時，alerts_per_minute_at_recall_* 皆為 None."""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1],
            "score": [0.2, 0.4, 0.6, 0.8],
            "is_rated": [True] * 4,
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=None)
        for r in _RECALL_STRINGS:
            self.assertIsNone(
                out.get(f"alerts_per_minute_at_recall_{r}"),
                f"window_hours=None → alerts_per_minute_at_recall_{r} must be None.",
            )

    def test_window_hours_positive_apm_can_be_float(self):
        """window_hours>0 且具 alerts 時，alerts_per_minute_at_recall_* 可為正數."""
        df = pd.DataFrame({
            "label": [0, 1, 0, 1, 0, 1],
            "score": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9],
            "is_rated": [True] * 6,
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        # At least one recall level may have a numeric apm.
        apm_values = [out.get(f"alerts_per_minute_at_recall_{r}") for r in _RECALL_STRINGS]
        self.assertTrue(
            any(v is not None and v >= 0 for v in apm_values),
            "With window_hours=1.0 and mixed labels, at least one apm_at_recall should be numeric.",
        )


if __name__ == "__main__":
    unittest.main()
