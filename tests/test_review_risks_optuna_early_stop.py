"""Minimal reproducible tests for Code Review — Optuna 整份 study 的 early stop 變更.

STATUS.md «Code Review — Optuna 整份 study 的 early stop 變更» 的風險點轉成可重現測試。
僅新增 tests，不改 production code。
預期尚未修復的項目使用 @unittest.expectedFailure，待 production 修正後移除。
"""

from __future__ import annotations

import unittest
import unittest.mock
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.trainer as trainer_mod

# ---------------------------------------------------------------------------
# Helpers: minimal data for run_optuna_search (non-empty val, both classes)
# ---------------------------------------------------------------------------


def _minimal_optuna_data(n_train: int = 50, n_val: int = 30, seed: int = 42):
    """Return (X_tr, y_tr, X_vl, y_vl, sw_train) for run_optuna_search."""
    rng = np.random.default_rng(seed)
    X_tr = pd.DataFrame(rng.normal(size=(n_train, 4)), columns=list("abcd"))
    y_tr = pd.Series(([0, 1] * ((n_train // 2) + 1))[:n_train], dtype="int64")
    sw_tr = pd.Series(np.ones(n_train), dtype="float64")
    X_vl = pd.DataFrame(rng.normal(size=(n_val, 4)), columns=list("abcd"))
    y_vl = pd.Series(([0, 1] * ((n_val // 2) + 1))[:n_val], dtype="int64")
    return X_tr, y_tr, X_vl, y_vl, sw_tr


# ---------------------------------------------------------------------------
# Review #1 — Config 型別非 int 時可能 TypeError
# ---------------------------------------------------------------------------


class TestOptunaEarlyStopReview1ConfigType(unittest.TestCase):
    """Review #1: OPTUNA_EARLY_STOP_PATIENCE 為 str 時不應拋錯，行為等同關閉 early stop."""

    def test_patience_str_does_not_raise_and_returns_dict(self):
        """當 OPTUNA_EARLY_STOP_PATIENCE='50' 時，run_optuna_search 不應 TypeError，應完成並回傳 dict."""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data()
        with patch.object(trainer_mod, "OPTUNA_EARLY_STOP_PATIENCE", "50"):
            result = trainer_mod.run_optuna_search(
                X_tr, y_tr, X_vl, y_vl, sw,
                n_trials=3,
                label="review1-str",
            )
        self.assertIsInstance(result, dict, "run_optuna_search should return dict when patience is str (treated as disabled).")

    def test_patience_zero_does_not_add_early_stop_callback(self):
        """OPTUNA_EARLY_STOP_PATIENCE=0 時應跑滿 n_trials，不觸發 early stop（stop 未被呼叫）."""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data()
        stop_calls = []

        orig_stop = getattr(trainer_mod.optuna.Study, "stop")

        def track_stop(self):
            stop_calls.append(True)
            return orig_stop(self)

        with patch.object(trainer_mod, "OPTUNA_EARLY_STOP_PATIENCE", 0):
            with patch.object(trainer_mod.optuna.Study, "stop", track_stop):
                trainer_mod.run_optuna_search(
                    X_tr, y_tr, X_vl, y_vl, sw,
                    n_trials=3,
                    label="review1-zero",
                )
        self.assertEqual(len(stop_calls), 0, "patience=0 should not add early-stop callback so stop() is never called.")


# ---------------------------------------------------------------------------
# Review #2 — Trial 失敗時 study.best_value 為 None，callback 不應崩潰
# ---------------------------------------------------------------------------


class TestOptunaEarlyStopReview2FailedTrials(unittest.TestCase):
    """Review #2: 前幾次 trial 失敗（best_value 不可用）時 callback 不拋錯、不誤觸 study.stop()."""

    def test_objective_fails_twice_then_succeeds_completes_without_crash(self):
        """Objective 前 2 次失敗、之後成功時，run_optuna_search 應完成不拋錯。
        目前因 _progress_callback 在「尚無完成 trial」時讀 study.best_value 會觸發 Optuna 的 ValueError，
        與 Review #3 同根因；修復 #3 後此測試應可通過。"""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data()
        call_count = [0]

        def mock_ap(y_true, y_score):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise ValueError("simulated trial failure")
            return 0.5

        orig_optimize = trainer_mod.optuna.Study.optimize

        def optimize_with_catch(self, func, n_trials=None, timeout=None, callbacks=None, **kwargs):
            return orig_optimize(
                self, func,
                n_trials=n_trials,
                timeout=timeout,
                callbacks=callbacks or [],
                catch=(ValueError,),
                **kwargs,
            )

        with patch.object(trainer_mod, "average_precision_score", side_effect=mock_ap):
            with patch.object(trainer_mod.optuna.Study, "optimize", optimize_with_catch):
                result = trainer_mod.run_optuna_search(
                    X_tr, y_tr, X_vl, y_vl, sw,
                    n_trials=5,
                    label="review2-fail-twice",
                )
        self.assertIsInstance(result, dict)
        self.assertGreaterEqual(call_count[0], 3, "at least 3 trials should run (2 fail + 1+ succeed).")


# ---------------------------------------------------------------------------
# Review #3 — _progress_callback 在 best_value 為 None 時 format 錯誤
# ---------------------------------------------------------------------------


class TestOptunaEarlyStopReview3ProgressCallbackNoneBest(unittest.TestCase):
    """Review #3: 第一個 trial 失敗時 _progress_callback 以 best_value=None 印 log 不應 TypeError."""

    def test_first_trial_fails_progress_callback_does_not_raise(self):
        """第一個 trial 失敗時 progress 仍會觸發（n=1）；best_value 不可用時 log 不應拋錯."""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data()
        call_count = [0]

        def mock_ap(y_true, y_score):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("simulated first trial failure")
            return 0.5

        orig_optimize = trainer_mod.optuna.Study.optimize

        def optimize_with_catch(self, func, n_trials=None, timeout=None, callbacks=None, **kwargs):
            return orig_optimize(
                self, func,
                n_trials=n_trials,
                timeout=timeout,
                callbacks=callbacks or [],
                catch=(ValueError,),
                **kwargs,
            )

        with patch.object(trainer_mod, "average_precision_score", side_effect=mock_ap):
            with patch.object(trainer_mod.optuna.Study, "optimize", optimize_with_catch):
                result = trainer_mod.run_optuna_search(
                    X_tr, y_tr, X_vl, y_vl, sw,
                    n_trials=2,
                    label="review3-first-fail",
                )
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# Review #4 — patience 遠大於 n_trials 時不應呼叫 study.stop()
# ---------------------------------------------------------------------------


class TestOptunaEarlyStopReview4PatienceGtNTrials(unittest.TestCase):
    """Review #4: patience > n_trials 時跑滿 n_trials，不觸發 early stop."""

    def test_patience_100_n_trials_5_stop_never_called(self):
        """OPTUNA_EARLY_STOP_PATIENCE=100、n_trials=5 時 study.stop() 不應被呼叫."""
        X_tr, y_tr, X_vl, y_vl, sw = _minimal_optuna_data()
        stop_calls = []
        orig_stop = getattr(trainer_mod.optuna.Study, "stop")

        def track_stop(self):
            stop_calls.append(True)
            return orig_stop(self)

        with patch.object(trainer_mod, "OPTUNA_EARLY_STOP_PATIENCE", 100):
            with patch.object(trainer_mod.optuna.Study, "stop", track_stop):
                trainer_mod.run_optuna_search(
                    X_tr, y_tr, X_vl, y_vl, sw,
                    n_trials=5,
                    label="review4-patience-gt-n",
                )
        self.assertEqual(len(stop_calls), 0, "patience=100 and n_trials=5 should not trigger early stop.")


# ---------------------------------------------------------------------------
# Config 型別契約（與 Review #1 相關的 lint/type 層面）
# ---------------------------------------------------------------------------


class TestOptunaEarlyStopConfigTypeContract(unittest.TestCase):
    """OPTUNA_EARLY_STOP_PATIENCE 應為 int 或 None，供日後 env 引入時防呆."""

    def test_config_early_stop_patience_is_int_or_none(self):
        """config.OPTUNA_EARLY_STOP_PATIENCE 應為 (int | None)，避免日後 os.getenv 未轉 int 導致 TypeError."""
        try:
            import trainer.config as _cfg
        except Exception:
            self.skipTest("trainer.config not importable")
        val = getattr(_cfg, "OPTUNA_EARLY_STOP_PATIENCE", None)
        self.assertIsInstance(
            val,
            (int, type(None)),
            "OPTUNA_EARLY_STOP_PATIENCE must be int or None; if set via env, cast to int.",
        )


if __name__ == "__main__":
    unittest.main()
