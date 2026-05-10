"""Unit tests for L2 bundle auto-materialization (#17)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
from jsonschema import validate

from trainer.training.l2_bundle_materialize import (
    auto_bundle_cache_is_current,
    build_auto_l2_cache_key,
    materialize_l2_training_bundle_dir,
    stable_cache_key_fingerprint,
    write_trainer_impact_checkpoint,
)
from trainer.training.l2_training_manifest import load_and_validate_bundle
from trainer.training.l2_window_projection import try_project_l2_bundle_window
from trainer.training.l2_reuse_keys import resolve_l2_auto_cache


class TestL2BundleMaterialize(unittest.TestCase):
    def test_materialize_then_validate_and_schema(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[2] / "schema" / "l2_training_bundle.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            train = pd.DataFrame(
                {
                    "bet_id": [1, 2],
                    "payout_complete_dtm": pd.to_datetime(["2025-01-01", "2025-01-02"]),
                    "label": [0, 1],
                    "is_rated": [True, True],
                }
            )
            valid = train.iloc[:1].copy()
            test = train.iloc[1:].copy()
            key = build_auto_l2_cache_key(
                bridge_manifest_stat="1|100",
                window_start_iso="2025-01-01",
                window_end_iso="2025-06-01",
                recent_chunks=None,
                train_split_frac=0.65,
                valid_split_frac=0.2,
                neg_sample_frac_config=1.0,
                feature_spec_fingerprint="abc",
                rebuild_canonical_mapping=False,
                identity_mapping_mode="cutoff_window",
                force_recompute=False,
            )
            from trainer.core import _config_training_domain as _tdom

            if getattr(_tdom, "L2_REUSE_V3_CACHE_KEYS", True):
                self.assertEqual(key.get("kind"), "trainer_auto_l2_bundle_v3")
                self.assertIn("source_invariant", key)
                self.assertIn("window_view", key)
            else:
                self.assertEqual(key.get("kind"), "trainer_auto_l2_bundle_v2")
            materialize_l2_training_bundle_dir(
                d,
                train_df=train,
                valid_df=valid,
                test_df=test,
                train_path=None,
                valid_path=None,
                test_path=None,
                source_snapshot_id="snap_unit_test",
                train_end="2025-04-01T00:00:00",
                window_start="2025-01-01T00:00:00",
                window_end="2025-06-01T00:00:00",
                identity_mapping_mode="cutoff_window",
                train_sampling_applied=False,
                cache_key=key,
            )
            mf = json.loads((d / "l2_training_bundle.json").read_text(encoding="utf-8"))
            validate(instance=mf, schema=schema)
            self.assertEqual(mf.get("schema_version"), "2")
            self.assertIsInstance(mf.get("split_day_manifest"), dict)
            m = load_and_validate_bundle(d)
            self.assertEqual(m.source_snapshot_id, "snap_unit_test")
            self.assertEqual(m.schema_version, "2")
            self.assertGreater(len(m.train_export_paths), 0)
            self.assertTrue(m.valid_full_unsampled and m.test_full_unsampled)
            self.assertIsNone(m.per_feature_fingerprints)

    def test_materialize_persists_feature_lineage_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            train = pd.DataFrame(
                {
                    "bet_id": [1],
                    "payout_complete_dtm": pd.to_datetime(["2025-01-01"]),
                    "label": [0],
                    "is_rated": [True],
                }
            )
            key = build_auto_l2_cache_key(
                bridge_manifest_stat=None,
                window_start_iso="2025-01-01",
                window_end_iso="2025-02-01",
                recent_chunks=None,
                train_split_frac=0.65,
                valid_split_frac=0.2,
                neg_sample_frac_config=1.0,
                feature_spec_fingerprint="fp",
                rebuild_canonical_mapping=False,
                identity_mapping_mode="cutoff_window",
                force_recompute=False,
            )
            materialize_l2_training_bundle_dir(
                d,
                train_df=train,
                valid_df=train,
                test_df=train,
                train_path=None,
                valid_path=None,
                test_path=None,
                source_snapshot_id="snap_lineage",
                train_end="2025-01-15T00:00:00",
                window_start="2025-01-01T00:00:00",
                window_end="2025-02-01T00:00:00",
                identity_mapping_mode="cutoff_window",
                train_sampling_applied=False,
                cache_key=key,
                per_feature_fingerprints={"feat_a": "deadbeef01", "feat_b": "cafebabe02"},
            )
            mf = json.loads((d / "l2_training_bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(
                mf["feature_lineage"]["per_feature_fingerprints"]["feat_a"],
                "deadbeef01",
            )
            m = load_and_validate_bundle(d)
            self.assertEqual(m.per_feature_fingerprints, {"feat_a": "deadbeef01", "feat_b": "cafebabe02"})

    def test_cache_key_stable_fingerprint(self) -> None:
        k1 = build_auto_l2_cache_key(
            bridge_manifest_stat="1|1",
            window_start_iso="a",
            window_end_iso="b",
            recent_chunks=3,
            train_split_frac=0.65,
            valid_split_frac=0.2,
            neg_sample_frac_config=1.0,
            feature_spec_fingerprint="x",
            rebuild_canonical_mapping=False,
            identity_mapping_mode="cutoff_window",
            force_recompute=False,
        )
        k2 = dict(k1)
        self.assertEqual(stable_cache_key_fingerprint(k1), stable_cache_key_fingerprint(k2))

    def test_materialize_normalizes_bundle_parquet_column_case(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            train = pd.DataFrame(
                {
                    "Bet_ID": [1, 2],
                    "Payout_Complete_Dtm": pd.to_datetime(["2025-01-01", "2025-01-02"]),
                    "Label": [0, 1],
                    "Is_Rated": [True, True],
                }
            )
            key = build_auto_l2_cache_key(
                bridge_manifest_stat=None,
                window_start_iso="2025-01-01",
                window_end_iso="2025-02-01",
                recent_chunks=None,
                train_split_frac=0.65,
                valid_split_frac=0.2,
                neg_sample_frac_config=1.0,
                feature_spec_fingerprint="mixed_case",
                rebuild_canonical_mapping=False,
                identity_mapping_mode="cutoff_window",
                force_recompute=False,
            )
            materialize_l2_training_bundle_dir(
                d,
                train_df=train,
                valid_df=train.iloc[:1].copy(),
                test_df=train.iloc[1:].copy(),
                train_path=None,
                valid_path=None,
                test_path=None,
                source_snapshot_id="snap_case",
                train_end="2025-01-15T00:00:00",
                window_start="2025-01-01T00:00:00",
                window_end="2025-02-01T00:00:00",
                identity_mapping_mode="cutoff_window",
                train_sampling_applied=False,
                cache_key=key,
            )
            manifest = load_and_validate_bundle(d)
            monolithic_cols = pq.read_schema(manifest.train_path).names
            shard_cols = pq.read_schema(manifest.train_export_paths[0]).names
            self.assertIn("label", monolithic_cols)
            self.assertNotIn("Label", monolithic_cols)
            self.assertIn("payout_complete_dtm", shard_cols)
            self.assertNotIn("Payout_Complete_Dtm", shard_cols)

            read_back = pd.read_parquet(manifest.train_path)
            self.assertIn("label", read_back.columns)
            self.assertIn("is_rated", read_back.columns)
            self.assertNotIn("Label", read_back.columns)

    def test_auto_bundle_cache_is_current_requires_sidecar_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            key = build_auto_l2_cache_key(
                bridge_manifest_stat=None,
                window_start_iso="2025-01-01",
                window_end_iso="2025-02-01",
                recent_chunks=None,
                train_split_frac=0.65,
                valid_split_frac=0.2,
                neg_sample_frac_config=1.0,
                feature_spec_fingerprint="z",
                rebuild_canonical_mapping=False,
                identity_mapping_mode="cutoff_window",
                force_recompute=False,
            )
            self.assertFalse(auto_bundle_cache_is_current(bundle_dir=d, expected_key=key))
            tiny = pd.DataFrame({"bet_id": [1], "payout_complete_dtm": [pd.Timestamp("2025-01-01")], "label": [0], "is_rated": [True]})
            materialize_l2_training_bundle_dir(
                d,
                train_df=tiny,
                valid_df=tiny,
                test_df=tiny,
                train_path=None,
                valid_path=None,
                test_path=None,
                source_snapshot_id="snap_x",
                train_end="2025-01-15T00:00:00",
                window_start="2025-01-01T00:00:00",
                window_end="2025-02-01T00:00:00",
                identity_mapping_mode="cutoff_window",
                train_sampling_applied=False,
                cache_key=key,
            )
            self.assertTrue(auto_bundle_cache_is_current(bundle_dir=d, expected_key=key))

    def test_resolve_fail_closed_on_invalid_expected_key(self) -> None:
        bad = {"kind": "trainer_auto_l2_bundle_v3", "source_invariant": {}, "window_view": {}}
        with tempfile.TemporaryDirectory() as td:
            r = resolve_l2_auto_cache(
                bundle_dir=Path(td),
                expected_key=bad,
                bundle_files_ok=True,
            )
            self.assertEqual(r.get("l2_cache_miss_reason"), "invalid_expected_key")

    def test_window_projection_subset_rows(self) -> None:
        import duckdb

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            ts = pd.to_datetime(
                ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"],
            )
            train = pd.DataFrame(
                {
                    "bet_id": [1, 2, 3, 4, 5],
                    "gaming_day": ts.normalize(),
                    "payout_complete_dtm": ts,
                    "label": [0, 1, 0, 1, 0],
                    "is_rated": [True] * 5,
                }
            )
            key_wide = build_auto_l2_cache_key(
                bridge_manifest_stat=None,
                window_start_iso="2025-01-01T00:00:00",
                window_end_iso="2025-01-06T00:00:00",
                recent_chunks=None,
                train_split_frac=0.65,
                valid_split_frac=0.2,
                neg_sample_frac_config=1.0,
                feature_spec_fingerprint="proj_test",
                rebuild_canonical_mapping=False,
                identity_mapping_mode="cutoff_window",
                force_recompute=False,
            )
            materialize_l2_training_bundle_dir(
                d,
                train_df=train,
                valid_df=train.iloc[:2].copy(),
                test_df=train.iloc[2:4].copy(),
                train_path=None,
                valid_path=None,
                test_path=None,
                source_snapshot_id="snap_proj",
                train_end="2025-01-05T00:00:00",
                window_start="2025-01-01T00:00:00",
                window_end="2025-01-06T00:00:00",
                identity_mapping_mode="cutoff_window",
                train_sampling_applied=False,
                cache_key=key_wide,
            )
            key_narrow = build_auto_l2_cache_key(
                bridge_manifest_stat=None,
                window_start_iso="2025-01-02T00:00:00",
                window_end_iso="2025-01-05T00:00:00",
                recent_chunks=None,
                train_split_frac=0.65,
                valid_split_frac=0.2,
                neg_sample_frac_config=1.0,
                feature_spec_fingerprint="proj_test",
                rebuild_canonical_mapping=False,
                identity_mapping_mode="cutoff_window",
                force_recompute=False,
            )
            pr = try_project_l2_bundle_window(d, key_narrow)
            self.assertTrue(pr.get("ok"), pr)
            self.assertTrue(auto_bundle_cache_is_current(bundle_dir=d, expected_key=key_narrow))
            m = load_and_validate_bundle(d)
            con = duckdb.connect(":memory:")
            try:
                n_train = int(
                    con.execute(f"SELECT count(*) FROM read_parquet('{m.train_path}')").fetchone()[0]
                )
            finally:
                con.close()
            self.assertEqual(n_train, 3)

    def test_impact_checkpoint_roundtrip(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as td:
            fake_ckpt = Path(td) / "last_impact_checkpoint.json"
            with patch(
                "trainer.training.l2_bundle_materialize.trainer_impact_checkpoint_path",
                return_value=fake_ckpt,
            ):
                write_trainer_impact_checkpoint(
                    per_feature_fingerprints={"a": "1"},
                    source_snapshot_id="snap_ckpt",
                )
                from trainer.training.l2_bundle_materialize import read_trainer_impact_checkpoint

                raw = read_trainer_impact_checkpoint()
            self.assertIsInstance(raw, dict)
            assert raw is not None
            self.assertEqual(raw.get("source_snapshot_id"), "snap_ckpt")
            self.assertEqual(raw.get("per_feature_fingerprints"), {"a": "1"})


if __name__ == "__main__":
    unittest.main()
