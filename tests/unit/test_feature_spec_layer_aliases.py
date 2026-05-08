"""Unit tests for YAML track key mirroring (layer+method aliases)."""

from __future__ import annotations

import pathlib
import tempfile
import unittest

import yaml

from trainer.features.feature_spec_layer_aliases import mirror_layer_method_track_keys_inplace


class TestFeatureSpecLayerAliases(unittest.TestCase):
    """``bet_duckdb_window`` / ``run_state_machine`` mirror to legacy ``track_*``."""

    def test_new_keys_mirrored_to_legacy(self) -> None:
        spec = {
            "bet_duckdb_window": {"candidates": [{"feature_id": "f_x", "type": "window", "expression": "1", "window_frame": ""}]},
            "run_state_machine": {"candidates": [{"feature_id": "f_run", "dtype": "float64"}]},
        }
        mirror_layer_method_track_keys_inplace(spec)
        self.assertIn("track_llm", spec)
        self.assertIn("track_human", spec)
        self.assertEqual(spec["track_llm"]["candidates"][0]["feature_id"], "f_x")
        self.assertEqual(spec["track_human"]["candidates"][0]["feature_id"], "f_run")

    def test_conflicting_dual_sections_raises(self) -> None:
        spec = {
            "track_llm": {"candidates": [{"feature_id": "a", "type": "window", "expression": "1", "window_frame": ""}]},
            "bet_duckdb_window": {"candidates": [{"feature_id": "b", "type": "window", "expression": "1", "window_frame": ""}]},
        }
        with self.assertRaises(ValueError):
            mirror_layer_method_track_keys_inplace(spec)

    def test_runtime_subset_canonical_only_roundtrips_load(self) -> None:
        """Dual legacy+canonical sections with identical ids must not break reload (#14)."""
        from trainer.features.features import build_runtime_feature_spec_subset, load_feature_spec

        full = {
            "track_llm": {
                "candidates": [
                    {
                        "feature_id": "a",
                        "type": "window",
                        "expression": "1",
                        "window_frame": "",
                    },
                    {
                        "feature_id": "b",
                        "type": "window",
                        "expression": "2",
                        "window_frame": "",
                    },
                ]
            },
            "bet_duckdb_window": {
                "candidates": [
                    {
                        "feature_id": "a",
                        "type": "window",
                        "expression": "1",
                        "window_frame": "",
                    },
                    {
                        "feature_id": "b",
                        "type": "window",
                        "expression": "2",
                        "window_frame": "",
                    },
                ]
            },
            "track_human": {"candidates": []},
            "run_state_machine": {"candidates": []},
            "track_profile": {"candidates": []},
            "player_profile_snapshot": {"candidates": []},
        }
        mirror_layer_method_track_keys_inplace(full)
        frozen = build_runtime_feature_spec_subset(full, ["a"])
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "subset.yaml"
            p.write_text(yaml.safe_dump(frozen, sort_keys=False), encoding="utf-8")
            loaded = load_feature_spec(p)
        self.assertIn("bet_duckdb_window", loaded)
        self.assertNotIn("track_llm", yaml.safe_dump(frozen))
        ids = [c["feature_id"] for c in loaded["bet_duckdb_window"]["candidates"] if c.get("feature_id")]
        self.assertEqual(ids, ["a"])


if __name__ == "__main__":
    unittest.main()
