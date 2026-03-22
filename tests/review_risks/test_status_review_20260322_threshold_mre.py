"""
STATUS.md —「Code Review（高可靠性覆核）」§1–§9（2026-03-22）

將該節所列風險轉成最小可重現／契約測試；**僅 tests，不修改 production**。
對照 [.cursor/plans/STATUS.md](../../../.cursor/plans/STATUS.md) 同日期 Code Review 條目。

執行方式見 STATUS 該節「MRE／契約測試落地」。
"""

from __future__ import annotations

import math
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# §1 語意分叉：test_ap 全列 vs PR oracle 僅 rated
# ---------------------------------------------------------------------------
class TestStatusReview1ApFullFrameVsOracleRatedOnly(unittest.TestCase):
    """全 df 雙類，但 rated 子集無正例 → AP 有值、PR@recall 為 None。"""

    def test_ap_positive_while_precision_at_recall_none_when_positives_unrated(self) -> None:
        from trainer.training import backtester as bm

        df = pd.DataFrame(
            {
                # 僅 rated 為負例；正例都在 unrated
                "label": [0, 0, 1, 1],
                "score": [0.1, 0.2, 0.9, 0.95],
                "is_rated": [True, True, False, False],
            }
        )
        out = bm.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        self.assertGreater(out["test_positives"], 0)
        self.assertIsNotNone(out["test_ap"])
        self.assertGreater(out["test_ap"], 0.0)
        self.assertIsNone(
            out["test_precision_at_recall_0.01"],
            "oracle 僅 rated；無 rated 正例時不應填 PR@recall",
        )


# ---------------------------------------------------------------------------
# §2 from_pr_arrays + NaN window：與 None 同結果且無 warning（高層 API 才有 warning）
# ---------------------------------------------------------------------------
class TestStatusReview2FromPrArraysNanWindowSilentVsPickLogs(unittest.TestCase):
    """`pick_threshold_dec026_from_pr_arrays` 傳 raw NaN window不寫 log；`pick_threshold_dec026` 會 warning。"""

    def test_from_pr_arrays_nan_window_matches_none_no_warning(self) -> None:
        from trainer.training.threshold_selection import (
            dec026_pr_alert_arrays,
            pick_threshold_dec026_from_pr_arrays,
        )

        y = np.array([0, 0, 0, 0, 1, 1], dtype=float)
        s = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.9], dtype=float)
        prep = dec026_pr_alert_arrays(y, s)
        assert prep is not None
        pr_p, pr_r, pr_th, ac, _n = prep
        common = dict(
            recall_floor=0.01,
            min_alert_count=1,
            min_alerts_per_hour=100.0,
            fbeta_beta=0.5,
        )
        a = pick_threshold_dec026_from_pr_arrays(
            pr_p, pr_r, pr_th, ac, window_hours=float("nan"), **common
        )
        b = pick_threshold_dec026_from_pr_arrays(
            pr_p, pr_r, pr_th, ac, window_hours=None, **common
        )
        self.assertEqual(a, b)
        with self.assertNoLogs("trainer.training.threshold_selection", level="WARNING"):
            pick_threshold_dec026_from_pr_arrays(
                pr_p, pr_r, pr_th, ac, window_hours=float("nan"), **common
            )

    def test_pick_threshold_logs_warning_for_nan_window(self) -> None:
        from trainer.training.threshold_selection import pick_threshold_dec026

        y = np.array([0, 0, 0, 0, 1, 1], dtype=float)
        s = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.9], dtype=float)
        with self.assertLogs("trainer.training.threshold_selection", level="WARNING") as cm:
            pick_threshold_dec026(
                y,
                s,
                recall_floor=0.01,
                min_alert_count=1,
                min_alerts_per_hour=100.0,
                window_hours=float("nan"),
                fbeta_beta=0.5,
            )
        self.assertTrue(
            any("window_hours" in x and "non-finite" in x for x in cm.output),
            cm.output,
        )


# ---------------------------------------------------------------------------
# §3 嚴格二元：略低於 1.0 的 float 觸發 fallback
# ---------------------------------------------------------------------------
class TestStatusReview3NearOneLabelFailsStrictBinary(unittest.TestCase):
    def test_nextafter_one_point_zero_triggers_fallback(self) -> None:
        from trainer.training.threshold_selection import pick_threshold_dec026

        y = np.array([0.0, 1.0, np.nextafter(1.0, 0.0)], dtype=float)
        s = np.array([0.1, 0.8, 0.9], dtype=float)
        self.assertFalse(np.all((y == 0.0) | (y == 1.0)))
        out = pick_threshold_dec026(
            y, s, recall_floor=0.01, min_alert_count=1, fbeta_beta=0.5
        )
        self.assertTrue(out.is_fallback)
        self.assertEqual(out.threshold, 0.5)


# ---------------------------------------------------------------------------
# §4 單一 THRESHOLD_MIN_RECALL vs 多個 _TARGET_RECALLS 報表鍵
# ---------------------------------------------------------------------------
class TestStatusReview4SingleTrainingRecallVsMultiBacktesterKeys(unittest.TestCase):
    def test_config_single_recall_floor_and_multiple_threshold_at_recall_keys(self) -> None:
        from trainer.core import config
        from trainer.training import backtester as bm

        self.assertGreater(len(bm._TARGET_RECALLS), 1)
        df = pd.DataFrame(
            {
                "label": [0, 1, 0, 1, 0, 1],
                "score": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9],
                "is_rated": [True] * 6,
            }
        )
        out = bm.compute_micro_metrics(df, 0.5, window_hours=1.0)
        for r in bm._TARGET_RECALLS:
            self.assertIn(f"threshold_at_recall_{r}", out)
        # 訓練選阈使用單一 config 常數（與多鍵報表並存 — 儀表板勿混用語意）
        self.assertIsNotNone(config.THRESHOLD_MIN_RECALL)


# ---------------------------------------------------------------------------
# §5 fbeta_beta 非法值：鎖定現況（供日後防呆時對照）
# ---------------------------------------------------------------------------
class TestStatusReview5IllegalFbetaBetaCurrentContract(unittest.TestCase):
    def test_nan_negative_zero_fbeta_locked(self) -> None:
        from trainer.training.threshold_selection import pick_threshold_dec026

        y = np.array([0.0, 0.0, 1.0, 1.0], dtype=float)
        s = np.array([0.1, 0.2, 0.6, 0.9], dtype=float)
        base = pick_threshold_dec026(
            y, s, recall_floor=0.01, min_alert_count=1, fbeta_beta=0.5
        )
        self.assertFalse(base.is_fallback)

        nan_b = pick_threshold_dec026(
            y, s, recall_floor=0.01, min_alert_count=1, fbeta_beta=float("nan")
        )
        self.assertFalse(nan_b.is_fallback)
        self.assertEqual(nan_b.fbeta, 0.0)

        neg_b = pick_threshold_dec026(
            y, s, recall_floor=0.01, min_alert_count=1, fbeta_beta=-1.0
        )
        self.assertFalse(neg_b.is_fallback)
        self.assertEqual(neg_b.fbeta, 1.0)

        zero_b = pick_threshold_dec026(
            y, s, recall_floor=0.01, min_alert_count=1, fbeta_beta=0.0
        )
        self.assertFalse(zero_b.is_fallback)
        self.assertEqual(zero_b.fbeta, 1.0)


# ---------------------------------------------------------------------------
# §6 window_hours=+inf：alerts_per_hour 為 0、sanitize 會 warning
# ---------------------------------------------------------------------------
class TestStatusReview6InfWindowHoursAlertsPerHourAndWarning(unittest.TestCase):
    def test_inf_window_gives_zero_alerts_per_hour_and_logs_warning(self) -> None:
        from trainer.training import backtester as bm

        df = pd.DataFrame(
            {
                "label": [0, 1, 0, 1, 0, 1],
                "score": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9],
                "is_rated": [True] * 6,
            }
        )
        with self.assertLogs("trainer.training.threshold_selection", level="WARNING"):
            out = bm.compute_micro_metrics(df, 0.5, window_hours=float("inf"))
        self.assertEqual(out["alerts_per_hour"], 0.0)
        apm = out.get("alerts_per_minute_at_recall_0.01")
        if apm is not None:
            self.assertTrue(math.isfinite(apm))
            self.assertEqual(apm, 0.0)


# ---------------------------------------------------------------------------
# §7 單次 PR 呼叫（與 test_threshold_dec032_review_risks_mre #1 同契約）
# ---------------------------------------------------------------------------
class TestStatusReview7ComputeMicroMetricsSinglePrecisionRecallCurve(unittest.TestCase):
    def test_precision_recall_curve_called_once(self) -> None:
        from sklearn.metrics import precision_recall_curve as sklearn_pr_curve

        import trainer.training.threshold_selection as ts_mod
        from trainer.training import backtester as bm

        calls: list[int] = []

        def _counting(*args: object, **kwargs: object):
            calls.append(1)
            return sklearn_pr_curve(*args, **kwargs)

        df = pd.DataFrame(
            {
                "label": [0, 1, 0, 1, 0, 1],
                "score": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9],
                "is_rated": [True] * 6,
            }
        )
        with patch.object(ts_mod, "precision_recall_curve", side_effect=_counting):
            bm.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        self.assertEqual(len(calls), 1)


# ---------------------------------------------------------------------------
# §8 repo 根子程序 import tests.integration.test_api_server
# ---------------------------------------------------------------------------
class TestStatusReview8SubprocessImportIntegrationTestApiServer(unittest.TestCase):
    def test_python_c_import_from_repo_root(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            "from tests.integration.test_api_server import _make_stub_artifacts; "
            "assert isinstance(_make_stub_artifacts(), dict)",
        ]
        subprocess.run(cmd, cwd=str(_REPO_ROOT), check=True)


# ---------------------------------------------------------------------------
# §9 T-OnlineCalibration：非法 runtime 閾值 — 待功能落地後實作
# ---------------------------------------------------------------------------
class TestStatusReview9RuntimeThresholdValidationPlaceholder(unittest.TestCase):
    @unittest.skip("待 T-OnlineCalibration：state DB runtime 閾值驗證與 scorer fallback")
    def test_illegal_runtime_threshold_falls_back_to_bundle(self) -> None:
        raise AssertionError("unreachable until scorer reads runtime_rated_threshold")


if __name__ == "__main__":
    unittest.main()
