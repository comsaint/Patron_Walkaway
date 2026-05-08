"""Unit tests for label disk cache helpers and L2 contract frame builder."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from trainer.training.label_asset_cache import (
    build_label_asset_contract_dataframe,
    build_label_disk_cache_components,
    label_asset_cache_disabled,
    label_disk_cache_fingerprint,
    try_load_label_intermediate_cache,
    write_label_intermediate_cache,
)


class TestLabelAssetCache(unittest.TestCase):
    """Label intermediate cache fingerprint + L2 contract narrow frame."""

    def test_label_disk_cache_roundtrip(self) -> None:
        comp = build_label_disk_cache_components(
            window_start_iso="2024-01-01T00:00:00",
            window_end_iso="2024-01-08T00:00:00",
            extended_end_iso="2024-01-09T00:00:00",
            data_hash="dh1",
            walkaway_gap_min=30,
            alert_horizon_min=60,
            label_lookahead_min=120,
            identity_mapping_mode="cutoff_window",
            pit_identity_engine="cutoff_window_map",
            source_snapshot_id="snap-a",
        )
        comp["bets_label_input_hash"] = "bh99"
        fp = label_disk_cache_fingerprint(comp)
        self.assertEqual(len(fp), 24)
        df = pd.DataFrame(
            {
                "canonical_id": ["c1", "c1"],
                "bet_id": [1, 2],
                "payout_complete_dtm": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "label": pd.array([0, 1], dtype="int8"),
                "censored": [False, False],
            }
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "lab.parquet"
            k = Path(d) / "lab.key"
            write_label_intermediate_cache(labeled=df, parquet_path=p, sidecar_path=k, components=comp)
            loaded = try_load_label_intermediate_cache(
                parquet_path=p,
                sidecar_path=k,
                expected_fingerprint=fp,
                expected_components=comp,
                expected_n_rows=len(df),
            )
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(len(loaded), 2)

    def test_build_label_asset_contract_dataframe(self) -> None:
        cov = datetime(2024, 1, 9, tzinfo=timezone.utc)
        rows = pd.DataFrame(
            {
                "bet_id": [10],
                "canonical_id": ["x"],
                "label": pd.array([1], dtype="int8"),
                "censored": [False],
            }
        )
        out = build_label_asset_contract_dataframe(rows, source_snapshot_id="s1", coverage_end=cov)
        from trainer.training.l2_trainer_contracts import LABEL_ASSET_REQUIRED_COLUMNS

        self.assertEqual(list(out.columns), list(LABEL_ASSET_REQUIRED_COLUMNS))
        self.assertIn("is_censored", out.columns)
        self.assertFalse(bool(out["is_censored"].iloc[0]))

    def test_label_asset_cache_disabled_env(self) -> None:
        # Default off env -> not disabled
        self.assertFalse(label_asset_cache_disabled())


if __name__ == "__main__":
    unittest.main()
