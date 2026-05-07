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
            tb = split_parquet_total_bytes(m)
            self.assertEqual(tb, 1 + 2 + 3)
            peak = estimate_step7_peak_ram_gb_from_split_bytes(
                tb,
                train_split_frac=0.7,
                use_duckdb=True,
                chunk_concat_ram_factor=3.0,
            )
            self.assertGreater(peak, 0.0)
