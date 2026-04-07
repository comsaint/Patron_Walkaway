from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import trainer.trainer as trainer_mod
from trainer.core import config as core_config


class TestTask7ChunkCacheKey(unittest.TestCase):
    def test_order_insensitive_bets_hash_same_rows_different_order(self) -> None:
        bets_a = pd.DataFrame(
            {
                "bet_id": [101, 102, 103, 104],
                "player_id": ["p1", "p2", "p1", "p3"],
                "amount": [10.0, 20.0, 15.0, 30.0],
            }
        )
        bets_b = bets_a.iloc[[2, 0, 3, 1]].reset_index(drop=True)

        hash_a = trainer_mod._order_insensitive_bets_hash(bets_a)
        hash_b = trainer_mod._order_insensitive_bets_hash(bets_b)

        self.assertEqual(hash_a, hash_b)

    def test_chunk_cache_key_changes_when_data_changes(self) -> None:
        chunk = {
            "window_start": pd.Timestamp("2026-01-01 00:00:00"),
            "window_end": pd.Timestamp("2026-02-01 00:00:00"),
        }
        bets = pd.DataFrame({"bet_id": [1, 2], "amount": [10.0, 20.0]})
        bets_changed = pd.DataFrame({"bet_id": [1, 2], "amount": [10.0, 99.0]})

        key_a = trainer_mod._chunk_cache_key(chunk, bets)
        key_b = trainer_mod._chunk_cache_key(chunk, bets_changed)

        self.assertNotEqual(key_a, key_b)

    def test_sidecar_json_roundtrip_preserves_fingerprint(self) -> None:
        comp = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "abcdef01",
            "cfg_hash": "cafe42",
            "profile_hash": "none",
            "feature_spec_hash": "feed",
            "neg_sample_frac": 0.5,
        }
        fp = trainer_mod._fingerprint_from_chunk_cache_components(comp)
        raw = trainer_mod._write_chunk_cache_sidecar(fp, comp, source_mode="clickhouse")
        obj = json.loads(raw)
        self.assertEqual(obj["v"], 1)
        self.assertEqual(obj["fingerprint"], fp)
        self.assertEqual(obj["source"]["mode"], "clickhouse")
        fp2, comp2 = trainer_mod._read_chunk_cache_sidecar(raw)
        self.assertEqual(fp2, fp)
        self.assertIsNotNone(comp2)
        assert comp2 is not None
        self.assertEqual(comp2["data_hash"], comp["data_hash"])
        self.assertEqual(float(comp2["neg_sample_frac"]), 0.5)

    def test_read_legacy_plain_pipe_sidecar(self) -> None:
        comp = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "abc12345",
            "cfg_hash": "111111",
            "profile_hash": "none",
            "feature_spec_hash": "x",
            "neg_sample_frac": 1.0,
        }
        line = trainer_mod._fingerprint_from_chunk_cache_components(comp)
        fp, comp2 = trainer_mod._read_chunk_cache_sidecar(line + "\n")
        self.assertEqual(fp, line)
        self.assertIsNotNone(comp2)
        assert comp2 is not None
        self.assertEqual(comp2["feature_spec_hash"], "x")

    def test_miss_reason_reports_spec_when_spec_differs(self) -> None:
        chunk = {
            "window_start": pd.Timestamp("2026-01-01 00:00:00"),
            "window_end": pd.Timestamp("2026-02-01 00:00:00"),
        }
        bets = pd.DataFrame({"bet_id": [1], "amount": [1.0]})
        cur = trainer_mod._chunk_cache_components(
            chunk, bets,
            profile_hash="none",
            feature_spec_hash="newspec",
            neg_sample_frac=1.0,
        )
        prev = dict(cur)
        prev["feature_spec_hash"] = "oldspec"
        bad_fp = "not-a-match"
        reasons = trainer_mod._chunk_cache_miss_reasons(bad_fp, prev, cur)
        self.assertIn("spec", reasons)

    def test_profile_hash_chunk_scoped_excludes_future_snapshots(self) -> None:
        """R4: rows with snapshot_dtm > window_end must not bust older chunk keys."""
        we = pd.Timestamp("2026-02-01 00:00:00")
        base = pd.DataFrame(
            {
                "canonical_id": ["a", "b"],
                "snapshot_dtm": [pd.Timestamp("2026-01-15"), pd.Timestamp("2026-01-20")],
                "f1": [1.0, 2.0],
            }
        )
        with_future = pd.concat(
            [
                base,
                pd.DataFrame(
                    {
                        "canonical_id": ["c"],
                        "snapshot_dtm": [pd.Timestamp("2026-03-01")],
                        "f1": [9.0],
                    }
                ),
            ],
            ignore_index=True,
        )
        self.assertEqual(
            trainer_mod._profile_hash_chunk_scoped(base, we),
            trainer_mod._profile_hash_chunk_scoped(with_future, we),
        )

    def test_profile_hash_chunk_scoped_detects_in_window_value_change(self) -> None:
        we = pd.Timestamp("2026-02-01 00:00:00")
        p1 = pd.DataFrame(
            {
                "canonical_id": ["a"],
                "snapshot_dtm": [pd.Timestamp("2026-01-15")],
                "f1": [1.0],
            }
        )
        p2 = pd.DataFrame(
            {
                "canonical_id": ["a"],
                "snapshot_dtm": [pd.Timestamp("2026-01-15")],
                "f1": [99.0],
            }
        )
        self.assertNotEqual(
            trainer_mod._profile_hash_chunk_scoped(p1, we),
            trainer_mod._profile_hash_chunk_scoped(p2, we),
        )

    def test_chunk_cache_components_accepts_data_hash_without_bets(self) -> None:
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
            data_hash="cafebabe",
        )
        self.assertEqual(got["data_hash"], "cafebabe")

    def test_chunk_cache_components_requires_bets_or_data_hash(self) -> None:
        chunk = {
            "window_start": pd.Timestamp("2026-01-01 00:00:00"),
            "window_end": pd.Timestamp("2026-02-01 00:00:00"),
        }
        with self.assertRaises(ValueError):
            trainer_mod._chunk_cache_components(chunk, None, profile_hash="none")

    def test_chunk_cache_components_rejects_whitespace_only_data_hash(self) -> None:
        chunk = {
            "window_start": pd.Timestamp("2026-01-01 00:00:00"),
            "window_end": pd.Timestamp("2026-02-01 00:00:00"),
        }
        with self.assertRaises(ValueError):
            trainer_mod._chunk_cache_components(
                chunk, None, profile_hash="none", data_hash="   ",
            )

    def test_chunk_cache_components_strips_data_hash(self) -> None:
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
            data_hash="  deadbeef  ",
        )
        self.assertEqual(got["data_hash"], "deadbeef")

    def test_prefeatures_cache_components_excludes_spec_and_neg_sample(self) -> None:
        """Task 7 R6: pre-LLM key drops real spec hash and neg_sample (downstream-only)."""
        comp = {
            "window_start": "2026-01-01T00:00:00",
            "window_end": "2026-02-01T00:00:00",
            "data_hash": "11111111",
            "cfg_hash": "aaaaaa",
            "profile_hash": "none",
            "feature_spec_hash": "real_spec_hash",
            "neg_sample_frac": 0.25,
        }
        pc = trainer_mod._prefeatures_cache_components(comp)
        self.assertEqual(pc["feature_spec_hash"], trainer_mod._CHUNK_PREFEATURES_SPEC_PLACEHOLDER)
        self.assertEqual(pc["neg_sample_frac"], 1.0)
        self.assertEqual(pc["data_hash"], comp["data_hash"])

    def test_local_parquet_source_data_hash_changes_with_bounds_or_file_stats(self) -> None:
        ws = pd.Timestamp("2026-01-01 00:00:00")
        ee = pd.Timestamp("2026-02-01 00:00:00")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pq.write_table(pa.table({"k": [1]}), root / "gmwds_t_bet.parquet")
            pq.write_table(pa.table({"k": [1]}), root / "gmwds_t_session.parquet")
            old_root = trainer_mod.LOCAL_PARQUET_DIR
            trainer_mod.LOCAL_PARQUET_DIR = root
            try:
                h1 = trainer_mod._local_parquet_source_data_hash(ws.to_pydatetime(), ee.to_pydatetime())
                h2 = trainer_mod._local_parquet_source_data_hash(
                    ws.to_pydatetime(), pd.Timestamp("2026-03-01").to_pydatetime()
                )
                self.assertNotEqual(h1, h2)
                pq.write_table(pa.table({"k": [1, 2]}), root / "gmwds_t_bet.parquet")
                h3 = trainer_mod._local_parquet_source_data_hash(ws.to_pydatetime(), ee.to_pydatetime())
                self.assertNotEqual(h1, h3)
            finally:
                trainer_mod.LOCAL_PARQUET_DIR = old_root

    def test_local_parquet_source_data_hash_ignores_mtime_only_changes(self) -> None:
        """Portable fp_v2: same bytes + bounds → same hash after utime-only touch."""
        ws = pd.Timestamp("2026-01-01 00:00:00")
        ee = pd.Timestamp("2026-02-01 00:00:00")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bet = root / "gmwds_t_bet.parquet"
            sess = root / "gmwds_t_session.parquet"
            pq.write_table(pa.table({"k": [1]}), bet)
            pq.write_table(pa.table({"s": [1]}), sess)
            old_root = trainer_mod.LOCAL_PARQUET_DIR
            trainer_mod.LOCAL_PARQUET_DIR = root
            try:
                h0 = trainer_mod._local_parquet_source_data_hash(ws.to_pydatetime(), ee.to_pydatetime())
                t_future = os.path.getmtime(bet) + 86400.0
                os.utime(bet, (t_future, t_future))
                os.utime(sess, (t_future + 1.0, t_future + 1.0))
                h1 = trainer_mod._local_parquet_source_data_hash(ws.to_pydatetime(), ee.to_pydatetime())
            finally:
                trainer_mod.LOCAL_PARQUET_DIR = old_root
        self.assertEqual(h0, h1)

    def test_chunk_two_stage_env_empty_uses_default_true(self) -> None:
        with patch.dict(os.environ, {"CHUNK_TWO_STAGE_CACHE": ""}):
            self.assertTrue(core_config.chunk_two_stage_cache_enabled())

    def test_chunk_two_stage_env_false_disables(self) -> None:
        with patch.dict(os.environ, {"CHUNK_TWO_STAGE_CACHE": "false"}):
            self.assertFalse(core_config.chunk_two_stage_cache_enabled())
        with patch.dict(os.environ, {"CHUNK_TWO_STAGE_CACHE": "off"}):
            self.assertFalse(core_config.chunk_two_stage_cache_enabled())

    def test_trainer_chunk_two_stage_matches_config_module(self) -> None:
        with patch.dict(os.environ, {"CHUNK_TWO_STAGE_CACHE": "0"}):
            self.assertFalse(core_config.chunk_two_stage_cache_enabled())
            self.assertFalse(trainer_mod._chunk_two_stage_cache_enabled())


if __name__ == "__main__":
    unittest.main()
