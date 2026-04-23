"""Round 411 review risks — rated-only early-prune contracts (tests only).

Focus:
- Trainer/backtester should perform rated-only pruning before heavy FE.
- Scorer should avoid running formal FE on unrated rows.
"""

from __future__ import annotations

import inspect
import unittest

import trainer.serving.scorer as scorer_mod
import trainer.training.backtester as backtester_mod
import trainer.training.trainer as trainer_mod


class TestR411TrainerEarlyPruneContract(unittest.TestCase):
    """Trainer: prune unrated rows before Track Human / Track LLM / labels."""

    def test_process_chunk_should_define_rated_subset_before_track_human(self):
        source = inspect.getsource(trainer_mod.process_chunk)
        idx_identity = source.find('bets["canonical_id"] = bets["canonical_id"].fillna')
        idx_track_human = source.find("add_track_human_features(", idx_identity)
        idx_rated_marker = source.find("rated_ids", idx_identity, idx_track_human)

        self.assertGreater(idx_identity, -1, "process_chunk should attach canonical_id before FE.")
        self.assertGreater(idx_track_human, -1, "process_chunk should call add_track_human_features.")
        self.assertGreater(
            idx_rated_marker,
            -1,
            "R411 #1: process_chunk should build rated-only routing before add_track_human_features.",
        )


class TestR411BacktesterEarlyPruneContract(unittest.TestCase):
    """Backtester: parity with trainer on early rated-only pruning."""

    def test_backtest_should_prune_before_track_human(self):
        source = inspect.getsource(backtester_mod.backtest)
        idx_identity = source.find('bets["canonical_id"] = bets["canonical_id"].fillna')
        idx_track_human = source.find("add_track_human_features(", idx_identity)
        idx_rated_hint = source.find("is_rated", idx_identity, idx_track_human)

        self.assertGreater(idx_identity, -1, "backtest should attach canonical_id before FE.")
        self.assertGreater(idx_track_human, -1, "backtest should call add_track_human_features.")
        self.assertGreater(
            idx_rated_hint,
            -1,
            "R411 #2: backtest should apply rated-only pruning before add_track_human_features.",
        )


class TestR411ScorerTelemetrySplitContract(unittest.TestCase):
    """Scorer: non-rated telemetry must not require full FE output."""

    def test_score_once_should_not_compute_unrated_telemetry_from_features_all(self):
        source = inspect.getsource(scorer_mod.score_once)
        self.assertNotIn(
            "_telemetry_new = features_all",
            source,
            "R411 #3: scorer telemetry should not depend on features_all full FE output.",
        )

    def test_score_once_should_filter_bets_for_features_to_rated_before_formal_fe(self):
        source = inspect.getsource(scorer_mod.score_once)
        self.assertIn(
            "rated_player_ids",
            source,
            "R411 #4: scorer should define rated player routing set before formal FE.",
        )
        self.assertIn(
            'bets_for_features["player_id"].astype(str).isin(rated_player_ids)',
            source,
            "R411 #4: scorer formal FE input should be filtered to rated player_ids.",
        )


if __name__ == "__main__":
    unittest.main()
