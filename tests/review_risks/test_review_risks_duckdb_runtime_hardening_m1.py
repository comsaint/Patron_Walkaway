from __future__ import annotations

import inspect
import unittest

import trainer.features as features_mod
import trainer.trainer as trainer_mod


class TestDuckDbRuntimeHardeningM1SourceContracts(unittest.TestCase):
    def test_track_llm_uses_shared_runtime_policy(self):
        src = inspect.getsource(features_mod.compute_track_llm_features)
        self.assertIn("resolve_duckdb_runtime_policy(", src)
        self.assertIn("apply_duckdb_runtime(", src)

    def test_step8_helpers_use_shared_runtime_policy(self):
        src_std = inspect.getsource(features_mod.compute_column_std_duckdb)
        src_corr = inspect.getsource(features_mod.compute_correlation_matrix_duckdb)
        self.assertIn("resolve_duckdb_runtime_policy(", src_std)
        self.assertIn("apply_duckdb_runtime(", src_std)
        self.assertIn("resolve_duckdb_runtime_policy(", src_corr)
        self.assertIn("apply_duckdb_runtime(", src_corr)

    def test_step7_and_canonical_use_shared_runtime_hooks(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn("resolve_duckdb_runtime_policy", src)
        self.assertIn("apply_duckdb_runtime", src)
        src_canonical = inspect.getsource(trainer_mod.build_canonical_links_and_dummy_from_duckdb)
        self.assertIn("resolve_duckdb_runtime_policy", src_canonical)
        self.assertIn("apply_duckdb_runtime", src_canonical)

    def test_libsvm_export_uses_shared_runtime_policy(self):
        src = inspect.getsource(trainer_mod._export_parquet_to_libsvm)
        self.assertIn("resolve_duckdb_runtime_policy", src)
        self.assertIn("apply_duckdb_runtime", src)


if __name__ == "__main__":
    unittest.main()
