"""Minimal reproducible tests for Round 410 Code Review — Optuna HPO 抽樣.

STATUS.md «Round 410 Code Review — Optuna HPO 抽樣（R410 Review）» 的風險點轉成
最小可重現測試（或 AST/源碼契約）。僅新增 tests，不改 production code。
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import trainer.trainer as trainer_mod

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "training" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


# ---------------------------------------------------------------------------
# Helpers: minimal data for run_optuna_search
# ---------------------------------------------------------------------------


def _minimal_optuna_data(
    n_train: int = 50,
    n_val: int = 30,
    seed: int = 42,
    pos_frac: float | None = 0.5,
    index_start: int = 0,
):
    """Return (X_train, y_train, X_val, y_val, sw_train) for run_optuna_search.

    pos_frac: fraction of positives (0..1). If None, single class (all 0).
    index_start: start index for train (0 = default contiguous); val uses index_start + n_train + i.
    """
    rng = np.random.default_rng(seed)
    n_pos_train = int(round(n_train * (pos_frac or 0)))
    n_pos_val = int(round(n_val * (pos_frac or 0))) if pos_frac is not None else 0
    y_tr = pd.Series([1] * n_pos_train + [0] * (n_train - n_pos_train), dtype="int64")
    y_vl = pd.Series([1] * n_pos_val + [0] * (n_val - n_pos_val), dtype="int64")
    if pos_frac is not None:
        y_tr = y_tr.sample(frac=1, random_state=rng).reset_index(drop=True)
        y_vl = y_vl.sample(frac=1, random_state=rng).reset_index(drop=True)
    X_tr = pd.DataFrame(
        rng.normal(size=(n_train, 4)),
        columns=list("abcd"),
        index=range(index_start, index_start + n_train),
    )
    X_vl = pd.DataFrame(
        rng.normal(size=(n_val, 4)),
        columns=list("abcd"),
        index=range(index_start + n_train, index_start + n_train + n_val),
    )
    sw_tr = pd.Series(np.ones(n_train), dtype="float64", index=X_tr.index)
    return X_tr, y_tr, X_vl, y_vl, sw_tr


def _expected_param_keys() -> set:
    """Keys that run_optuna_search returns when it runs at least one trial."""
    return {"n_estimators", "learning_rate", "max_depth", "num_leaves", "min_child_samples"}


# ---------------------------------------------------------------------------
# R410 #1 — Valid 僅 1 列時 AP 退化（鎖定：小 valid 子集時仍完成且回傳結構正確）
# ---------------------------------------------------------------------------


class TestR410_1_ValidSmallSubset(unittest.TestCase):
    """R410 #1: 當 valid 依比例抽樣後筆數很小（如 2）時，run_optuna_search 不崩潰且回傳 dict 含預期鍵。"""

    def test_small_valid_subset_completes_and_returns_params(self):
        """len(X_val)=200, r≈0.01 → n_valid=2；執行 1 trial 應完成且回傳含 n_estimators 等鍵的 dict。"""
        # Train 2000, sample 20 → r=0.01; val 200 → n_valid = max(1, int(200*0.01))=2
        n_train, n_val = 2000, 200
        sample_rows = 20
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data(n_train=n_train, n_val=n_val, pos_frac=0.3)
        with patch.object(trainer_mod, "OPTUNA_HPO_SAMPLE_ROWS", sample_rows, create=True):
            result = trainer_mod.run_optuna_search(
                X_tr, y_tr, X_vl, y_vl, sw,
                n_trials=1,
                label="r410-1",
            )
        self.assertIsInstance(result, dict, "run_optuna_search should return dict.")
        for k in _expected_param_keys():
            self.assertIn(k, result, f"result should contain key {k!r}.")


# ---------------------------------------------------------------------------
# R410 #2 — stratify + train_size=int（鎖定：有兩類且抽樣時完成並回傳；比例由實作保證）
# ---------------------------------------------------------------------------


class TestR410_2_StratifiedSubsamplingCompletes(unittest.TestCase):
    """R410 #2: 給定兩類、固定 random_state，啟用抽樣時 run_optuna_search 完成且回傳 params。"""

    def test_stratified_like_data_subsampling_returns_params(self):
        """5000 train、約 30%% 正類、sample_rows=1000、n_trials=1 → 完成且回傳含預期鍵的 dict。"""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data(
            n_train=5000, n_val=500, pos_frac=0.3
        )
        with patch.object(trainer_mod, "OPTUNA_HPO_SAMPLE_ROWS", 1000, create=True):
            result = trainer_mod.run_optuna_search(
                X_tr, y_tr, X_vl, y_vl, sw,
                n_trials=1,
                label="r410-2",
            )
        self.assertIsInstance(result, dict)
        for k in _expected_param_keys():
            self.assertIn(k, result)


# ---------------------------------------------------------------------------
# R410 #3 — Index 對齊（鎖定：非連續 index 時不拋錯、回傳 params）
# ---------------------------------------------------------------------------


class TestR410_3_NonContiguousIndex(unittest.TestCase):
    """R410 #3: X_train 為非 0 起始或非連續 index 時，啟用抽樣、n_trials=1 不拋錯且回傳 params。"""

    def test_non_contiguous_index_subsampling_completes(self):
        """Index 從 100 起始、啟用 HPO 抽樣，run_optuna_search 應完成並回傳 dict。"""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data(
            n_train=500, n_val=100, index_start=100
        )
        with patch.object(trainer_mod, "OPTUNA_HPO_SAMPLE_ROWS", 200, create=True):
            result = trainer_mod.run_optuna_search(
                X_tr, y_tr, X_vl, y_vl, sw,
                n_trials=1,
                label="r410-3",
            )
        self.assertIsInstance(result, dict)
        for k in _expected_param_keys():
            self.assertIn(k, result)


# ---------------------------------------------------------------------------
# R410 #4 — OPTUNA_HPO_SAMPLE_ROWS 為 float（鎖定：float 時不崩潰，目前實作視為未設定）
# ---------------------------------------------------------------------------


class TestR410_4_FloatSampleRows(unittest.TestCase):
    """R410 #4: OPTUNA_HPO_SAMPLE_ROWS 為 float 時不拋錯；目前實作視為未設定故不抽樣、照常完成。"""

    def test_float_sample_rows_no_crash_returns_params(self):
        """OPTUNA_HPO_SAMPLE_ROWS=1_500_000.0、train 2000 > 1.5e6 為 False，故不抽樣；應完成並回傳 dict。"""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data(n_train=2000, n_val=300)
        with patch.object(trainer_mod, "OPTUNA_HPO_SAMPLE_ROWS", 1_500_000.0, create=True):
            result = trainer_mod.run_optuna_search(
                X_tr, y_tr, X_vl, y_vl, sw,
                n_trials=1,
                label="r410-4",
            )
        self.assertIsInstance(result, dict)
        for k in _expected_param_keys():
            self.assertIn(k, result)


# ---------------------------------------------------------------------------
# R410 #5 — 單一類別 fallback（鎖定：y_train 全 0 或全 1 時完成且回傳 dict）
# ---------------------------------------------------------------------------


class TestR410_5_SingleClassFallback(unittest.TestCase):
    """R410 #5: y_train 僅一類時 stratified 會失敗，fallback 隨機抽樣；run_optuna_search 應完成並回傳 dict。"""

    def test_single_class_train_subsampling_completes(self):
        """y_train 全 0、啟用抽樣、n_trials=1 → 不拋錯且回傳 dict（fallback 路徑）。valid 保留兩類以計算 AP。"""
        X_tr, _, X_vl, y_vl, sw = _minimal_optuna_data(
            n_train=500, n_val=100, pos_frac=0.5
        )
        y_tr = pd.Series(np.zeros(len(X_tr), dtype=np.int64), index=X_tr.index)
        with patch.object(trainer_mod, "OPTUNA_HPO_SAMPLE_ROWS", 200, create=True):
            result = trainer_mod.run_optuna_search(
                X_tr, y_tr, X_vl, y_vl, sw,
                n_trials=1,
                label="r410-5",
            )
        self.assertIsInstance(result, dict)
        for k in _expected_param_keys():
            self.assertIn(k, result)


# ---------------------------------------------------------------------------
# R410 #6 — len(X_train) == OPTUNA_HPO_SAMPLE_ROWS（鎖定：相等時不進入抽樣分支）
# ---------------------------------------------------------------------------


class TestR410_6_EqualLengthNoSubsample(unittest.TestCase):
    """R410 #6: len(X_train) == OPTUNA_HPO_SAMPLE_ROWS 時不應進入抽樣分支（條件為 > 非 >=）。"""

    def test_equal_train_and_sample_rows_completes(self):
        """len(X_train)=1000、OPTUNA_HPO_SAMPLE_ROWS=1000 → 不抽樣、完成並回傳 dict。"""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data(n_train=1000, n_val=200)
        with patch.object(trainer_mod, "OPTUNA_HPO_SAMPLE_ROWS", 1000, create=True):
            result = trainer_mod.run_optuna_search(
                X_tr, y_tr, X_vl, y_vl, sw,
                n_trials=1,
                label="r410-6",
            )
        self.assertIsInstance(result, dict)
        for k in _expected_param_keys():
            self.assertIn(k, result)

    def test_source_uses_strict_gt_for_subsample_condition(self):
        """契約：run_optuna_search 內抽樣條件為 len(X_train) > _sample_rows（非 >=），避免邊界 stratify。"""
        # 鎖定現有實作，避免未來改為 >= 導致 train_test_split 邊界行為
        self.assertIn(
            "len(X_train) > _sample_rows",
            _TRAINER_SRC,
            "Subsampling condition must be strict '>' so len==sample_rows does not enter branch.",
        )
        self.assertNotRegex(
            _TRAINER_SRC,
            r"len\s*\(\s*X_train\s*\)\s*>=\s*_sample_rows",
            "Subsampling must not use '>=' (would trigger when len==sample_rows).",
        )


# ---------------------------------------------------------------------------
# R410 #7 — Config 極大值（鎖定：OPTUNA_HPO_SAMPLE_ROWS 極大時不崩潰）
# ---------------------------------------------------------------------------


class TestR410_7_HugeSampleRowsNoCrash(unittest.TestCase):
    """R410 #7: OPTUNA_HPO_SAMPLE_ROWS 設為極大（如 2**31）時不崩潰；目前 len(X_train) > _sample_rows 為 False 故不抽樣。"""

    def test_huge_sample_rows_completes(self):
        """OPTUNA_HPO_SAMPLE_ROWS=2**31、len(X_train)=10000 → 不抽樣、完成並回傳 dict。"""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data(n_train=10000, n_val=500)
        with patch.object(trainer_mod, "OPTUNA_HPO_SAMPLE_ROWS", 2**31, create=True):
            result = trainer_mod.run_optuna_search(
                X_tr, y_tr, X_vl, y_vl, sw,
                n_trials=1,
                label="r410-7",
            )
        self.assertIsInstance(result, dict)
        for k in _expected_param_keys():
            self.assertIn(k, result)


# ---------------------------------------------------------------------------
# Config 型別契約（與 R410 #4 相關）
# ---------------------------------------------------------------------------


class TestR410_ConfigTypeContract(unittest.TestCase):
    """OPTUNA_HPO_SAMPLE_ROWS 在 config 中應為 int 或 None，供日後 env 引入時防呆。"""

    def test_config_hpo_sample_rows_is_int_or_none(self):
        """trainer.config.OPTUNA_HPO_SAMPLE_ROWS 應為 (int | None)。"""
        try:
            import trainer.config as _cfg
        except Exception:
            self.skipTest("trainer.config not importable")
        val = getattr(_cfg, "OPTUNA_HPO_SAMPLE_ROWS", None)
        self.assertIsInstance(
            val,
            (int, type(None)),
            "OPTUNA_HPO_SAMPLE_ROWS must be int or None; if set via env, cast to int.",
        )


if __name__ == "__main__":
    unittest.main()
