"""LightGBM GPU Phase A code review -> executable MRE guards (tests only).

Encodes STATUS.md § Code Review — LightGBM GPU Phase A（R1–R8）現行行為／風險，
不修改 production；若日後加固（sanitize、strip hyperparams、clamp、skip probe 等），
請同步調整或刪除對應斷言。
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import trainer.training.trainer as tr


_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestLightgbmGpuPhaseAReviewRisksMRE(unittest.TestCase):
    # --- R1: 繞過 run_pipeline 時不會自動 configure / probe ---

    def test_risk1_train_single_rated_model_does_not_call_configure_device(self) -> None:
        src = inspect.getsource(tr.train_single_rated_model)
        self.assertNotIn(
            "configure_lightgbm_device_for_run",
            src,
            "MRE: direct train_single_rated_model never invokes configure (probe only via run_pipeline).",
        )

    def test_risk1_run_optuna_search_does_not_call_configure_device(self) -> None:
        src = inspect.getsource(tr.run_optuna_search)
        self.assertNotIn(
            "configure_lightgbm_device_for_run",
            src,
            "MRE: Optuna path does not invoke configure_lightgbm_device_for_run.",
        )

    def test_risk1_run_pipeline_calls_configure_device(self) -> None:
        src = inspect.getsource(tr.run_pipeline)
        self.assertIn(
            "configure_lightgbm_device_for_run",
            src,
            "Sanity: pipeline entry is where device is resolved/probed.",
        )

    # --- R2: _EFFECTIVE_LIGHTGBM_DEVICE 非 cpu/gpu 時原樣進 params（無 sanitize）---

    def test_risk2_invalid_effective_device_passes_through_lgb_params(self) -> None:
        old = tr._EFFECTIVE_LIGHTGBM_DEVICE
        tr._EFFECTIVE_LIGHTGBM_DEVICE = "cuda"
        try:
            p = tr._lgb_params_for_pipeline()
            self.assertEqual(
                p.get("device_type"),
                "cuda",
                "MRE (today): invalid device string is not normalized — LightGBM may error later.",
            )
        finally:
            tr._EFFECTIVE_LIGHTGBM_DEVICE = old

    # --- R3: core config 不接受 cuda 字串，會落到 cpu ---

    def test_risk3_env_lightgbm_device_type_cuda_import_defaults_cpu_subprocess(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT), "LIGHTGBM_DEVICE_TYPE": "cuda"}
        cmd = [
            sys.executable,
            "-c",
            "from trainer.core import config; print(config.LIGHTGBM_DEVICE_TYPE)",
        ]
        r = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            r.stdout.strip(),
            "cpu",
            "MRE: Linux CUDA users cannot select device_type=cuda via this env today.",
        )

    def test_risk3b_env_lightgbm_cuda_infers_trainer_device_mode_auto_subprocess(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT), "LIGHTGBM_DEVICE_TYPE": "cuda"}
        cmd = [
            sys.executable,
            "-c",
            "from trainer.core import config; print(config.TRAINER_DEVICE_MODE)",
        ]
        r = subprocess.run(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(
            r.stdout.strip(),
            "auto",
            "MRE: invalid LIGHTGBM_DEVICE_TYPE does not force unified TRAINER_DEVICE_MODE to gpu.",
        )

    # --- R4: hyperparams 可覆寫 pipeline 的 device_type ---

    def test_risk4_hyperparams_device_type_overrides_pipeline_effective_gpu(self) -> None:
        old_eff = tr._EFFECTIVE_LIGHTGBM_DEVICE
        tr._EFFECTIVE_LIGHTGBM_DEVICE = "gpu"
        try:
            X_tr = pd.DataFrame({"f": np.arange(20, dtype=np.float64)})
            y_tr = pd.Series([0, 1] * 10)
            X_vl = X_tr.head(0)
            y_vl = y_tr.head(0)
            sw = pd.Series([1.0] * 20)
            hp = {
                "n_estimators": 5,
                "learning_rate": 0.1,
                "num_leaves": 15,
                "max_depth": 4,
                "min_child_samples": 5,
                "colsample_bytree": 1.0,
                "subsample": 1.0,
                "subsample_freq": 1,
                "reg_alpha": 0.0,
                "reg_lambda": 0.0,
                "device_type": "cpu",
            }
            model, _metrics = tr._train_one_model(
                X_tr, y_tr, X_vl, y_vl, sw, hp, label="mre_r4", log_results=False
            )
            self.assertEqual(
                model.get_params().get("device_type"),
                "cpu",
                "MRE: merge order lets hyperparams override _lgb_params_for_pipeline device.",
            )
        finally:
            tr._EFFECTIVE_LIGHTGBM_DEVICE = old_eff

    # --- R5: GPU probe 未對齊 class_weight 等正式訓練欄位 ---

    def test_risk5_gpu_probe_source_omits_class_weight(self) -> None:
        src = inspect.getsource(tr._lightgbm_gpu_probe_ok)
        self.assertNotIn(
            "class_weight",
            src,
            "MRE: probe uses a minimal LGBMClassifier; formal training adds class_weight etc.",
        )

    # --- R6: 裝置狀態為模組全域（併行風險文件化）---

    def test_risk6_device_state_is_module_level_global(self) -> None:
        self.assertIn("_EFFECTIVE_LIGHTGBM_DEVICE", tr.__dict__)
        self.assertIn("_LIGHTGBM_GPU_FALLBACK_USED", tr.__dict__)
        self.assertIn("_REQUESTED_LIGHTGBM_DEVICE_FOR_METRICS", tr.__dict__)
        self.assertIn("_REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS", tr.__dict__)
        self.assertIn("TRAINER_DEVICE_MODE", tr.__dict__)

    # --- R7: LIGHTGBM_GPU_N_JOBS 無上限 clamp ---

    def test_risk7_gpu_n_jobs_not_clamped_in_lgb_params(self) -> None:
        old_eff = tr._EFFECTIVE_LIGHTGBM_DEVICE
        old_nj = tr.LIGHTGBM_GPU_N_JOBS
        tr._EFFECTIVE_LIGHTGBM_DEVICE = "gpu"
        tr.LIGHTGBM_GPU_N_JOBS = 99999
        try:
            p = tr._lgb_params_for_pipeline()
            self.assertEqual(p["n_jobs"], 99999)
        finally:
            tr._EFFECTIVE_LIGHTGBM_DEVICE = old_eff
            tr.LIGHTGBM_GPU_N_JOBS = old_nj

    # --- R8: 尚未支援略過 probe 的 env 開關（若加入需改此測試）---

    def test_risk8_no_lightgbm_skip_gpu_probe_env_handling(self) -> None:
        trainer_py = Path(tr.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "LIGHTGBM_SKIP_GPU_PROBE",
            trainer_py,
            "MRE: optional skip-probe env is not implemented; add test when it exists.",
        )


if __name__ == "__main__":
    unittest.main()
