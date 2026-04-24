from __future__ import annotations

import unittest

from trainer.core import config


class TestDuckDbRuntimePolicy(unittest.TestCase):
    def test_resolve_policy_none_available_falls_back_to_min(self):
        policy = config.resolve_duckdb_runtime_policy("profile", None)
        self.assertIn("memory_limit_bytes", policy)
        self.assertGreater(policy["memory_limit_bytes"], 0)
        self.assertEqual(policy["threads"], max(1, int(config.DUCKDB_THREADS)))

    def test_resolve_policy_step7_large_input_reduces_threads(self):
        available = 10 * 1024**3
        input_bytes = int(available * 0.40)
        policy = config.resolve_duckdb_runtime_policy("step7", available, input_bytes=input_bytes)
        self.assertEqual(policy["threads"], 1)
        self.assertIn("duckdb_tmp", policy["temp_directory"])

    def test_resolve_policy_screening_has_stage_specific_threads(self):
        policy = config.resolve_duckdb_runtime_policy("screening", 8 * 1024**3, input_bytes=1)
        self.assertEqual(policy["stage"], "screening")
        self.assertEqual(policy["threads"], max(1, int(config.SCREENING_DUCKDB_THREADS)))

    def test_apply_runtime_executes_required_statements(self):
        class _FakeCon:
            def __init__(self) -> None:
                self.sql: list[str] = []

            def execute(self, stmt: str) -> None:
                self.sql.append(stmt)

        fake = _FakeCon()
        policy = config.resolve_duckdb_runtime_policy("canonical_map", 8 * 1024**3)
        config.apply_duckdb_runtime(fake, policy)
        body = "\n".join(fake.sql)
        self.assertIn("SET memory_limit=", body)
        self.assertIn("SET threads=", body)
        self.assertIn("SET temp_directory=", body)


if __name__ == "__main__":
    unittest.main()
