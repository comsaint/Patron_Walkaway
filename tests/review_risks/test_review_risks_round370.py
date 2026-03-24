"""Minimal reproducible guards for reviewer risks (Round 370).

Scope:
- Convert R-NEG-1..R-NEG-7 from STATUS.md into executable checks.
- Tests only; no production code edits.
- Unresolved risks are marked expectedFailure so they stay visible in CI.
"""

from __future__ import annotations

import ast
import inspect
import re
import unittest

import trainer.trainer as trainer_mod


def _load_count(tree: ast.AST, name: str) -> int:
    """Count Name(name, Load) occurrences in an AST."""
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    )


class TestR370NegativeSamplingRiskGuards(unittest.TestCase):
    """Risk guards derived from STATUS.md self-review items (all resolved)."""

    def test_chunk_cache_key_includes_neg_sample_frac(self):
        """R-NEG-1 resolved: cache key includes neg_sample_frac to avoid stale cache hits."""
        src = inspect.getsource(trainer_mod._chunk_cache_key)
        self.assertIn(
            "neg_sample_frac",
            src,
            "_chunk_cache_key should include neg_sample_frac in signature/key payload",
        )

    def test_process_chunk_passes_neg_sample_frac_into_cache_key(self):
        """R-NEG-1 resolved: process_chunk forwards neg_sample_frac into cache key."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertRegex(
            src,
            r"_chunk_cache_components\([^)]*neg_sample_frac\s*=\s*neg_sample_frac",
            "process_chunk should forward neg_sample_frac when building cache components",
        )

    def test_training_metrics_records_neg_sample_frac(self):
        """R-NEG-2 resolved: training_metrics.json persists effective neg_sample_frac."""
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertIn(
            '"neg_sample_frac"',
            src,
            "save_artifact_bundle should write neg_sample_frac into training_metrics.json",
        )

    def test_oom_check_no_unused_total_ram_assignment(self):
        """R-NEG-3 resolved: total_ram is assigned and read (included in print/log)."""
        src = inspect.getsource(trainer_mod._oom_check_and_adjust_neg_sample_frac)
        tree = ast.parse(src)
        assigns = [
            node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ]
        self.assertIn("total_ram", assigns, "Sanity: expected total_ram assignment in current code")
        self.assertGreater(
            _load_count(tree, "total_ram"),
            0,
            "total_ram is assigned but never read; remove or include in logs/calculation",
        )

    def test_oom_check_validates_assumed_pos_rate_range(self):
        """R-NEG-4 resolved: ASSUMED_POS_RATE validated in (0, 1) before formula."""
        src = inspect.getsource(trainer_mod._oom_check_and_adjust_neg_sample_frac)
        self.assertRegex(
            src,
            r"0\.0\s*<\s*NEG_SAMPLE_FRAC_ASSUMED_POS_RATE\s*<\s*1\.0",
            "Expected explicit range validation for NEG_SAMPLE_FRAC_ASSUMED_POS_RATE",
        )

    def test_oom_check_logs_total_ram_alongside_available(self):
        """R-NEG-5 resolved: OOM check reports both total and available RAM."""
        src = inspect.getsource(trainer_mod._oom_check_and_adjust_neg_sample_frac)
        self.assertTrue(
            re.search(r"total.*available|available.*total", src, flags=re.IGNORECASE),
            "OOM-check should report both total and available RAM",
        )

    def test_neg_sampling_seed_not_hardcoded_constant(self):
        """R-NEG-6 resolved: chunk-specific seed replaces fixed random_state=42."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertNotIn(
            "random_state=42",
            src,
            "Prefer chunk-specific seed instead of fixed random_state=42",
        )

    def test_neg_sampling_frac_zero_has_explicit_guard(self):
        """R-NEG-7 resolved: explicit error logged when all negatives are removed."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn(
            "removed ALL negatives",
            src,
            "Expected explicit guard message when neg sampling yields zero negatives",
        )


if __name__ == "__main__":
    unittest.main()
