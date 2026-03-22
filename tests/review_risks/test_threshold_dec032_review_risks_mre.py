"""
STATUS.md — Code Review：pick_threshold_dec026／DEC-032（2026-03-22）

將 reviewer 風險點轉成最小可重現／契約測試；**僅 tests，不修改 production**。
對應 STATUS「Code Review：pick_threshold_dec026／trainer／backtester」各編號（#1–#8）。

- #1、#5：需匯入 `trainer.training.backtester`（較重，但單檔可接受）。#1 預期 **單次** `precision_recall_curve`。
- #2–#4、#8：僅匯入 `trainer.training.threshold_selection`（輕量）。
- #6：僅讀取 `trainer/training/trainer.py` 原始碼（與 test_t_pipeline_step_durations_review_mre 同策略）。
- #7：讀取 `threshold_selection.py` 文字之契約（無 sqlite）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRAINER_PY = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_THRESHOLD_SELECTION_PY = _REPO_ROOT / "trainer" / "training" / "threshold_selection.py"


def _trainer_text() -> str:
    return _TRAINER_PY.read_text(encoding="utf-8")


def _threshold_selection_text() -> str:
    return _THRESHOLD_SELECTION_PY.read_text(encoding="utf-8")


def _func_src(module_text: str, name: str) -> str:
    tree = ast.parse(module_text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(module_text, node) or ""
    return ""


# ---------------------------------------------------------------------------
# #1 效能：compute_micro_metrics 對四個 recall 僅觸發一次 PR 曲線（單次 sklearn）
# ---------------------------------------------------------------------------
class TestReview1PrecisionRecallCurveCalledOncePerComputeMicroMetrics(unittest.TestCase):
    """MRE：同一 `compute_micro_metrics` 呼叫內，`precision_recall_curve` 僅 1 次（四 recall 共用陣列）。"""

    def test_four_target_recalls_share_one_pr_curve_call(self) -> None:
        from sklearn.metrics import precision_recall_curve as sklearn_precision_recall_curve

        import trainer.training.threshold_selection as ts_mod
        from trainer.training import backtester as backtester_mod

        calls: list[int] = []

        def _counting_pr_curve(*args: object, **kwargs: object):
            calls.append(1)
            return sklearn_precision_recall_curve(*args, **kwargs)

        df = pd.DataFrame(
            {
                "label": [0, 1, 0, 1, 0, 1],
                "score": [0.1, 0.2, 0.3, 0.4, 0.5, 0.9],
                "is_rated": [True] * 6,
            }
        )
        with patch.object(ts_mod, "precision_recall_curve", side_effect=_counting_pr_curve):
            backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        self.assertEqual(
            len(calls),
            1,
            "DEC-026 四個 recall 水準 → 單次 dec026_pr_alert_arrays → 一次 PR 曲線",
        )


# ---------------------------------------------------------------------------
# #2 邊界：window_hours=NaN 時 per-hour 守衛被靜默略過（與 None 等價）
# ---------------------------------------------------------------------------
class TestReview2NanWindowHoursSkipsPerHourGuardSilently(unittest.TestCase):
    """MRE：NaN window_hours 不套用 min_alerts_per_hour（與 window_hours=None 同結果）。"""

    def test_nan_window_hours_matches_none_when_min_alerts_per_hour_set(self) -> None:
        from trainer.training.threshold_selection import pick_threshold_dec026

        y = np.array([0, 0, 0, 0, 1, 1], dtype=float)
        s = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.9], dtype=float)
        common = dict(
            recall_floor=0.01,
            min_alert_count=1,
            min_alerts_per_hour=100.0,
            fbeta_beta=0.5,
        )
        a = pick_threshold_dec026(y, s, window_hours=float("nan"), **common)
        b = pick_threshold_dec026(y, s, window_hours=None, **common)
        self.assertEqual(a, b, "NaN > 0 為 False → per-hour 分支不啟用，應與 None 一致")


# ---------------------------------------------------------------------------
# #3 邊界：min_alert_count<=0 削弱守衛（現況不擲錯）
# ---------------------------------------------------------------------------
class TestReview3NonPositiveMinAlertCountWeakensGuardNoRaise(unittest.TestCase):
    """MRE：負值 min_alert_count 目前不 raise；alert_counts >= -1 恒真。"""

    def test_negative_min_alert_count_does_not_raise(self) -> None:
        from trainer.training.threshold_selection import pick_threshold_dec026

        y = np.array([0, 1, 0, 1], dtype=float)
        s = np.array([0.2, 0.8, 0.3, 0.9], dtype=float)
        out = pick_threshold_dec026(
            y, s, recall_floor=0.01, min_alert_count=-3, min_alerts_per_hour=None, window_hours=None
        )
        self.assertIsInstance(out.threshold, float)


# ---------------------------------------------------------------------------
# #4 邊界：非 0/1 標籤 — n_pos/n_neg 計數與樣本數不一致仍進 sklearn
# ---------------------------------------------------------------------------
class TestReview4NonBinaryLabelsPosNegUndercountMre(unittest.TestCase):
    """MRE：非嚴格 0/1 標籤 → 不呼叫 sklearn PR；回傳 fallback（與 trainer 管線一致）。"""

    def test_label_value_two_returns_fallback_not_sklearn_multiclass_error(self) -> None:
        from trainer.training.threshold_selection import pick_threshold_dec026

        y = np.array([0.0, 1.0, 2.0], dtype=float)
        s = np.array([0.1, 0.5, 0.9], dtype=float)
        self.assertEqual(int((y == 1).sum()), 1)
        self.assertEqual(int((y == 0).sum()), 1)
        self.assertEqual(len(y), 3)
        out = pick_threshold_dec026(y, s, recall_floor=0.01, min_alert_count=1)
        self.assertTrue(out.is_fallback)
        self.assertEqual(out.threshold, 0.5)


# ---------------------------------------------------------------------------
# #5 契約：混入 is_rated=False 時 PR oracle 用全列、micro 告警仍僅 rated
# ---------------------------------------------------------------------------
class TestReview5UnratedRowsSkewOracleVersusRatedOnlySubset(unittest.TestCase):
    """MRE：PR oracle 僅 rated 列；混入高分 unrated 不改 threshold_at_recall_*。"""

    def test_high_score_unrated_does_not_change_oracle_threshold(self) -> None:
        from trainer.training import backtester as backtester_mod

        rated = pd.DataFrame(
            {
                "label": [0, 1, 0, 1],
                "score": [0.2, 0.4, 0.6, 0.8],
                "is_rated": [True] * 4,
            }
        )
        mixed = pd.concat(
            [
                rated,
                pd.DataFrame(
                    {
                        "label": [0, 0],
                        "score": [0.99, 0.995],
                        "is_rated": [False, False],
                    }
                ),
            ],
            ignore_index=True,
        )
        o_rated = backtester_mod.compute_micro_metrics(rated, 0.5, window_hours=1.0)
        o_mixed = backtester_mod.compute_micro_metrics(mixed, 0.5, window_hours=1.0)
        self.assertEqual(
            o_rated.get("threshold_at_recall_0.01"),
            o_mixed.get("threshold_at_recall_0.01"),
            "oracle 僅 is_rated=True；unrated 列不應推 PR／alert_counts",
        )
        # micro 告警數：mixed 僅 rated 可 alert
        self.assertEqual(o_mixed["alerts"], o_rated["alerts"])


# ---------------------------------------------------------------------------
# #6 語意：_compute_test_metrics_from_scores 未使用 pick_threshold_dec026
# ---------------------------------------------------------------------------
class TestReview6TestMetricsScoresPathDoesNotImportSharedPicker(unittest.TestCase):
    """契約：test 集 PR@recall 報告路徑與 DEC-032 共用選阈函式分叉。"""

    def test_compute_test_metrics_from_scores_has_no_pick_threshold_dec026(self) -> None:
        src = _func_src(_trainer_text(), "_compute_test_metrics_from_scores")
        self.assertIn("def _compute_test_metrics_from_scores", src)
        self.assertNotIn(
            "pick_threshold_dec026",
            src,
            "test 指標路徑尚未接線共用選阈 — 與 backtester oracle 口徑可不同",
        )


# ---------------------------------------------------------------------------
# #7 安全：threshold_selection 模組無 sqlite（本模組無持久化覆寫）
# ---------------------------------------------------------------------------
class TestReview7ThresholdSelectionModuleHasNoSqliteContract(unittest.TestCase):
    """契約：選阈純函式檔不應含 sqlite（runtime 覆寫屬後續別檔）。"""

    def test_no_sqlite_import_in_threshold_selection_source(self) -> None:
        text = _threshold_selection_text()
        self.assertNotIn("sqlite", text.lower())


# ---------------------------------------------------------------------------
# #8 文件：DEC-032 用語 select_* 與實作 pick_* 漂移
# ---------------------------------------------------------------------------
class TestReview8NamingDriftSelectVsPickDocumented(unittest.TestCase):
    """MRE：模組匯出 pick_threshold_dec026 與 PLAN／DEC-032 別名 select_threshold_dec026。"""

    def test_select_threshold_dec026_alias_matches_pick(self) -> None:
        import trainer.training.threshold_selection as ts

        self.assertTrue(hasattr(ts, "pick_threshold_dec026"))
        self.assertTrue(hasattr(ts, "select_threshold_dec026"))
        self.assertIs(ts.select_threshold_dec026, ts.pick_threshold_dec026)
