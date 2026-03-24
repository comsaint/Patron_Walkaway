"""Task 7 DoD chunk_cache_stats + R5 hardening review -> executable MRE guards.

Encode STATUS.md § Code Review — Task 7 DoD 計數 + R5 加固 findings.
Scope: tests only; no production code edits.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import trainer.trainer as trainer_mod


class TestTask7DodChunkCacheStatsReviewRisksMRE(unittest.TestCase):
    # --- 1) stats = per process_chunk invocation (not unique chunks) ---

    def test_risk1_run_pipeline_passes_same_chunk_cache_stats_to_step7_rerun(self) -> None:
        """MRE: one dict is shared; Step 7 _run_step6 also passes chunk_cache_stats (cumulative risk)."""
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn("chunk_cache_stats: Dict[str, int] = {}", src)
        self.assertRegex(
            src,
            r"chunk_cache_stats=chunk_cache_stats",
            "process_chunk and _run_step6 should share the same stats dict.",
        )
        # _run_step6 nested in Step 7 — same outer chunk_cache_stats
        self.assertIn("def _run_step6", src)

    def test_risk1_neg_sample_auto_path_calls_process_chunk_multiple_times_documented(self) -> None:
        """MRE: NEG_SAMPLE_FRAC_AUTO branch can invoke process_chunk twice for chunk 0."""
        src = inspect.getsource(trainer_mod.run_pipeline)
        if "NEG_SAMPLE_FRAC_AUTO" not in src:
            self.fail("expected NEG_SAMPLE_FRAC_AUTO in run_pipeline")
        self.assertIn("path1 = process_chunk", src)
        self.assertIn("path1_rerun = process_chunk", src)

    # --- 2) final_hit_total overlaps with sub-counters (not disjoint) ---

    def test_risk2_local_metadata_hit_increments_both_final_and_subcounter(self) -> None:
        """MRE: local metadata cache hit bumps final_hit_total AND local_metadata_total."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn("cache hit (key=%s, local metadata)", src)
        idx = src.find("local metadata")
        self.assertGreater(idx, 0)
        block = src[idx : idx + 900]
        self.assertIn("step6_chunk_cache_final_hit_total", block)
        self.assertIn("step6_chunk_cache_final_hit_local_metadata_total", block)

    def test_risk2_ch_hit_increments_both_final_and_after_load(self) -> None:
        """MRE: CH path final hit bumps final_hit_total AND after_load_total."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn("step6_chunk_cache_final_hit_after_load_total", src)

    # --- 3) read_metadata failure: >=2 WARNING per _local_parquet_source_data_hash call ---

    def test_risk3_two_files_two_warnings_on_both_metadata_fail(self) -> None:
        """MRE (debt): each failing file logs WARNING; one hash call touches two files."""
        ws = pd.Timestamp("2026-01-01 00:00:00")
        ee = pd.Timestamp("2026-02-01 00:00:00")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pq.write_table(pa.table({"k": [1]}), root / "gmwds_t_bet.parquet")
            pq.write_table(pa.table({"k": [1]}), root / "gmwds_t_session.parquet")
            old = trainer_mod.LOCAL_PARQUET_DIR
            trainer_mod.LOCAL_PARQUET_DIR = root
            try:
                with patch.object(pq, "read_metadata", side_effect=OSError("bad")):
                    with self.assertLogs("trainer", level="WARNING") as cm:
                        trainer_mod._local_parquet_source_data_hash(
                            ws.to_pydatetime(), ee.to_pydatetime()
                        )
            finally:
                trainer_mod.LOCAL_PARQUET_DIR = old
        self.assertGreaterEqual(len(cm.output), 2, cm.output)

    # --- 4) chunk_cache_stats merge can overwrite payload keys (no validation today) ---

    def test_risk4_chunk_cache_stats_can_overwrite_model_version_in_json(self) -> None:
        """MRE (debt): merge is blind — malicious/buggy key collides with core fields."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod._write_pipeline_diagnostics_json(
                    model_version="REAL_MV",
                    pipeline_started_at="2026-03-24T00:00:00+00:00",
                    pipeline_finished_at="2026-03-24T01:00:00+00:00",
                    total_duration_sec=1.0,
                    chunk_cache_stats={"model_version": "HIJACK"},
                )
            data = json.loads((model_dir / "pipeline_diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(data["model_version"], "HIJACK")

    def test_risk4_write_merge_does_not_typecheck_chunk_cache_values(self) -> None:
        """MRE: non-int values from chunk_cache_stats are still written (JSON encodes)."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod._write_pipeline_diagnostics_json(
                    model_version="mv",
                    pipeline_started_at="a",
                    pipeline_finished_at="b",
                    total_duration_sec=1.0,
                    chunk_cache_stats={"step6_chunk_cache_final_hit_total": "not_int"},
                )
            data = json.loads((model_dir / "pipeline_diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(data["step6_chunk_cache_final_hit_total"], "not_int")

    # --- 5) data_hash non-str passes str() and may not be 8-hex ---

    def test_risk5_bytes_data_hash_becomes_non_hex_string_today(self) -> None:
        """MRE: bytes data_hash is accepted and stringified (weak hex contract)."""
        chunk = {
            "window_start": pd.Timestamp("2026-01-01 00:00:00"),
            "window_end": pd.Timestamp("2026-02-01 00:00:00"),
        }
        got = trainer_mod._chunk_cache_components(
            chunk,
            None,
            profile_hash="none",
            feature_spec_hash="x",
            neg_sample_frac=1.0,
            data_hash=b"beef",
        )
        self.assertNotRegex(got["data_hash"], r"^[0-9a-f]{8}$")
        self.assertIn("beef", got["data_hash"])

    # --- 6) WARNING message includes path repr (privacy / log volume) ---

    def test_risk6_read_metadata_warning_includes_path_in_message_format(self) -> None:
        """MRE: warning format uses %s for path — typically absolute path on disk."""
        src = inspect.getsource(trainer_mod._local_parquet_source_data_hash)
        self.assertRegex(
            src,
            r"read_metadata failed for %s",
            "Expected warning to interpolate full path object.",
        )


if __name__ == "__main__":
    unittest.main()
