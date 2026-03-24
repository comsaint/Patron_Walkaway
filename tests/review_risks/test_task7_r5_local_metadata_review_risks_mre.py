"""Task 7 R5 local metadata hash review risks -> executable MRE guards.

Encode STATUS.md § Code Review — Task 7 R5 findings as tests on **current** behavior.
Scope: tests only; no production code edits.
"""

from __future__ import annotations

import inspect
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import trainer.trainer as trainer_mod


class TestTask7R5LocalMetadataReviewRisksMRE(unittest.TestCase):
    # --- 1) isoformat vs _filter_ts single source of truth ---

    def test_risk1_r5_uses_isoformat_bounds_not_filter_ts(self) -> None:
        """MRE: R5 payload encodes filter bounds via datetime.isoformat, not _filter_ts."""
        src_r5 = inspect.getsource(trainer_mod._local_parquet_source_data_hash)
        self.assertIn("isoformat()", src_r5)
        self.assertNotIn("_filter_ts", src_r5)

    def test_risk1_loader_uses_filter_ts_for_parquet_pushdown(self) -> None:
        """MRE: load_local_parquet pushdown still depends on nested _filter_ts."""
        src_ld = inspect.getsource(trainer_mod.load_local_parquet)
        self.assertIn("def _filter_ts", src_ld)
        self.assertIn('_filter_ts(bets_lo, bets_path, "payout_complete_dtm")', src_ld)
        self.assertIn(
            '_filter_ts(extended_end + timedelta(days=1), sess_path, "session_start_dtm")',
            src_ld,
        )

    def test_risk1_no_shared_exported_bound_helper_yet(self) -> None:
        """MRE (debt): public API has no shared _parquet_filter_bound_repr-style helper."""
        impl = Path(trainer_mod.__file__).resolve()
        text = impl.read_text(encoding="utf-8")
        self.assertNotRegex(
            text,
            r"def\s+_parquet_filter_bound_repr\b",
            "If this fails after refactor, drop or rename this guard per STATUS review closure.",
        )

    # --- 2) empty data_hash ---

    def test_risk2_empty_string_data_hash_raises_value_error(self) -> None:
        """MRE (post-hardening): empty data_hash is rejected."""
        chunk = {
            "window_start": pd.Timestamp("2026-01-01 00:00:00"),
            "window_end": pd.Timestamp("2026-02-01 00:00:00"),
        }
        with self.assertRaises(ValueError):
            trainer_mod._chunk_cache_components(
                chunk,
                None,
                profile_hash="none",
                feature_spec_hash="x",
                neg_sample_frac=1.0,
                data_hash="",
            )

    # --- 3) read_metadata failure silent -1 ---

    def test_risk3_read_metadata_failure_yields_nrows_minus_one_token(self) -> None:
        """MRE: corrupt metadata collapses distinct failures to the same nrows=-1 slot."""
        ws = pd.Timestamp("2026-01-01 00:00:00")
        ee = pd.Timestamp("2026-02-01 00:00:00")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pq.write_table(pa.table({"k": [1]}), root / "gmwds_t_bet.parquet")
            pq.write_table(pa.table({"k": [1]}), root / "gmwds_t_session.parquet")
            old = trainer_mod.LOCAL_PARQUET_DIR
            trainer_mod.LOCAL_PARQUET_DIR = root
            try:
                with patch.object(pq, "read_metadata", side_effect=OSError("boom")):
                    h = trainer_mod._local_parquet_source_data_hash(
                        ws.to_pydatetime(), ee.to_pydatetime()
                    )
            finally:
                trainer_mod.LOCAL_PARQUET_DIR = old
        self.assertEqual(len(h), 8)
        # Both files hit except -> two |-1| fragments in payload before md5
        src = inspect.getsource(trainer_mod._local_parquet_source_data_hash)
        self.assertIn("nrows = -1", src)

    def test_risk3_read_metadata_failure_logs_warning(self) -> None:
        """MRE (post-hardening): read_metadata failure emits WARNING on trainer logger."""
        ws = pd.Timestamp("2026-01-01 00:00:00")
        ee = pd.Timestamp("2026-02-01 00:00:00")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pq.write_table(pa.table({"k": [1]}), root / "gmwds_t_bet.parquet")
            pq.write_table(pa.table({"k": [1]}), root / "gmwds_t_session.parquet")
            old = trainer_mod.LOCAL_PARQUET_DIR
            trainer_mod.LOCAL_PARQUET_DIR = root
            try:
                with patch.object(pq, "read_metadata", side_effect=RuntimeError("x")):
                    with self.assertLogs("trainer", level="WARNING") as cm:
                        trainer_mod._local_parquet_source_data_hash(
                            ws.to_pydatetime(), ee.to_pydatetime()
                        )
            finally:
                trainer_mod.LOCAL_PARQUET_DIR = old
        self.assertTrue(
            any("read_metadata failed" in r for r in cm.output),
            cm.output,
        )

    # --- 4) duplicate metadata/schema reads (static debt counter) ---

    def test_risk4_local_hash_calls_read_metadata_per_file(self) -> None:
        """MRE: R5 touches pq.read_metadata once per artifact path in source."""
        src = inspect.getsource(trainer_mod._local_parquet_source_data_hash)
        self.assertEqual(src.count("pq.read_metadata(p)"), 1)  # single call site in _file_token

    def test_risk4_load_local_parquet_multiple_schema_reads(self) -> None:
        """MRE: loader re-reads schema/metadata paths; pairs with R5 for extra I/O on miss."""
        src = inspect.getsource(trainer_mod.load_local_parquet)
        self.assertGreaterEqual(
            src.count("read_schema"),
            2,
            "load_local_parquet should read_schema at least for bet + session filter paths.",
        )

    # --- 5) schema not part of R5 file token ---

    def test_risk5_r5_has_no_read_schema_in_source(self) -> None:
        """MRE: R5 hash does not incorporate Parquet schema fingerprint today."""
        src = inspect.getsource(trainer_mod._local_parquet_source_data_hash)
        self.assertNotIn("read_schema", src)

    def test_risk5_two_schemas_same_counts_can_collide_if_stat_identical_debt_note(self) -> None:
        """Document collision class: different logical schema but identical file token inputs.

        When production fixes risk 5, replace this with a stricter inequality assertion.
        """
        with tempfile.TemporaryDirectory() as td_a, tempfile.TemporaryDirectory() as td_b:
            root_a = Path(td_a)
            root_b = Path(td_b)
            # Same single row int column; second file adds nullable column with default (shape 1 row).
            pq.write_table(pa.table({"x": [1]}), root_a / "gmwds_t_bet.parquet")
            pq.write_table(pa.table({"x": [1], "y": pa.array([None], type=pa.int64())}), root_b / "gmwds_t_bet.parquet")
            pq.write_table(pa.table({"s": [1]}), root_a / "gmwds_t_session.parquet")
            pq.write_table(pa.table({"s": [1]}), root_b / "gmwds_t_session.parquet")

            ws = pd.Timestamp("2026-01-01 00:00:00")
            ee = pd.Timestamp("2026-02-01 00:00:00")

            def _h(root: Path) -> str:
                old = trainer_mod.LOCAL_PARQUET_DIR
                trainer_mod.LOCAL_PARQUET_DIR = root
                try:
                    return trainer_mod._local_parquet_source_data_hash(
                        ws.to_pydatetime(), ee.to_pydatetime()
                    )
                finally:
                    trainer_mod.LOCAL_PARQUET_DIR = old

            ha, hb = _h(root_a), _h(root_b)
        # Usually file sizes differ so hashes differ; if equal, MRE flags collision class.
        if ha == hb:
            self.fail(
                "R5 hashes equal for different bet schemas — schema should enter hash "
                f"(debt confirmed: ha={ha!r} hb={hb!r}).",
            )
        # When this test merely passes because sizes differ, risk 5 (schema omission) remains; guarded by read_schema MRE above.

    # --- 6) cache hit skips probe (local metadata path) ---

    def test_risk6_local_metadata_hit_branch_skips_corruption_probe(self) -> None:
        """MRE: 'local metadata' cache hit returns without parquet integrity probe."""
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn("local metadata", src)
        m = re.search(
            r"cache hit \(key=%s, local metadata\).*?return chunk_path",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "local metadata cache-hit branch not found")
        assert m is not None
        fragment = m.group(0)
        self.assertNotRegex(
            fragment,
            r"read_parquet|ParquetFile|pq\.ParquetFile",
            "Expected local metadata hit to skip parquet read probe.",
        )

    # --- 7) CH vs local data_hash semantics (miss_reason) ---

    def test_risk7_diff_data_hash_only_triggers_data_miss_reason(self) -> None:
        """MRE: switching row-hash vs file-meta strategy surfaces as pipeline data miss."""
        base = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "aaaaaaaa",
            "cfg_hash": "111111",
            "profile_hash": "none",
            "feature_spec_hash": "spec1",
            "neg_sample_frac": 1.0,
        }
        prev = dict(base)
        cur = {**base, "data_hash": "bbbbbbbb"}
        reasons = trainer_mod._chunk_cache_miss_reasons("stale_fp", prev, cur)
        self.assertIn("data", reasons)

    def test_risk7_sidecar_payload_has_no_data_hash_kind_field_today(self) -> None:
        """MRE: JSON sidecar schema has no explicit data_hash_kind (debug clarity debt)."""
        sample = json.loads(
            trainer_mod._write_chunk_cache_sidecar(
                "fp",
                {
                    "window_start": "2026-01-01T00:00:00",
                    "window_end": "2026-02-01T00:00:00",
                    "data_hash": "12345678",
                    "cfg_hash": "000000",
                    "profile_hash": "none",
                    "feature_spec_hash": "x",
                    "neg_sample_frac": 1.0,
                },
                source_mode="local_parquet",
            )
        )
        self.assertNotIn("data_hash_kind", json.dumps(sample))


if __name__ == "__main__":
    unittest.main()
