"""Minimal reproducible tests for Round 210 Review — OOM 探針（Chunk 1）風險點.

Review risks (Round 210 Review in STATUS.md) are turned into contract/behavior tests.
Tests that document desired behaviour not yet in production use @unittest.expectedFailure.
Tests-only: no production code changes.
"""

from __future__ import annotations

import inspect
import unittest
import trainer.trainer as trainer_mod


# ---------------------------------------------------------------------------
# R210 Review #1 — Bug: 重跑 chunk 1 回傳 None 時須保留探針 path，不遺失 chunk 0
# ---------------------------------------------------------------------------

class TestR210OomProbeChunk0NotLostWhenRerunReturnsNone(unittest.TestCase):
    """R210 Review #1: When probe rerun (frac<1.0) returns None, chunk_paths must still include chunk 0 (probe path)."""

    def test_step6_oom_probe_rerun_none_appends_probe_path(self):
        """In run_pipeline Step 6 OOM probe branch, when path1_rerun is None we must append path1 (probe result).

        Require an inner 'else:' (same indentation as 'if path1_rerun is not None') that appends path1.
        """
        src = inspect.getsource(trainer_mod.run_pipeline)
        idx_if_rerun = src.find("if path1_rerun is not None:")
        self.assertGreater(idx_if_rerun, -1, "if path1_rerun is not None block not found")
        # Indentation of "if path1_rerun" line (inner block)
        line_start = src.rfind("\n", 0, idx_if_rerun) + 1
        inner_indent = len(src[line_start : line_start + 50]) - len(
            src[line_start : line_start + 50].lstrip()
        )
        idx_append_rerun = src.find("chunk_paths.append(path1_rerun)", idx_if_rerun)
        self.assertGreater(idx_append_rerun, -1, "chunk_paths.append(path1_rerun) not found")
        # Between append(path1_rerun) and the next lesser-indented "else:", we need inner "else:" + path1
        segment = src[idx_append_rerun : idx_append_rerun + 600]
        lines = segment.split("\n")
        has_inner_else_path1 = False
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("else:") and len(line) - len(stripped) == inner_indent:
                # This is an else at same level as "if path1_rerun"; next lines should append path1
                rest = "\n".join(lines[i : i + 3])
                if "path1" in rest and "append" in rest:
                    has_inner_else_path1 = True
                    break
        self.assertTrue(
            has_inner_else_path1,
            "When path1_rerun is None production must append path1 (inner else) so chunk 0 is not lost (R210 Review #1).",
        )


# ---------------------------------------------------------------------------
# R210 Review #2 — 健壯性: psutil.virtual_memory 拋 OSError 時應回傳 current_frac
# ---------------------------------------------------------------------------

class TestR210OomCheckAfterChunk1HandlesPsutilOserror(unittest.TestCase):
    """R210 Review #2: _oom_check_after_chunk1 must return current_frac when psutil.virtual_memory raises OSError."""

    def test_oom_check_after_chunk1_returns_current_frac_when_virtual_memory_raises(self):
        """When virtual_memory() raises OSError, _oom_check_after_chunk1 should return current_frac and not raise.

        psutil is imported inside the function, so we patch sys.modules['psutil'] with a fake module.
        """
        import sys
        import types

        from trainer.trainer import _oom_check_after_chunk1

        def _vm_raise():
            raise OSError("mock: virtual_memory failed")

        fake_psutil = types.ModuleType("psutil")
        fake_psutil.virtual_memory = _vm_raise
        saved_psutil = sys.modules.get("psutil")
        try:
            sys.modules["psutil"] = fake_psutil
            result = _oom_check_after_chunk1(
                per_chunk_bytes=2**30,
                n_chunks=4,
                current_frac=1.0,
            )
            self.assertEqual(result, 1.0, "Should return current_frac when psutil.virtual_memory raises")
        finally:
            if saved_psutil is not None:
                sys.modules["psutil"] = saved_psutil
            elif "psutil" in sys.modules and sys.modules["psutil"] is fake_psutil:
                del sys.modules["psutil"]


# ---------------------------------------------------------------------------
# R210 Review #3 — 邊界: per_chunk_bytes 或 n_chunks 為 0 時回傳 current_frac
# ---------------------------------------------------------------------------

class TestR210OomCheckAfterChunk1ZeroSizeReturnsCurrentFrac(unittest.TestCase):
    """R210 Review #3: When per_chunk_bytes or n_chunks is 0, _oom_check_after_chunk1 returns current_frac (no div-by-zero)."""

    def test_oom_check_after_chunk1_zero_per_chunk_bytes_returns_current_frac(self):
        """per_chunk_bytes=0 => estimated_peak_ram=0 => return current_frac without division."""
        from trainer.trainer import _oom_check_after_chunk1

        result = _oom_check_after_chunk1(per_chunk_bytes=0, n_chunks=4, current_frac=1.0)
        self.assertEqual(result, 1.0, "Zero per_chunk_bytes should return current_frac")

    def test_oom_check_after_chunk1_zero_n_chunks_returns_current_frac(self):
        """n_chunks=0 => estimated_peak_ram=0 => return current_frac without division."""
        from trainer.trainer import _oom_check_after_chunk1

        result = _oom_check_after_chunk1(per_chunk_bytes=100, n_chunks=0, current_frac=1.0)
        self.assertEqual(result, 1.0, "Zero n_chunks should return current_frac")


if __name__ == "__main__":
    unittest.main()
