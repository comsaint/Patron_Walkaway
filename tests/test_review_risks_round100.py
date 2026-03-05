"""tests/test_review_risks_round100.py
=====================================
Minimal reproducible guardrail tests for Review Round 19 findings (R105–R112).

Tests-only: no production code changes.
"""

from __future__ import annotations

import ast
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ETL_PATH = _REPO_ROOT / "trainer" / "etl_player_profile.py"
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"

_ETL_SRC = _ETL_PATH.read_text(encoding="utf-8")
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")

_ETL_TREE = ast.parse(_ETL_SRC)
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_func_src(tree: ast.AST, src: str, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


class TestR105AutoScriptGateBlocksFastMode(unittest.TestCase):
    """R105: auto_script.exists() should not block fast-mode in-process backfill."""

    def test_auto_script_check_inside_subprocess_branch(self):
        """The auto_script.exists() check must be inside the else (subprocess) branch,
        not as a global early return. Otherwise fast-mode in-process backfill is
        blocked when the script is missing."""
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "ensure_player_profile_ready")
        self.assertGreater(len(src), 0, "ensure_player_profile_ready not found")
        # R105: "if not auto_script.exists(): return" must appear AFTER "else:" of
        # "if use_inprocess" — i.e. the script check must be inside the subprocess
        # branch. Check: the line with auto_script.exists() should be at greater
        # indent than the "for miss_start" loop (nested in else), or the script
        # check should not cause unconditional return before the loop.
        # Guardrail: "auto_script" and "return" in same block implies that block
        # must be the else of use_inprocess. We verify by: the char offset of
        # "if not auto_script.exists()" must be after "else:" of use_inprocess.
        idx_auto = src.find("auto_script.exists()")
        idx_use_inprocess = src.find("use_inprocess")
        idx_for_loop = src.find("for miss_start")
        self.assertGreater(idx_auto, 0, "auto_script.exists() not found")
        self.assertGreater(idx_use_inprocess, 0, "use_inprocess not found")
        self.assertGreater(idx_for_loop, 0, "for miss_start loop not found")
        # The fix: auto_script check should be inside the else block of the for
        # loop body (the else that pairs with "if use_inprocess").
        idx_else = src.find("else:", src.find("if use_inprocess", idx_for_loop))
        self.assertGreater(idx_else, 0, "else branch (for subprocess) not found")
        # R105: auto_script check must be inside the else (subprocess) block,
        # i.e. after the "else:" that corresponds to use_inprocess
        self.assertGreater(
            idx_auto,
            idx_else,
            "R105: auto_script.exists() check must be inside else (subprocess) branch, "
            "not before the loop — it currently blocks fast-mode when script is missing",
        )


class TestR106SchemaHashIncludesPopulationMode(unittest.TestCase):
    """R106: schema hash must include population indicator to prevent fast/normal cache mix."""

    def test_ensure_profile_hash_includes_whitelist_indicator(self):
        """The hash used for cache invalidation must differ when canonical_id_whitelist
        changes (fast vs normal mode). The schema-hash block must modify current_hash
        using whitelist/_pop_tag, not just have whitelist as a parameter."""
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "ensure_player_profile_ready")
        self.assertGreater(len(src), 0, "ensure_player_profile_ready not found")
        # R106: the block that sets current_hash for cache comparison must incorporate
        # canonical_id_whitelist (e.g. _pop_tag = f"_whitelist={len(...)}" and use in hash)
        idx = src.find("current_hash = compute_profile_schema_hash")
        hash_block = src[idx : idx + 600] if idx >= 0 else ""
        self.assertRegex(
            hash_block,
            r"_pop_tag|current_hash\s*=\s*[^;]*(?:whitelist|_pop_tag)|hashlib.*whitelist",
            "R106: schema hash for cache invalidation must incorporate population mode "
            "(canonical_id_whitelist) to prevent fast/normal cache poisoning",
        )


class TestR107FilterPreloadedNoRedundantCopy(unittest.TestCase):
    """R107: _filter_preloaded_sessions should not have redundant .copy() after .drop()."""

    def test_filter_preloaded_sessions_no_redundant_copy(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "_filter_preloaded_sessions")
        self.assertGreater(len(src), 0, "_filter_preloaded_sessions not found")
        # R107: .drop(columns=[...]) already returns new DataFrame; .copy() is redundant
        self.assertNotRegex(
            src,
            r"\.drop\s*\(\s*columns\s*=\s*\[.*\]\s*[^)]*\)\s*\.\s*copy\s*\(\s*\)",
            "R107: remove redundant .copy() after .drop() in _filter_preloaded_sessions",
        )


class TestR108BackfillLogsSkippedCount(unittest.TestCase):
    """R108: backfill must log explicit skipped count when snapshot_interval_days > 1."""

    def test_backfill_has_separate_skipped_counter(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "backfill")
        self.assertGreater(len(src), 0, "backfill not found")
        # R108: must have a separate 'skipped' variable/counter, not just "failed/skipped" text
        self.assertRegex(
            src,
            r"skipped\s*\+=\s*1|skipped\s*=\s*0|,\s*skipped\s*\)",
            "R108: backfill must maintain and log explicit skipped counter",
        )


class TestR109FastModeUsesWhitelistForProfileLoad(unittest.TestCase):
    """R109 (updated for DEC-017/R205): In fast-mode WITHOUT --sample-rated,
    load_player_profile must receive ALL canonical_ids from canonical_map
    (no implicit sampling).  Implicit sampling was removed in R205; the old
    DEC-015 FAST_MODE_RATED_SAMPLE_N auto-sampling no longer applies."""

    def test_run_pipeline_passes_whitelist_to_load_profile_in_fast_mode(self):
        """When fast_mode=True (no --sample-rated), load_player_profile
        canonical_ids must equal ALL canonical_ids in canonical_map (DEC-017/R205).
        Previously (DEC-015) this was capped at FAST_MODE_RATED_SAMPLE_N; that
        implicit behaviour was intentionally removed."""
        import argparse
        from trainer.trainer import run_pipeline, FAST_MODE_RATED_SAMPLE_N  # noqa: F401 (kept for reference)

        with patch("trainer.trainer.get_monthly_chunks") as mock_chunks, \
             patch("trainer.trainer.get_train_valid_test_split") as mock_split, \
             patch("trainer.trainer.load_local_parquet") as mock_load, \
             patch("trainer.trainer.apply_dq") as mock_dq, \
             patch("trainer.trainer.build_canonical_mapping_from_df") as mock_build, \
             patch("trainer.trainer.ensure_player_profile_ready"), \
             patch("trainer.trainer.load_player_profile") as mock_load_profile, \
             patch("trainer.trainer.process_chunk") as mock_process, \
             patch("trainer.trainer.train_single_rated_model") as mock_train, \
             patch("trainer.trainer.save_artifact_bundle"):
            from zoneinfo import ZoneInfo
            HK = ZoneInfo("Asia/Hong_Kong")
            base = datetime(2025, 1, 1, tzinfo=HK)
            mock_chunks.return_value = [
                {"window_start": base, "window_end": base + timedelta(days=30),
                 "extended_end": base + timedelta(days=31)},
            ] * 4
            mock_split.return_value = {
                "train_chunks": mock_chunks.return_value[:2],
                "valid_chunks": mock_chunks.return_value[2:3],
                "test_chunks": mock_chunks.return_value[3:],
            }
            mock_load.return_value = (pd.DataFrame(), pd.DataFrame())
            mock_dq.return_value = (pd.DataFrame(), pd.DataFrame())
            # canonical_map with 5000 unique IDs
            cids = [f"cid_{i}" for i in range(5000)]
            mock_build.return_value = pd.DataFrame({
                "player_id": list(range(5000)),
                "canonical_id": cids,
            })
            mock_load_profile.return_value = pd.DataFrame()
            with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tf:
                pd.DataFrame({
                    "payout_complete_dtm": [datetime(2025, 5, 15)],
                    "label": [1], "is_rated": [True],
                }).to_parquet(tf.name, index=False)
                _tmp_chunk = tf.name
            mock_process.return_value = _tmp_chunk
            mock_train.return_value = ({"model": None, "threshold": 0.5, "features": []}, None, {})

            with patch("trainer.trainer.pd.read_parquet") as mock_read:
                mock_read.return_value = pd.DataFrame({
                    "payout_complete_dtm": [datetime(2025, 5, 15, tzinfo=HK)],
                    "label": [1], "is_rated": [True],
                })

            try:
                args = argparse.Namespace(
                    start="2025-01-01", end="2025-06-01", days=None,
                    use_local_parquet=True, force_recompute=False, skip_optuna=False,
                    recent_chunks=None, fast_mode=True,
                    # sample_rated intentionally omitted (no --sample-rated flag)
                )
                run_pipeline(args)

                self.assertTrue(mock_load_profile.called)
                kwargs = mock_load_profile.call_args[1]
                cids_arg = kwargs.get("canonical_ids")
                # DEC-017/R205: without --sample-rated, fast-mode passes ALL canonical_ids.
                # Implicit sampling (DEC-015) was removed; rated_whitelist stays None.
                self.assertIsNotNone(cids_arg, "canonical_ids should not be None when canonical_map is non-empty")
                self.assertEqual(
                    len(cids_arg),
                    5000,
                    "R109 (DEC-017): fast-mode WITHOUT --sample-rated must pass ALL "
                    "canonical_ids (5000) to load_player_profile, not a sampled subset.",
                )
            finally:
                pathlib.Path(_tmp_chunk).unlink(missing_ok=True)


class TestR111FastModeCoverageCheckNoFalseWarning(unittest.TestCase):
    """R111: coverage check must not log WARNING when snapshot_interval_days > 1 (expected gaps)."""

    def test_ensure_profile_coverage_check_respects_interval(self):
        """When snapshot_interval_days > 1, the final coverage check should not
        emit WARNING for expected date gaps."""
        src = _get_func_src(_TRAINER_TREE, _TRAINER_SRC, "ensure_player_profile_ready")
        self.assertGreater(len(src), 0, "ensure_player_profile_ready not found")
        # R111: coverage check block must have a branch for snapshot_interval_days > 1
        self.assertRegex(
            src,
            r"snapshot_interval_days\s*>\s*1|interval.*coverage|coverage.*interval",
            "R111: coverage check must handle snapshot_interval_days > 1 to avoid "
            "false-positive WARNING for expected date gaps",
        )


class TestR112PreloadTriggeredByWhitelist(unittest.TestCase):
    """R112: preload should trigger when whitelist is set even if interval=1."""

    def test_backfill_preload_condition_includes_whitelist(self):
        src = _get_func_src(_ETL_TREE, _ETL_SRC, "backfill")
        self.assertGreater(len(src), 0, "backfill not found")
        # R112: the block that calls _preload_sessions_local must have condition
        # including canonical_id_whitelist (so whitelist-only fast-mode triggers preload)
        idx_preload = src.find("_preload_sessions_local()")
        self.assertGreater(idx_preload, 0)
        # The condition for that block should be in the preceding ~200 chars
        block_before = src[max(0, idx_preload - 250) : idx_preload]
        self.assertRegex(
            block_before,
            r"canonical_id_whitelist\s+is\s+not\s+None|whitelist\s+is\s+not\s+None",
            "R112: preload condition must include canonical_id_whitelist so whitelist-only "
            "fast-mode triggers in-memory session load",
        )


if __name__ == "__main__":
    unittest.main()
