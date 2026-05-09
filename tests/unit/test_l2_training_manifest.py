"""Unit tests for L2 training bundle manifest (GitHub #16 / TRN-17-01)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trainer.training.l2_training_manifest import (
    L2_TRAINING_BUNDLE_MANIFEST_FILE,
    estimate_step7_peak_ram_gb_from_split_bytes,
    load_and_validate_bundle,
    split_parquet_total_bytes,
)


class TestL2TrainingManifest(unittest.TestCase):
    def test_rejects_conflicting_snapshot_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "t.parquet").write_bytes(b"x")
            (d / "v.parquet").write_bytes(b"x")
            (d / "e.parquet").write_bytes(b"x")
            (d / L2_TRAINING_BUNDLE_MANIFEST_FILE).write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "source_snapshot_id": "snap-a",
                        "snapshot_id": "snap-b",
                        "l2_snapshot_id": "l2-1",
                        "train_end": "2024-01-01T00:00:00",
                        "window_start": "2023-01-01T00:00:00",
                        "window_end": "2024-01-01T00:00:00",
                        "paths": {"train": "t.parquet", "valid": "v.parquet", "test": "e.parquet"},
                        "split_semantics": {
                            "valid_full_unsampled": True,
                            "test_full_unsampled": True,
                            "train_sampling_applied": False,
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load_and_validate_bundle(d)
            self.assertIn("conflicting snapshot", str(ctx.exception).lower())

    def test_load_ok_and_split_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name, content in (("tr.parquet", b"a"), ("va.parquet", b"bb"), ("te.parquet", b"ccc")):
                (d / name).write_bytes(content)
            (d / L2_TRAINING_BUNDLE_MANIFEST_FILE).write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "source_snapshot_id": "snap-one",
                        "l2_snapshot_id": "l2-one",
                        "train_end": "2024-01-01T00:00:00",
                        "window_start": "2023-01-01T00:00:00",
                        "window_end": "2024-01-01T00:00:00",
                        "paths": {"train": "tr.parquet", "valid": "va.parquet", "test": "te.parquet"},
                        "split_semantics": {
                            "valid_full_unsampled": True,
                            "test_full_unsampled": True,
                            "train_sampling_applied": False,
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            m = load_and_validate_bundle(d)
            self.assertEqual(m.source_snapshot_id, "snap-one")
            self.assertEqual(m.schema_version, "1")
            self.assertEqual(m.train_export_paths, (m.train_path,))
            self.assertIsNone(m.split_day_manifest)
            tb = split_parquet_total_bytes(m)
            self.assertEqual(tb, 1 + 2 + 3)
            peak = estimate_step7_peak_ram_gb_from_split_bytes(
                tb,
                train_split_frac=0.7,
                use_duckdb=True,
                chunk_concat_ram_factor=3.0,
            )
            self.assertGreater(peak, 0.0)

    def test_schema_v2_resolves_export_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            shard_train = d / "day_shards" / "train" / "day=2025-01-01"
            shard_train.mkdir(parents=True)
            (shard_train / "part.parquet").write_bytes(b"x")
            for name in ("tr.parquet", "va.parquet", "te.parquet"):
                (d / name).write_bytes(b"y")
            manifest_obj = {
                "schema_version": "2",
                "source_snapshot_id": "snap-v2",
                "l2_snapshot_id": "l2-v2",
                "train_end": "2025-01-10T00:00:00",
                "window_start": "2025-01-01T00:00:00",
                "window_end": "2025-01-15T00:00:00",
                "paths": {"train": "tr.parquet", "valid": "va.parquet", "test": "te.parquet"},
                "split_day_manifest": {
                    "train": [{"day": "2025-01-01", "path": "day_shards/train/day=2025-01-01/part.parquet"}],
                    "valid": [{"day": "2025-01-01", "path": "va.parquet"}],
                    "test": [{"day": "2025-01-01", "path": "te.parquet"}],
                },
                "split_calendar": {
                    "train": {"gaming_day_min": "2025-01-01", "gaming_day_max": "2025-01-01"},
                },
                "split_semantics": {
                    "valid_full_unsampled": True,
                    "test_full_unsampled": True,
                    "train_sampling_applied": False,
                },
                "identity_mapping_mode": "cutoff_window",
            }
            (d / L2_TRAINING_BUNDLE_MANIFEST_FILE).write_text(
                json.dumps(manifest_obj, indent=2),
                encoding="utf-8",
            )
            m = load_and_validate_bundle(d)
            self.assertEqual(m.schema_version, "2")
            self.assertEqual(len(m.train_export_paths), 1)
            self.assertTrue(m.train_export_paths[0].is_file())
            self.assertEqual(m.valid_export_paths, (m.valid_path,))
