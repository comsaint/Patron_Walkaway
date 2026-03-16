"""Minimal reproducible tests for Round 127 review risks (tests-only).

R127-1: backtester should not zero-fill profile features.
R127-2: trainer process_chunk should derive profile exclusion from YAML, not hardcoded PROFILE_FEATURE_COLS.
"""

import inspect
import unittest


class TestRound127Risks(unittest.TestCase):

    def test_r127_1_backtester_should_not_zero_fill_profile_features(self):
        """R127-1: backtester zero-fill should exclude track_profile features.

        Current code applies fillna(0) to all artifact features, which erases
        the intended NaN semantics for profile features (train-serve parity risk).
        """
        import trainer.backtester as bt_mod

        src = inspect.getsource(bt_mod.backtest)
        self.assertNotIn(
            "labeled[_artifact_features] = labeled[_artifact_features].fillna(0)",
            src,
            "backtest should not blanket fillna(0) across all artifact features.",
        )

    def test_r127_2_trainer_process_chunk_should_use_yaml_for_profile_exclusion(self):
        """R127-2: process_chunk should not rely on hardcoded PROFILE_FEATURE_COLS
        for deciding non-profile columns in default-fill logic."""
        import trainer.trainer as trainer_mod

        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertNotIn(
            "if c not in PROFILE_FEATURE_COLS",
            src,
            "process_chunk should derive profile set from feature_spec/YAML, not PROFILE_FEATURE_COLS.",
        )


if __name__ == "__main__":
    unittest.main()
