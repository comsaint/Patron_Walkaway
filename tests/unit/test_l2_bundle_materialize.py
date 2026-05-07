"""Unit tests for L2 bundle auto-materialization (#17)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from jsonschema import validate

from trainer.training.l2_bundle_materialize import (
    auto_bundle_cache_is_current,
    build_auto_l2_cache_key,
    materialize_l2_training_bundle_dir,
    stable_cache_key_fingerprint,
)
from trainer.training.l2_training_manifest import load_and_validate_bundle


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
            m = load_and_validate_bundle(d)
            self.assertEqual(m.source_snapshot_id, "snap_unit_test")
            self.assertTrue(m.valid_full_unsampled and m.test_full_unsampled)

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


if __name__ == "__main__":
    unittest.main()
