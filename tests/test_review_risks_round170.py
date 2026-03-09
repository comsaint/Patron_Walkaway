"""Minimal reproducible guardrail tests for Round 47 review risks (R500-R505).

Tests-only round: no production code changes.
Known un-fixed risks are encoded as expected failures so they remain visible
without breaking the full suite.
"""

from __future__ import annotations

import datetime as dt
import inspect
import unittest
from unittest.mock import patch

import pandas as pd

import trainer.backtester as backtester_mod
import trainer.features as features_mod
import trainer.trainer as trainer_mod


class TestR500BacktesterTzAwareBoundary(unittest.TestCase):
    """R500: backtester should not crash on tz-aware window boundaries."""

    def test_backtest_tz_aware_window_should_not_raise_typeerror(self):
        # Minimal DQ output fixture (tz-naive payout_complete_dtm), matching the
        # post-apply_dq contract in trainer.py.
        bets = pd.DataFrame(
            {
                "bet_id": [1],
                "session_id": [10],
                "player_id": [100],
                "table_id": [7],
                "payout_complete_dtm": [pd.Timestamp("2026-02-06 00:01:00")],
                "wager": [100.0],
                "status": ["LOSE"],
            }
        )
        sessions = pd.DataFrame({"session_id": [10], "player_id": [100]})

        aware_start = dt.datetime(2026, 2, 6, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        aware_end = dt.datetime(2026, 2, 13, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        # Keep the execution path minimal and deterministic:
        # - apply_dq returns our tiny fixture
        # - canonical map empty -> fallback canonical_id path
        # - Track-B returns input unchanged (we only care about label-stage tz mismatch)
        # - load_player_profile / join_player_profile mocked so backtest runs without ClickHouse
        def _minimal_join_profile(labeled, profile_df):
            return labeled.copy()

        with patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)), patch.object(
            backtester_mod, "build_canonical_mapping_from_df", return_value=pd.DataFrame()
        ), patch.object(backtester_mod, "add_track_b_features", side_effect=lambda df, *_: df), patch.object(
            backtester_mod, "load_player_profile", return_value=None
        ), patch.object(backtester_mod, "join_player_profile", side_effect=_minimal_join_profile):
            # R500: should not raise TypeError (tz-aware window); no ClickHouse required (mocked).
            _ = backtester_mod.backtest(
                bets_raw=bets,
                sessions_raw=sessions,
                artifacts={},
                window_start=aware_start,
                window_end=aware_end,
                run_optuna=False,
            )


class TestR501RunPipelineEffectiveWindowNormalization(unittest.TestCase):
    """R501: run_pipeline should normalize effective_start/effective_end to tz-naive."""

    def test_run_pipeline_should_strip_tz_on_effective_window(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertRegex(
            src,
            r"effective_start\s*=\s*effective_start\.replace\(tzinfo=None\)",
            "run_pipeline should normalize effective_start to tz-naive near assignment.",
        )
        self.assertRegex(
            src,
            r"effective_end\s*=\s*effective_end\.replace\(tzinfo=None\)",
            "run_pipeline should normalize effective_end to tz-naive near assignment.",
        )


class TestR502Dec018Assertions(unittest.TestCase):
    """R502: DEC-018 suggested assertions should exist in production code."""

    def test_apply_dq_should_assert_tz_naive_output(self):
        src = inspect.getsource(trainer_mod.apply_dq)
        self.assertIn(
            "R23 violation: payout_complete_dtm must be tz-naive after DQ",
            src,
            "apply_dq should assert tz-naive payout_complete_dtm at function exit.",
        )

    def test_process_chunk_should_assert_tz_naive_boundaries(self):
        src = inspect.getsource(trainer_mod.process_chunk)
        self.assertIn(
            "must be tz-naive inside process_chunk",
            src,
            "process_chunk should assert tz-naive window boundaries after DEC-018 strip.",
        )


class TestR503R504DesignGuardrails(unittest.TestCase):
    """R503/R504: keep explicit structural guardrails around cache and split logic."""

    def test_chunk_cache_key_uses_original_chunk_isoformat(self):
        src = inspect.getsource(trainer_mod._chunk_cache_key)
        self.assertIn(
            'ws = chunk["window_start"].isoformat()',
            src,
            "_chunk_cache_key should keep original chunk isoformat for stable key semantics.",
        )
        self.assertIn(
            'we = chunk["window_end"].isoformat()',
            src,
            "_chunk_cache_key should keep original chunk isoformat for stable key semantics.",
        )

    def test_concat_split_keeps_defensive_tz_strip(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "if _payout_ts.dt.tz is not None:",
            src,
            "run_pipeline split assignment should keep defensive tz strip guard.",
        )


class TestR505TrackBCutoffDocContract(unittest.TestCase):
    """R505: Track B docstrings should explicitly state tz-naive cutoff contract."""

    def test_track_b_docstrings_should_mention_tz_naive_cutoff(self):
        targets = [
            features_mod.compute_loss_streak,
            features_mod.compute_run_boundary,
            features_mod.compute_table_hc,
        ]
        for fn in targets:
            doc = inspect.getdoc(fn) or ""
            self.assertRegex(
                doc.lower(),
                r"tz-?naive",
                f"{fn.__name__} docstring should mention tz-naive cutoff contract.",
            )


if __name__ == "__main__":
    unittest.main()

