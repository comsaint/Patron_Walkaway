"""Minimal reproducible tests for Round 123 Review risks (tests-only, no production changes).

R123-1: Trainer should coerce dtypes before training to prevent LightGBM crash on object columns.
R123-2: R1001 fallback should use YAML track_human, not hardcoded TRACK_B_FEATURE_COLS.
"""
import unittest
import inspect


class TestRound123Risks(unittest.TestCase):

    def test_r123_1_trainer_should_coerce_dtypes_before_training_to_prevent_lgbm_crash(self):
        """R123-1: train_single_rated_model (or path that builds X_train) must call coerce_feature_dtypes
        so that object-typed candidate columns do not reach LightGBM and cause a crash."""
        import trainer.trainer as trainer_mod
        src = inspect.getsource(trainer_mod.train_single_rated_model)
        self.assertIn(
            "coerce_feature_dtypes",
            src,
            "train_single_rated_model must coerce feature dtypes before building X_tr / fit to prevent LGBM crash.",
        )

    def test_r123_2_trainer_fallback_should_use_yaml_track_human_not_hardcoded_list(self):
        """R123-2: R1001 fallback must not use hardcoded TRACK_B_FEATURE_COLS; should use
        get_candidate_feature_ids(feature_spec, 'track_human', ...) from YAML (SSOT)."""
        import trainer.trainer as trainer_mod
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertNotIn(
            "TRACK_B_FEATURE_COLS",
            src,
            "run_pipeline R1001 fallback must not reference TRACK_B_FEATURE_COLS; use YAML track_human instead.",
        )


if __name__ == "__main__":
    unittest.main()
