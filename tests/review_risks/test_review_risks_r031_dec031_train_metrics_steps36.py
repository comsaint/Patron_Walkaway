"""R031 — STATUS.md「Code Review：DEC-031 步驟 3–6」風險之最小可重現／契約測試。

對應 `.cursor/plans/STATUS.md` 內 **R031-1 … R031-8**。
**僅測試**；不修改 production code。

多數測試在方法內 `import trainer.trainer`，避免收集階段載入過重模組。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_CONFIG_PATH = _REPO_ROOT / "trainer" / "core" / "config.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_train_single_rated_model_src() -> str:
    for node in _TRAINER_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == "train_single_rated_model":
            seg = ast.get_source_segment(_TRAINER_SRC, node)
            if not seg:
                raise AssertionError("empty train_single_rated_model segment")
            return seg
    raise AssertionError("train_single_rated_model not found")


def _get_function_src(func_name: str) -> str:
    for node in _TRAINER_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            seg = ast.get_source_segment(_TRAINER_SRC, node)
            if not seg:
                raise AssertionError(f"empty segment for {func_name}")
            return seg
    raise AssertionError(f"{func_name} not found")


# ---------------------------------------------------------------------------
# R031-1: y / scores 長度不一致時靜默截斷（與舊路徑「多半拋錯」行為不同）
# ---------------------------------------------------------------------------


class TestR031_1_MismatchedLengthSilentTrim(unittest.TestCase):
    """R031-1: `_train_metrics_dict_from_y_scores` 以 min 長度對齊，不拋例外。"""

    def test_train_metrics_dict_trims_to_min_length_without_raising(self):
        from trainer.trainer import _train_metrics_dict_from_y_scores

        y = np.array([1, 0, 1, 0, 1], dtype=np.int64)
        scores = np.array([0.9, 0.1, 0.8], dtype=np.float64)
        out = _train_metrics_dict_from_y_scores(y, scores, threshold=0.5, log_results=False)
        self.assertEqual(out["train_samples"], 3, "expected silent trim to min(len(y), len(scores))")
        self.assertIn("train_ap", out)

    def test_contract_source_contains_explicit_min_length_trim(self):
        """靜態契約：函式本文須含長度不符時之 min／截斷邏輯（防誤刪）。"""
        src = _get_function_src("_train_metrics_dict_from_y_scores")
        self.assertRegex(
            src,
            r"min\s*\(\s*len\s*\(\s*y_arr\s*\)\s*,\s*len\s*\(\s*scores_arr\s*\)\s*\)",
            "R031-1: trim logic must remain explicit in source.",
        )


# ---------------------------------------------------------------------------
# R031-2: 分批失敗 fallback predict_proba 仍配置 (n,2) 稠密矩陣
# ---------------------------------------------------------------------------


class TestR031_2_BatchedFallbackStillUsesDenseProba(unittest.TestCase):
    """R031-2: batched 失敗後 fallback 仍走 predict_proba → 二欄稠密矩陣。"""

    def test_batched_raises_memory_error_fallback_returns_metrics(self):
        """重現：分批路徑拋錯時仍回傳 train_*（依賴 predict_proba fallback）。"""
        import lightgbm as lgb

        from trainer.trainer import _compute_train_metrics

        X = pd.DataFrame({"f0": [0.0, 1.0, 0.0], "f1": [1.0, 0.0, 1.0]})
        y = pd.Series([0, 1, 0], dtype=np.int64)
        clf = lgb.LGBMClassifier(n_estimators=4, verbose=-1, force_col_wise=True)
        clf.fit(X, y)
        self.assertIsNotNone(getattr(clf, "booster_", None))

        with patch(
            "trainer.trainer._batched_booster_predict_scores",
            side_effect=MemoryError("simulated batch failure"),
        ):
            out = _compute_train_metrics(clf, 0.5, X, y, log_results=False)

        self.assertEqual(out["train_samples"], 3)
        self.assertIn("train_ap", out)

    def test_booster_wrapper_predict_proba_is_two_column_dense(self):
        """契約：`_BoosterWrapper.predict_proba` 對 n 列回傳 shape (n, 2)（峰值 RAM 語意）。"""
        import lightgbm as lgb

        from trainer.trainer import _BoosterWrapper

        X = pd.DataFrame({"f0": [0.0, 1.0], "f1": [1.0, 0.0]})
        y = np.array([0, 1])
        train_set = lgb.Dataset(X, label=y)
        booster = lgb.train({"verbose": -1, "force_col_wise": True}, train_set, num_boost_round=2)
        wrap = _BoosterWrapper(booster)
        pr = wrap.predict_proba(X)
        self.assertEqual(pr.shape, (2, 2))


# ---------------------------------------------------------------------------
# R031-3: LibSVM train 指標長度 trim 目前無 warning（可觀測性缺口）
# ---------------------------------------------------------------------------


class TestR031_3_LibsvmTrainMetricsTrimSilentInSource(unittest.TestCase):
    """R031-3: train LibSVM 分數與標籤長度不符之 if 區塊目前不含 logger.warning。"""

    def test_length_mismatch_trim_block_has_no_logger_warning_yet(self):
        """契約：現行實作為 silent trim；若日後加 warning，應改此測試預期。"""
        src = _get_train_single_rated_model_src()
        marker = "if len(tr_scores) != len(y_tr_file):"
        i = src.find(marker)
        self.assertGreater(i, 0, "train LibSVM metrics trim branch not found")
        chunk = src[i : i + 350]
        self.assertNotIn(
            "logger.warning",
            chunk,
            "R031-3: document silent trim; when adding warning, flip assertion and message.",
        )


# ---------------------------------------------------------------------------
# R031-4: 正類計數依賴 y == 1（與純 sum 在非 0/1 標籤上可能分歧）
# ---------------------------------------------------------------------------


class TestR031_4_LabelDtypeZeroOneEquivalence(unittest.TestCase):
    """R031-4: 嚴格 0/1 時 float 與 int 標籤應給出相同 train_positives / train_samples。"""

    def test_float01_vs_int_labels_same_positives_and_samples(self):
        from trainer.trainer import _train_metrics_dict_from_y_scores

        scores = np.array([0.2, 0.7, 0.3, 0.9], dtype=np.float64)
        y_int = np.array([0, 1, 0, 1], dtype=np.int64)
        y_flt = np.array([0.0, 1.0, 0.0, 1.0], dtype=np.float64)
        a = _train_metrics_dict_from_y_scores(y_int, scores, 0.5, log_results=False)
        b = _train_metrics_dict_from_y_scores(y_flt, scores, 0.5, log_results=False)
        self.assertEqual(a["train_positives"], b["train_positives"])
        self.assertEqual(a["train_samples"], b["train_samples"])

    def test_non_binary_label_two_counts_as_non_positive_for_eq1(self):
        """重現：標籤為 2 時 `(y==1).sum()` 不計入正類（與 `y.sum()` 語意不同）。"""
        from trainer.trainer import _train_metrics_dict_from_y_scores

        scores = np.array([0.5, 0.5, 0.5], dtype=np.float64)
        y = np.array([0, 2, 1], dtype=np.float64)
        out = _train_metrics_dict_from_y_scores(y, scores, 0.5, log_results=False)
        self.assertEqual(out["train_positives"], 1, "only strict == 1 counts as positive")


# ---------------------------------------------------------------------------
# R031-5: Plan B+ train 指標檔案分支與 SSOT 相關契約字串
# ---------------------------------------------------------------------------


class TestR031_5_LibsvmTrainMetricsBranchContract(unittest.TestCase):
    """R031-5: 原始碼須保留 LibSVM train 指標優先分支（防誤刪）。"""

    def test_train_single_rated_model_contains_libsvm_train_metrics_gate(self):
        src = _get_train_single_rated_model_src()
        self.assertIn("used_libsvm_train_metrics", src)
        self.assertIn("_labels_from_libsvm(_train_libsvm_p)", src)
        self.assertIn("_train_metrics_dict_from_y_scores", src)


# ---------------------------------------------------------------------------
# R031-6: 廣義 except Exception 吞掉 RuntimeError 仍 fallback（可恢復假設）
# ---------------------------------------------------------------------------


class TestR031_6_BroadExceptAllowsRuntimeErrorFallback(unittest.TestCase):
    """R031-6: 分批路徑 `except Exception` 使 RuntimeError 亦觸發 predict_proba fallback。"""

    def test_runtime_error_in_batched_predict_falls_back_to_proba(self):
        import lightgbm as lgb

        from trainer.trainer import _compute_train_metrics

        X = pd.DataFrame({"f0": [0.0, 1.0], "f1": [1.0, 0.0]})
        y = pd.Series([0, 1], dtype=np.int64)
        clf = lgb.LGBMClassifier(n_estimators=3, verbose=-1, force_col_wise=True)
        clf.fit(X, y)

        with patch(
            "trainer.trainer._batched_booster_predict_scores",
            side_effect=RuntimeError("simulated implementation bug"),
        ):
            out = _compute_train_metrics(clf, 0.5, X, y, log_results=False)

        self.assertEqual(out["train_samples"], 2)


# ---------------------------------------------------------------------------
# R031-7: TRAIN_METRICS_PREDICT_BATCH_ROWS 尚無 env 覆寫（config 契約）
# ---------------------------------------------------------------------------


class TestR031_7_BatchRowsNotEnvDrivenYet(unittest.TestCase):
    """R031-7: 目前 batch 常數為純指派；若改 getenv 應更新此測試。"""

    def test_config_train_metrics_batch_rows_line_has_no_getenv(self):
        text = _CONFIG_PATH.read_text(encoding="utf-8")
        m = re.search(r"^TRAIN_METRICS_PREDICT_BATCH_ROWS\s*[:=].+$", text, re.MULTILINE)
        self.assertIsNotNone(m, "TRAIN_METRICS_PREDICT_BATCH_ROWS assignment not found in config.py")
        line = m.group(0)
        self.assertNotIn(
            "getenv",
            line,
            "R031-7: batch rows not env-overridable yet; update test when os.getenv is added.",
        )

    def test_runtime_default_matches_config_module(self):
        import trainer.core.config as cfg

        from trainer.trainer import TRAIN_METRICS_PREDICT_BATCH_ROWS as mod_val

        self.assertEqual(mod_val, cfg.TRAIN_METRICS_PREDICT_BATCH_ROWS)


# ---------------------------------------------------------------------------
# R031-8: Train LibSVM 路徑須在 DATA_DIR 下（與 test 分支同型檢查）
# ---------------------------------------------------------------------------


class TestR031_8_TrainLibsvmPathUnderDataDirContract(unittest.TestCase):
    """R031-8: train 指標檔案分支須含 relative_to(DATA_DIR) 守衛。"""

    def test_train_libsvm_metrics_branch_checks_under_data_dir(self):
        src = _get_train_single_rated_model_src()
        idx = src.find("_train_libsvm_p.resolve().relative_to(DATA_DIR.resolve())")
        self.assertGreater(
            idx,
            0,
            "train LibSVM metrics path must resolve under DATA_DIR (same contract as test file branch).",
        )


# ---------------------------------------------------------------------------
# Lint 契約：_compute_train_metrics 仍含 except Exception（審閱風險標記）
# ---------------------------------------------------------------------------


class TestR031_LintContract_BroadExceptStillPresent(unittest.TestCase):
    """以原始碼契約標記審閱風險；若收窄 except，應同步改測試與 STATUS。"""

    def test_compute_train_metrics_contains_except_exception_for_batched_fallback(self):
        src = _get_function_src("_compute_train_metrics")
        self.assertIn("except Exception", src)
        self.assertIn("_batched_booster_predict_scores", src)


if __name__ == "__main__":
    unittest.main()
