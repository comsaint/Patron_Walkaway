"""Minimal reproducible guards for reviewer risks (Round 371).

Scope:
- Convert R-371-1..R-371-7 from STATUS.md into executable checks.
- Tests/lint-like source guards only; no production code edits.
- All risks resolved; expectedFailure decorators removed.
"""

from __future__ import annotations

import inspect
import pathlib
import re
import unittest

import trainer.config as config_mod
import trainer.etl_player_profile as etl_mod
import trainer.scorer as scorer_mod
import trainer.trainer as trainer_mod


class TestR371RiskGuards(unittest.TestCase):
    """Risk guards derived from STATUS.md self-review items (all resolved)."""

    def test_r371_1_scorer_should_not_query_clickhouse_profile(self):
        """R-371-1 resolved: scorer profile loader is local-only (no CH fallback)."""
        src = inspect.getsource(scorer_mod._load_profile_for_scoring)
        self.assertNotIn(
            "from ClickHouse",
            src,
            "scorer should avoid ClickHouse profile fallback; use local parquet only",
        )
        self.assertNotRegex(
            src,
            r"get_clickhouse_client|SOURCE_DB|TPROFILE|query_df\(",
            "scorer profile loader should not contain ClickHouse query logic",
        )

    def test_r371_2_etl_should_not_attempt_clickhouse_insert(self):
        """R-371-2 resolved: profile ETL persists locally without CH insert branch."""
        src = inspect.getsource(etl_mod.backfill_one_snapshot_date)
        self.assertNotIn(
            "_write_to_clickhouse(",
            src,
            "ETL snapshot backfill should not call _write_to_clickhouse",
        )

    def test_r371_3_oom_check_should_handle_cache_key_mismatch(self):
        """R-371-3 resolved: OOM pre-check filters chunks by .cache_key sidecar."""
        src = inspect.getsource(trainer_mod._oom_check_and_adjust_neg_sample_frac)
        self.assertRegex(
            src,
            r"cache_key|\.cache_key|stored_key|current_key",
            "OOM-check should account for chunk cache-key staleness before using cached sizes",
        )

    def test_r371_4_step7_should_avoid_split_copy_spike(self):
        """R-371-4 resolved: Step 7 split uses reset_index instead of .copy()."""
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertNotRegex(
            src,
            r"train_df\s*=\s*full_df\[.*\]\.copy\(\)",
            "train_df copy from full_df amplifies peak memory",
        )
        self.assertNotRegex(
            src,
            r"valid_df\s*=\s*full_df\[.*\]\.copy\(\)",
            "valid_df copy from full_df amplifies peak memory",
        )
        self.assertNotRegex(
            src,
            r"test_df\s*=\s*full_df\[.*\]\.copy\(\)",
            "test_df copy from full_df amplifies peak memory",
        )

    def test_r371_5_neg_sampling_seed_should_be_process_stable(self):
        """R-371-5 resolved: chunk seed uses hashlib (stable across processes)."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertNotRegex(
            src,
            r"_chunk_seed\s*=\s*hash\(",
            "Prefer stable hashlib-based seed over built-in hash()",
        )

    def test_r371_6_config_comment_should_match_factor(self):
        """R-371-6 resolved: config comment reflects actual expansion factor."""
        cfg_src = pathlib.Path(config_mod.__file__).read_text(encoding="utf-8")
        if getattr(config_mod, "CHUNK_CONCAT_RAM_FACTOR", 3) >= 10:
            self.assertNotRegex(
                cfg_src,
                r"2[–-]3x|~2\s*[–-]\s*3x",
                "Comment still says ~2-3x while configured factor is >=10",
            )

    def test_r371_7_oom_check_should_include_split_overhead(self):
        """R-371-7 resolved: OOM estimate includes TRAIN_SPLIT_FRAC overhead."""
        src = inspect.getsource(trainer_mod._oom_check_and_adjust_neg_sample_frac)
        self.assertRegex(
            src,
            r"TRAIN_SPLIT_FRAC|split_overhead|copy_overhead",
            "OOM-check should model split/copy overhead in peak RAM estimate",
        )


if __name__ == "__main__":
    unittest.main()
