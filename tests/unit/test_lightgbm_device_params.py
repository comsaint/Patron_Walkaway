"""Unit tests for LightGBM device-aware params (GPU plan Phase A)."""

from __future__ import annotations

import types
import unittest

import trainer.training.trainer as tr


class TestLgbParamsForPipeline(unittest.TestCase):
    def tearDown(self) -> None:
        tr._EFFECTIVE_LIGHTGBM_DEVICE = tr.LIGHTGBM_DEVICE_TYPE
        tr._REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS = tr.TRAINER_DEVICE_MODE

    def test_cpu_branch_has_force_col_wise_and_n_jobs_minus_one(self) -> None:
        tr._EFFECTIVE_LIGHTGBM_DEVICE = "cpu"
        p = tr._lgb_params_for_pipeline()
        self.assertEqual(p.get("device_type"), "cpu")
        self.assertEqual(p.get("force_col_wise"), True)
        self.assertEqual(p.get("n_jobs"), -1)
        self.assertEqual(p.get("objective"), "binary")

    def test_gpu_branch_omits_force_col_wise_uses_gpu_n_jobs(self) -> None:
        tr._EFFECTIVE_LIGHTGBM_DEVICE = "gpu"
        p = tr._lgb_params_for_pipeline()
        self.assertEqual(p.get("device_type"), "gpu")
        self.assertNotIn("force_col_wise", p)
        self.assertGreaterEqual(int(p.get("n_jobs", 0)), 1)

    def test_backend_runtime_params_for_gpu_backends(self) -> None:
        cat_params = tr.backend_runtime_params_for_backend(
            "catboost",
            device_mode="gpu",
            gpu_id="1",
        )
        xgb_params = tr.backend_runtime_params_for_backend(
            "xgboost",
            device_mode="gpu",
            gpu_id="2",
        )
        self.assertEqual(cat_params["task_type"], "GPU")
        self.assertEqual(cat_params["devices"], "1")
        self.assertEqual(xgb_params["device"], "cuda:2")
        self.assertEqual(xgb_params["tree_method"], "hist")

    def test_resolve_gbm_backend_runtime_plan_parallelizes_when_multiple_gpus_visible(self) -> None:
        with unittest.mock.patch.object(tr, "TRAINER_DEVICE_MODE", "auto"), unittest.mock.patch.object(
            tr,
            "GBM_BAKEOFF_MAX_PARALLEL_BACKENDS",
            0,
        ), unittest.mock.patch.object(tr, "discover_visible_gpu_ids", return_value=["0", "1"]):
            plan = tr.resolve_gbm_backend_runtime_plan()
        self.assertEqual(plan["trainer_device_mode_requested"], "auto")
        self.assertEqual(plan["effective_backend_device_mode"], "gpu")
        self.assertEqual(plan["parallel_backend_workers"], 2)
        self.assertTrue(plan["parallel_backend_execution"])
        self.assertEqual(plan["gpu_assignments"]["catboost"], "0")
        self.assertEqual(plan["gpu_assignments"]["xgboost"], "1")

    def test_resolve_gbm_backend_runtime_plan_cpu_when_trainer_device_mode_cpu(self) -> None:
        with unittest.mock.patch.object(tr, "TRAINER_DEVICE_MODE", "cpu"), unittest.mock.patch.object(
            tr, "discover_visible_gpu_ids", return_value=["0", "1"]
        ):
            plan = tr.resolve_gbm_backend_runtime_plan()
        self.assertEqual(plan["effective_backend_device_mode"], "cpu")
        self.assertFalse(plan["parallel_backend_execution"])

    def test_configure_lightgbm_respects_trainer_device_mode_cpu(self) -> None:
        args = types.SimpleNamespace(lgbm_device=None)
        with unittest.mock.patch.object(tr, "TRAINER_DEVICE_MODE", "cpu"):
            tr.configure_lightgbm_device_for_run(args)
        self.assertEqual(tr._EFFECTIVE_LIGHTGBM_DEVICE, "cpu")
        self.assertEqual(tr._REQUESTED_TRAINER_DEVICE_MODE_FOR_METRICS, "cpu")
        self.assertFalse(tr._LIGHTGBM_GPU_FALLBACK_USED)

    def test_configure_lightgbm_auto_uses_cpu_when_probe_fails(self) -> None:
        args = types.SimpleNamespace(lgbm_device=None)
        with (
            unittest.mock.patch.object(tr, "TRAINER_DEVICE_MODE", "auto"),
            unittest.mock.patch.object(tr, "_lightgbm_gpu_probe_ok", return_value=False),
        ):
            tr.configure_lightgbm_device_for_run(args)
        self.assertEqual(tr._EFFECTIVE_LIGHTGBM_DEVICE, "cpu")
        self.assertFalse(tr._LIGHTGBM_GPU_FALLBACK_USED)

    def test_configure_lightgbm_gpu_sets_fallback_when_probe_fails(self) -> None:
        args = types.SimpleNamespace(lgbm_device=None)
        with (
            unittest.mock.patch.object(tr, "TRAINER_DEVICE_MODE", "gpu"),
            unittest.mock.patch.object(tr, "_lightgbm_gpu_probe_ok", return_value=False),
        ):
            tr.configure_lightgbm_device_for_run(args)
        self.assertEqual(tr._EFFECTIVE_LIGHTGBM_DEVICE, "cpu")
        self.assertTrue(tr._LIGHTGBM_GPU_FALLBACK_USED)


if __name__ == "__main__":
    unittest.main()
