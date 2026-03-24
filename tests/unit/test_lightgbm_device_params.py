"""Unit tests for LightGBM device-aware params (GPU plan Phase A)."""

from __future__ import annotations

import unittest

import trainer.training.trainer as tr


class TestLgbParamsForPipeline(unittest.TestCase):
    def tearDown(self) -> None:
        tr._EFFECTIVE_LIGHTGBM_DEVICE = tr.LIGHTGBM_DEVICE_TYPE

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


if __name__ == "__main__":
    unittest.main()
