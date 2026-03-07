import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path
import json
import importlib

class TestRound119Risks(unittest.TestCase):

    def test_r119_1_features_module_io_should_handle_missing_yaml_gracefully(self):
        """Risk #1: features.py performs module-level I/O.
        After fix: a missing YAML should not crash on import — fallback to empty spec."""
        import trainer.features as features_mod

        yaml_path = Path(features_mod.__file__).parent / "feature_spec" / "features_candidates.template.yaml"
        backup_path = yaml_path.with_suffix(".yaml.bak")

        if not yaml_path.exists():
            self.skipTest("Template YAML not found, cannot run this test.")

        yaml_path.rename(backup_path)
        try:
            # After fix: reload should succeed with an empty fallback, not raise FileNotFoundError
            importlib.reload(features_mod)
        finally:
            backup_path.rename(yaml_path)
            # Restore full module state
            importlib.reload(features_mod)

    def test_r119_2_save_artifact_bundle_should_not_use_screening_only_for_track_classification(self):
        """Risk #2: features with screening_eligible=False must still be correctly classified
        by track, not defaulted to track_llm."""
        import trainer.trainer as trainer_mod

        mock_spec = {
            "track_human": {
                "candidates": [
                    {"feature_id": "human_secret", "screening_eligible": False}
                ]
            }
        }

        with tempfile.TemporaryDirectory() as td:
            model_dir = Path(td)

            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                spec_path = model_dir / "mock_spec.yaml"
                import yaml
                with open(spec_path, "w") as f:
                    yaml.dump(mock_spec, f)

                trainer_mod.save_artifact_bundle(
                    rated={"model": "mock", "threshold": 0.5, "features": ["human_secret"]},
                    feature_cols=["human_secret"],
                    combined_metrics={},
                    model_version="v1",
                    feature_spec_path=spec_path,
                )

            with open(model_dir / "feature_list.json") as f:
                feature_list = json.load(f)

        track_by_name = {c["name"]: c["track"] for c in feature_list}
        # human_secret is in track_human, even though screening_eligible=False
        self.assertEqual(track_by_name.get("human_secret"), "track_human")

    def test_r119_3_backtester_should_fill_zeros_based_on_artifact_features(self):
        """Risk #3: backtester should use the model artifact's feature list for zero-filling,
        not re-parse all YAML candidates (which may include unscreened extras)."""
        import trainer.backtester as bt_mod
        import inspect
        # The function is `backtest`, not `process_chunk_backtest` (which never existed).
        src = inspect.getsource(bt_mod.backtest)

        self.assertNotIn(
            "get_all_candidate_feature_ids",
            src,
            "backtester.backtest should not depend on get_all_candidate_feature_ids for zero-filling.",
        )

if __name__ == "__main__":
    unittest.main()
