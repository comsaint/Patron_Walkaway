"""Task 7 chunk-cache review risks -> executable MRE guards.

Scope:
- Tests only; do not modify production code.
- Convert review findings into minimal reproducible checks.
"""

from __future__ import annotations

import inspect
import re
import unittest

import trainer.trainer as trainer_mod


class TestTask7ChunkCacheReviewRisksMRE(unittest.TestCase):
    def test_risk1_data_hash_truncated_to_8_hex(self) -> None:
        """MRE: data hash currently truncated to 8 hex chars (32-bit)."""
        src = inspect.getsource(trainer_mod._commutative_frame_row_digest)
        self.assertRegex(
            src,
            r"return\s+digest\[:8\]",
            "Expected current implementation to truncate data hash to 8 hex chars",
        )

    def test_risk1_uses_commutative_sum_xor_sqsum_signature(self) -> None:
        """MRE: order-insensitive signature uses sum/xor/sq_sum reducers."""
        src = inspect.getsource(trainer_mod._commutative_frame_row_digest)
        self.assertIn("sum64", src)
        self.assertIn("xor64", src)
        self.assertIn("sq_sum64", src)

    def test_risk2_cache_hit_path_has_no_corruption_probe(self) -> None:
        """MRE: cache-hit branch returns path directly without lightweight parquet probe."""
        src = inspect.getsource(trainer_mod.process_chunk)
        # Local + ClickHouse paths each nest `if stored_key == current_key` under
        # `if not force_recompute and chunk_path.exists()`; `else:` aligns at 12 spaces.
        pat = re.compile(
            r"if\s+stored_key\s*==\s*current_key:\n"
            r"(?P<body>(?:\s{12}.+\n)+?)"
            r"\s{12}else:",
        )
        matches = list(pat.finditer(src))
        self.assertGreaterEqual(len(matches), 1, "Unable to locate cache-hit branch in process_chunk")
        for m in matches:
            body = m.group("body")
            self.assertIn("return chunk_path", body)
            self.assertNotRegex(
                body,
                r"read_parquet|ParquetFile|SELECT 1|cache corrupt|except",
                "Current cache-hit branch is expected to skip corruption probe",
            )

    def test_risk3_sqsum_multiplies_full_hash_array(self) -> None:
        """MRE: sq_sum path currently allocates row_hash * row_hash temporary array."""
        src = inspect.getsource(trainer_mod._commutative_frame_row_digest)
        self.assertRegex(
            src,
            r"row_hash\s*\*\s*row_hash",
            "Expected current implementation to contain row_hash * row_hash expression",
        )

    def test_risk4_boundary_contract_cases_not_present_in_task7_unit_tests(self) -> None:
        """MRE: current Task7 unit test file does not include boundary contract cases."""
        import pathlib

        unit_test_path = pathlib.Path(__file__).resolve().parents[1] / "unit" / "test_task7_chunk_cache_key.py"
        content = unit_test_path.read_text(encoding="utf-8")
        self.assertNotIn("empty", content.lower())
        self.assertNotIn("dtype", content.lower())
        self.assertNotIn("column_order", content.lower())


if __name__ == "__main__":
    unittest.main()
