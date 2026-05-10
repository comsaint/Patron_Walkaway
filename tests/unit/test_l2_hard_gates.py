"""Hard-gate B/C tests: impact orchestrator contract, projection parity, optional perf smoke."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
import pandas as pd
import pytest

from trainer.training.l2_bundle_materialize import (
    build_auto_l2_cache_key,
    materialize_l2_training_bundle_dir,
)
from trainer.training.l2_impact_orchestrator import (
    impacted_partition_ids_from_plan,
    raise_if_impacted_only_chunk_miss_forbidden,
)
from trainer.training.l2_training_manifest import load_and_validate_bundle
from trainer.training.l2_window_projection import try_project_l2_bundle_window


class TestImpactOrchestrator(unittest.TestCase):
    def test_impacted_partition_ids_from_plan(self) -> None:
        plan = {
            "impacted_work_units": [
                {"layer": "bet", "feature_id": "a", "partition_id": "time_chunk:20250101:20250131"},
                {"layer": "bet", "feature_id": "b", "partition_id": "*"},
            ]
        }
        pids = impacted_partition_ids_from_plan(plan)
        self.assertEqual(pids, frozenset({"time_chunk:20250101:20250131"}))

    def test_impacted_partition_ids_empty(self) -> None:
        self.assertEqual(impacted_partition_ids_from_plan(None), frozenset())
        self.assertEqual(impacted_partition_ids_from_plan({}), frozenset())

    def test_strict_forbids_stale_miss(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            raise_if_impacted_only_chunk_miss_forbidden(
                impact_orchestrator_mode="enforce",
                orchestrator_execution_mode="impacted_only",
                allow_chunk_full_fallback=False,
                chunk_label="2025-01-01–2025-01-07",
                miss_reasons=["spec"],
            )
        self.assertIn("L2_IMPACT_ALLOW_CHUNK_FULL_FALLBACK", str(ctx.exception))

    def test_strict_allows_when_fallback_on(self) -> None:
        raise_if_impacted_only_chunk_miss_forbidden(
            impact_orchestrator_mode="enforce",
            orchestrator_execution_mode="impacted_only",
            allow_chunk_full_fallback=True,
            chunk_label="x",
            miss_reasons=["spec"],
        )


class TestProjectionParity(unittest.TestCase):
    def test_projection_train_matches_fresh_narrow_materialize(self) -> None:
        """C: subset window via projection vs materializing only that window — train rows parity."""
        ts = pd.to_datetime(
            ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"],
        )
        train = pd.DataFrame(
            {
                "bet_id": [10, 20, 30, 40, 50],
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
            feature_spec_fingerprint="parity_fp",
            rebuild_canonical_mapping=False,
            identity_mapping_mode="cutoff_window",
            force_recompute=False,
        )
        key_narrow = build_auto_l2_cache_key(
            bridge_manifest_stat=None,
            window_start_iso="2025-01-02T00:00:00",
            window_end_iso="2025-01-05T00:00:00",
            recent_chunks=None,
            train_split_frac=0.65,
            valid_split_frac=0.2,
            neg_sample_frac_config=1.0,
            feature_spec_fingerprint="parity_fp",
            rebuild_canonical_mapping=False,
            identity_mapping_mode="cutoff_window",
            force_recompute=False,
        )
        with tempfile.TemporaryDirectory() as td_proj:
            d_proj = Path(td_proj)
            materialize_l2_training_bundle_dir(
                d_proj,
                train_df=train,
                valid_df=train.iloc[:2].copy(),
                test_df=train.iloc[2:4].copy(),
                train_path=None,
                valid_path=None,
                test_path=None,
                source_snapshot_id="snap_parity",
                train_end="2025-01-05T00:00:00",
                window_start="2025-01-01T00:00:00",
                window_end="2025-01-06T00:00:00",
                identity_mapping_mode="cutoff_window",
                train_sampling_applied=False,
                cache_key=key_wide,
            )
            pr = try_project_l2_bundle_window(d_proj, key_narrow)
            self.assertTrue(pr.get("ok"), pr)
            m_proj = load_and_validate_bundle(d_proj)
            bids_p = sorted(pd.read_parquet(m_proj.train_path)["bet_id"].astype(int).tolist())

        train_narrow = train[train["gaming_day"] >= pd.Timestamp("2025-01-02").normalize()]
        train_narrow = train_narrow[train_narrow["gaming_day"] < pd.Timestamp("2025-01-05").normalize()]
        with tempfile.TemporaryDirectory() as td_fresh:
            d_fresh = Path(td_fresh)
            materialize_l2_training_bundle_dir(
                d_fresh,
                train_df=train_narrow.reset_index(drop=True),
                valid_df=train.iloc[:2].copy(),
                test_df=train.iloc[2:4].copy(),
                train_path=None,
                valid_path=None,
                test_path=None,
                source_snapshot_id="snap_parity",
                train_end="2025-01-04T00:00:00",
                window_start="2025-01-02T00:00:00",
                window_end="2025-01-05T00:00:00",
                identity_mapping_mode="cutoff_window",
                train_sampling_applied=False,
                cache_key=key_narrow,
            )
            m_fresh = load_and_validate_bundle(d_fresh)
            bids_f = sorted(pd.read_parquet(m_fresh.train_path)["bet_id"].astype(int).tolist())

        self.assertEqual(bids_p, bids_f)


@pytest.mark.skipif(os.getenv("L2_GATE_PERF") != "1", reason="set L2_GATE_PERF=1 for optional perf smoke")
def test_projection_second_call_faster_smoke() -> None:
    """C (optional): second projection on same bundle is fast (no-op path or cheap re-filter)."""
    ts = pd.to_datetime(["2025-02-01", "2025-02-02", "2025-02-03"])
    train = pd.DataFrame(
        {
            "bet_id": [1, 2, 3],
            "gaming_day": ts.normalize(),
            "payout_complete_dtm": ts,
            "label": [0, 1, 0],
            "is_rated": [True, True, True],
        }
    )
    key_w = build_auto_l2_cache_key(
        bridge_manifest_stat=None,
        window_start_iso="2025-02-01T00:00:00",
        window_end_iso="2025-02-04T00:00:00",
        recent_chunks=None,
        train_split_frac=0.65,
        valid_split_frac=0.2,
        neg_sample_frac_config=1.0,
        feature_spec_fingerprint="perf_fp",
        rebuild_canonical_mapping=False,
        identity_mapping_mode="cutoff_window",
        force_recompute=False,
    )
    key_n = build_auto_l2_cache_key(
        bridge_manifest_stat=None,
        window_start_iso="2025-02-01T00:00:00",
        window_end_iso="2025-02-03T00:00:00",
        recent_chunks=None,
        train_split_frac=0.65,
        valid_split_frac=0.2,
        neg_sample_frac_config=1.0,
        feature_spec_fingerprint="perf_fp",
        rebuild_canonical_mapping=False,
        identity_mapping_mode="cutoff_window",
        force_recompute=False,
    )
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        materialize_l2_training_bundle_dir(
            d,
            train_df=train,
            valid_df=train.iloc[:1].copy(),
            test_df=train.iloc[1:2].copy(),
            train_path=None,
            valid_path=None,
            test_path=None,
            source_snapshot_id="snap_perf",
            train_end="2025-02-03T00:00:00",
            window_start="2025-02-01T00:00:00",
            window_end="2025-02-04T00:00:00",
            identity_mapping_mode="cutoff_window",
            train_sampling_applied=False,
            cache_key=key_w,
        )
        t0 = time.perf_counter()
        r1 = try_project_l2_bundle_window(d, key_n)
        t1 = time.perf_counter() - t0
        assert r1.get("ok")
        t0 = time.perf_counter()
        r2 = try_project_l2_bundle_window(d, key_n)
        t2 = time.perf_counter() - t0
        assert r2.get("ok")
        assert t2 <= max(t1 * 3.0, 5.0), f"second projection slower than expected: t1={t1:.3f}s t2={t2:.3f}s"


if __name__ == "__main__":
    unittest.main()
