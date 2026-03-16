"""Minimal reproducible tests for Round 180 Review — Step 7 Layer 2 OOM failsafe.

Review risks (Round 180 Review in STATUS.md) are turned into source/contract tests.
Tests that document current gaps use @unittest.expectedFailure until production is fixed.
Tests-only: no production code changes.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


def _get_trainer_source() -> str:
    path = Path(__file__).resolve().parents[2] / "trainer" / "training" / "trainer.py"
    return path.read_text(encoding="utf-8")


def _find_step7_sort_and_split_body(source: str) -> str | None:
    """Return the body of _step7_sort_and_split (from def to next section/def)."""
    start = source.find("def _step7_sort_and_split(")
    if start == -1:
        return None
    rest = source[start:]
    end_match = re.search(r"\n    # [0-9]+\. Load all chunks|\n    def [a-z_]+\(|\n    # [0-9]+\. ", rest)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


def _find_layer2_block(body: str) -> str | None:
    """Return the Layer 2 block: from 'current = current_neg_frac' through the while True loop."""
    start = body.find("current = current_neg_frac")
    if start == -1:
        return None
    rest = body[start:]
    # Up to the next outer block (if _is_duckdb_oom(exc): logger.warning... for the non-Layer2 path)
    end_match = re.search(r"\n            if _is_duckdb_oom\(exc\):", rest)
    end = end_match.start() if end_match else len(rest)
    return rest[:end]


class TestR180Layer2Step6OomNoInfiniteLoop(unittest.TestCase):
    """Round 180 Review P0: When step6_runner raises OOM we must not continue (infinite loop).

    Contract: the except branch that does 'current = new_frac; continue' must only run when
    the exception came from after step6_runner returned (e.g. a flag set after
    chunk_paths = step6_runner(new_frac) and used in the continue condition).
    """

    def test_r180_layer2_continue_guarded_by_step6_completed_flag(self):
        """Layer 2 except: continue must be guarded by a flag set after step6_runner returns."""
        source = _get_trainer_source()
        body = _find_step7_sort_and_split_body(source)
        self.assertIsNotNone(body, "_step7_sort_and_split not found")
        layer2 = _find_layer2_block(body)
        self.assertIsNotNone(layer2, "Layer 2 block (current = current_neg_frac ...) not found")
        # Require a variable set after step6_runner and used in the OOM-retry continue condition.
        has_step6_done_after_runner = re.search(
            r"chunk_paths\s*=\s*step6_runner\s*\([^)]+\)\s*\n\s*(?:if\s+not\s+chunk_paths.*\n\s*raise.*\n\s*)?"
            r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*True",
            layer2,
        ) or "step6_completed" in layer2 or "step6_done" in layer2 or "chunk_paths_updated" in layer2
        # And that flag must appear in the condition that leads to continue (allow lines between current=new_frac and continue)
        continue_condition = re.search(
            r"except Exception as retry_exc:.*?if\s+(.*?):\s*\n\s*logger\.warning.*\n.*?current\s*=\s*new_frac.*?\n\s*continue",
            layer2,
            re.DOTALL,
        )
        has_guard_in_condition = (
            continue_condition is not None
            and (
                "step6_completed" in (continue_condition.group(1) if continue_condition else "")
                or "step6_done" in (continue_condition.group(1) if continue_condition else "")
                or "chunk_paths_updated" in (continue_condition.group(1) if continue_condition else "")
            )
        )
        self.assertTrue(
            has_step6_done_after_runner and has_guard_in_condition,
            "Layer 2 must set a flag after step6_runner(new_frac) and use it in the "
            "condition for 'current = new_frac; continue' to avoid infinite loop when "
            "step6_runner raises OOM (Round 180 Review P0).",
        )


class TestR180Layer2MaxRetries(unittest.TestCase):
    """Round 180 Review P1: PLAN requires '最多 retry 數次（例如 3 次）'."""

    def test_r180_layer2_has_bounded_retry_count(self):
        """Layer 2 while loop must be bounded by a retry counter (e.g. retries_left or range(3))."""
        source = _get_trainer_source()
        body = _find_step7_sort_and_split_body(source)
        self.assertIsNotNone(body)
        layer2 = _find_layer2_block(body)
        self.assertIsNotNone(layer2)
        has_retry_bound = (
            "retries_left" in layer2
            or "max_retries" in layer2
            or "retry_count" in layer2
            or re.search(r"range\s*\(\s*3\s*\)", layer2)
            or re.search(r"for\s+.*\s+in\s+range\s*\(\s*\d+\s*\)", layer2)
        )
        self.assertTrue(
            has_retry_bound,
            "Layer 2 must bound retries (e.g. retries_left, max_retries, or range(3)) per PLAN (Round 180 Review P1).",
        )


class TestR180Layer2CleanupOnReadFailure(unittest.TestCase):
    """Round 180 Review P2: In retry loop except, unlink split parquets before fallback/continue."""

    def test_r180_layer2_except_cleans_split_parquets(self):
        """Layer 2 except branch must unlink train_path/valid_path/test_path before fallback or continue."""
        source = _get_trainer_source()
        body = _find_step7_sort_and_split_body(source)
        self.assertIsNotNone(body)
        layer2 = _find_layer2_block(body)
        self.assertIsNotNone(layer2)
        # In the except Exception as retry_exc block we need cleanup of the three paths
        # before "return _step7_pandas_fallback" or before "continue"
        except_block = re.search(
            r"except Exception as retry_exc:\s*\n(.*?)(?=return _step7_pandas_fallback|continue)",
            layer2,
            re.DOTALL,
        )
        self.assertIsNotNone(except_block, "Layer 2 except retry_exc block not found")
        block_content = except_block.group(1) if except_block else ""
        has_unlink = (
            "unlink" in block_content
            and ("train_path" in block_content or "valid_path" in block_content or "test_path" in block_content)
        ) or ("missing_ok" in block_content and "path" in block_content)
        self.assertTrue(
            has_unlink,
            "Layer 2 except (retry_exc) must unlink train_path/valid_path/test_path before "
            "fallback or continue (Round 180 Review P2).",
        )


class TestR180Layer2CurrentNegFracValidated(unittest.TestCase):
    """Round 180 Review P2: current_neg_frac must be validated (0, 1] before Layer 2 loop."""

    def test_r180_layer2_validates_current_neg_frac_before_loop(self):
        """Before while True (or before _step7_oom_failsafe_next_frac), validate current_neg_frac in (0, 1]."""
        source = _get_trainer_source()
        body = _find_step7_sort_and_split_body(source)
        self.assertIsNotNone(body)
        layer2 = _find_layer2_block(body)
        self.assertIsNotNone(layer2)
        # Before "while True" we need a check like 0 < current_neg_frac <= 1 or similar
        before_while = re.search(r"current = current_neg_frac\s*\n(.*?)while True:", layer2, re.DOTALL)
        block = before_while.group(1) if before_while else ""
        has_validation = (
            "current_neg_frac" in block
            and (
                "0 <" in block
                or "<= 1" in block
                or "> 0" in block
                or "(0, 1]" in block
                or "0.0 <" in block
            )
        )
        self.assertTrue(
            has_validation,
            "Layer 2 must validate current_neg_frac in (0, 1] before the while loop (Round 180 Review P2).",
        )


if __name__ == "__main__":
    unittest.main()
