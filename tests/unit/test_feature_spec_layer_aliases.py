"""Unit tests for YAML track key mirroring (layer+method aliases)."""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
