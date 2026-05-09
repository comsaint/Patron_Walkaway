"""Unit tests for trainer.training.feature_materialization and trip_materializer."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pandas as pd

from trainer.features.trip_materializer import materialize_trip_layer_features
from trainer.training import feature_materialization as fm


class TestFeatureMaterializationHelpers(unittest.TestCase):
    """Spec-first helpers, fingerprints, and impact hints."""

    def test_find_undeclared_skips_reserved_and_declared(self) -> None:
        spec = {
            "bet_duckdb_window": {
                "candidates": [{"feature_id": "my_feat", "type": "passthrough"}],
            }
        }
        cols = ["my_feat", "label", "player_id", "_internal", "lda_trip_run_count", "ghost_col"]
        bad = fm.find_undeclared_feature_columns(cols, spec)
        self.assertEqual(bad, ["ghost_col"])

    def test_impacted_propagates_via_depends_on(self) -> None:
        spec = {
            "bet_duckdb_window": {
                "candidates": [
                    {"feature_id": "base_a", "type": "x"},
                    {"feature_id": "derived_c", "type": "y", "depends_on": ["base_a"]},
                    {"feature_id": "other_b", "type": "z"},
                ],
            }
        }
        prev = fm.per_feature_fingerprints(spec)
        spec2 = {
            "bet_duckdb_window": {
                "candidates": [
                    {"feature_id": "base_a", "type": "x_changed"},
                    {"feature_id": "derived_c", "type": "y", "depends_on": ["base_a"]},
                    {"feature_id": "other_b", "type": "z"},
                ],
            }
        }
        curr = fm.per_feature_fingerprints(spec2)
        hint = fm.impacted_feature_ids_on_fingerprint_change(prev, curr, spec2)
        self.assertIn("base_a", hint["changed_feature_ids"])
        self.assertIn("derived_c", hint["impacted_feature_ids"])
        self.assertNotIn("other_b", hint["changed_feature_ids"])

    def test_maybe_raise_spec_first_strict(self) -> None:
        spec = {"bet_duckdb_window": {"candidates": [{"feature_id": "f1"}]}}
        with patch.dict(os.environ, {"TRAINER_SPEC_FIRST_STRICT": "1"}):
            with self.assertRaises(RuntimeError) as ctx:
                fm.maybe_raise_spec_first_columns(["f1", "undeclared_xyz"], spec)
        self.assertIn("undeclared_xyz", str(ctx.exception))

    def test_build_audit_includes_impact_hint_when_prev_fps(self) -> None:
        spec = {"bet_duckdb_window": {"candidates": [{"feature_id": "only_one", "expression": "1"}]}}
        prev = {"only_one": "aaa"}
        spec2 = {"bet_duckdb_window": {"candidates": [{"feature_id": "only_one", "expression": "2"}]}}
        audit = fm.build_pipeline_feature_materialization_audit(
            feature_spec=spec2,
            train_columns=["only_one", "label"],
            prev_per_feature_fp=prev,
        )
        self.assertIn("impact_hint_vs_previous_run", audit)
        self.assertIn("only_one", audit["impact_hint_vs_previous_run"]["changed_feature_ids"])

    def test_build_audit_includes_impact_plan_and_cache_lexicon(self) -> None:
        spec = {"bet_duckdb_window": {"candidates": [{"feature_id": "f1", "expression": "1"}]}}
        audit = fm.build_pipeline_feature_materialization_audit(
            feature_spec=spec,
            train_columns=["f1", "label"],
            curr_source_snapshot_id="snap_x",
        )
        self.assertIn("impact_plan", audit)
        self.assertEqual(audit["impact_plan"]["impact_planner_version"], "impact_planner_v3")
        self.assertIn("cache_key_lexicon_sample", audit)
        self.assertIn("materialization_gates", audit)

    def test_upstream_closure_hash_stable(self) -> None:
        spec = {
            "bet_duckdb_window": {
                "candidates": [
                    {"feature_id": "bet_f", "expression": "1"},
                    {"feature_id": "compose_x", "expression": "2", "depends_on": ["run_f"]},
                ],
            },
            "run_state_machine": {"candidates": [{"feature_id": "run_f", "expression": "x"}]},
        }
        fps = fm.per_feature_fingerprints(spec)
        h1 = fm.upstream_fingerprint_closure_hash(spec, "compose_x", fps)
        h2 = fm.upstream_fingerprint_closure_hash(spec, "compose_x", fps)
        self.assertEqual(h1, h2)
        self.assertTrue(len(h1) >= 8)

    def test_strict_materialization_gate_raises_on_bad_asset_path(self) -> None:
        with patch.dict(
            os.environ,
            {"TRAINER_PLAYER_LAYER_ASSET_PATH": "", "TRAINER_LAYER_ASSET_BUNDLE_DIR": ""},
        ):
            rep = fm.evaluate_materialization_gate_bundle()
        self.assertTrue(rep["gates"]["player_layer_asset_path_guard"]["ok"])
        with patch.dict(
            os.environ,
            {
                "TRAINER_PLAYER_LAYER_ASSET_PATH": "/nonexistent/player_layer.parquet",
                "TRAINER_LAYER_ASSET_BUNDLE_DIR": "",
            },
        ):
            rep_bad = fm.evaluate_materialization_gate_bundle()
        self.assertFalse(rep_bad["gates"]["player_layer_asset_path_guard"]["ok"])
        with patch.dict(
            os.environ,
            {
                "TRAINER_PLAYER_LAYER_ASSET_PATH": "/nonexistent/player_layer.parquet",
                "TRAINER_LAYER_ASSET_BUNDLE_DIR": "",
                "TRAINER_MATERIALIZATION_STRICT_GATES": "1",
            },
        ):
            rep_bad2 = fm.evaluate_materialization_gate_bundle()
            with self.assertRaises(RuntimeError):
                fm.raise_if_strict_materialization_gates_failed(rep_bad2)


class TestImpactPlanner(unittest.TestCase):
    """trainer.training.impact_planner — spec + snapshot delta."""

    def test_snapshot_change_marks_full_matrix(self) -> None:
        from trainer.training.impact_planner import plan_impacted_materialization_work

        spec = {"bet_duckdb_window": {"candidates": [{"feature_id": "f1", "expression": "x"}]}}
        plan = plan_impacted_materialization_work(
            curr_spec=spec,
            prev_spec=None,
            prev_per_feature_fp=None,
            prev_source_snapshot_id="snap_a",
            curr_source_snapshot_id="snap_b",
        )
        self.assertTrue(plan["full_matrix_recommended"])
        self.assertIn("DATA_SNAPSHOT_ID_CHANGED", plan["impact_reasons"])
        self.assertGreater(plan["impacted_work_unit_count"], 0)

    def test_snapshot_change_expands_to_chunk_partitions(self) -> None:
        from trainer.training.impact_planner import plan_impacted_materialization_work

        spec = {"bet_duckdb_window": {"candidates": [{"feature_id": "f1", "expression": "x"}]}}
        pids = ["time_chunk:20250101:20250131", "time_chunk:20250201:20250228"]
        plan = plan_impacted_materialization_work(
            curr_spec=spec,
            prev_spec=None,
            prev_per_feature_fp=None,
            prev_source_snapshot_id="snap_a",
            curr_source_snapshot_id="snap_b",
            chunk_partition_ids=pids,
        )
        self.assertFalse(plan["full_matrix_recommended"])
        self.assertGreater(plan["impacted_work_unit_count"], 0)
        parts = {u["partition_id"] for u in plan["impacted_work_units"]}
        self.assertEqual(parts, set(pids))
        self.assertTrue(all(u["partition_id"] in pids for u in plan["impacted_work_units"]))
        self.assertTrue(all("asset_id" in u and len(u["asset_id"]) == 64 for u in plan["impacted_work_units"]))


class TestLayerAssetStore(unittest.TestCase):
    """trainer.training.layer_asset_store — partition id + manifest."""

    def test_chunk_partition_id_stable(self) -> None:
        from trainer.training.layer_asset_store import chunk_partition_id, write_chunk_layer_asset_manifest
        import tempfile
        from pathlib import Path
        import pandas as pd

        ws = pd.Timestamp("2025-03-01")
        we = pd.Timestamp("2025-03-31")
        self.assertEqual(chunk_partition_id(ws, we), "time_chunk:20250301:20250331")
        spec = {"bet_duckdb_window": {"candidates": [{"feature_id": "f1", "expression": "1"}]}}
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "chunk_20250301_20250331.parquet"
            p.write_bytes(b"")
            write_chunk_layer_asset_manifest(
                chunk_parquet_path=p,
                chunk={"window_start": ws, "window_end": we},
                labeled_columns=pd.Index(["f1", "label", "bet_id"]),
                feature_spec=spec,
                source_snapshot_id="snap_z",
                pit_policy_id="cutoff_window",
                pit_identity_engine="cutoff_window_map",
                row_count=3,
            )
            side = p.with_suffix(".layer_assets.json")
            self.assertTrue(side.is_file())
            from trainer.training.layer_asset_store import read_chunk_layer_asset_manifest

            mf = read_chunk_layer_asset_manifest(p)
            self.assertIsNotNone(mf)
            assert mf is not None
            self.assertEqual(mf.get("manifest_version"), "layer_asset_manifest_v2")
            self.assertIn("asset_id", mf)
            self.assertEqual(len(str(mf["asset_id"])), 64)

    def test_bundle_index_write_and_validate(self) -> None:
        import tempfile
        from pathlib import Path

        from trainer.training.layer_asset_store import (
            validate_layer_asset_bundle_index,
            write_layer_asset_bundle_index,
        )

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "a.bin").write_bytes(b"hello")
            write_layer_asset_bundle_index(
                d,
                [{"relative_path": "a.bin", "role": "test"}],
                aggregate_coverage_start="2025-01-01",
                aggregate_coverage_end="2025-03-31",
            )
            ok, msg = validate_layer_asset_bundle_index(d)
            self.assertTrue(ok, msg)

    def test_online_increment_partition_id_shape(self) -> None:
        from trainer.training.layer_asset_store import online_increment_partition_id
        import pandas as pd

        s = pd.Timestamp("2025-01-01T00:00:00", tz="UTC")
        e = pd.Timestamp("2025-01-01T00:00:45", tz="UTC")
        pid = online_increment_partition_id(s, e)
        self.assertTrue(pid.startswith("inc:"))


class TestTripMaterializer(unittest.TestCase):
    """Trip-layer materializer contract."""

    def test_fail_closed_raises_when_lda_missing(self) -> None:
        df = pd.DataFrame({"bet_id": [1]})
        with self.assertRaises(ValueError) as ctx:
            materialize_trip_layer_features(df, fail_closed=True)
        self.assertIn("lda", str(ctx.exception).lower())

    def test_lenient_returns_same_frame(self) -> None:
        df = pd.DataFrame({"bet_id": [1], "lda_trip_run_count": [0.0]})
        out = materialize_trip_layer_features(df, fail_closed=False)
        self.assertIs(out, df)
        self.assertEqual(str(out["lda_trip_run_count"].dtype), "float32")

    def test_lenient_zero_fills_missing_lda_columns(self) -> None:
        df = pd.DataFrame({"bet_id": [1]})
        out = materialize_trip_layer_features(df, fail_closed=False)
        self.assertIs(out, df)
        self.assertIn("lda_trip_run_count", out.columns)
        self.assertEqual(str(out["lda_trip_run_count"].dtype), "float32")
