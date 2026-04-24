from __future__ import annotations

import importlib
import pathlib
import sys
import unittest

import pandas as pd


def _import_features():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return importlib.import_module("trainer.features")


features_mod = _import_features()


class TestConsecutiveNonWinStreak(unittest.TestCase):
    def test_consecutive_non_win_streak_resets_on_win(self) -> None:
        df = pd.DataFrame(
            {
                "canonical_id": ["A"] * 6,
                "bet_id": [1, 2, 3, 4, 5, 6],
                "payout_complete_dtm": pd.to_datetime(
                    [
                        "2026-01-01 10:00:00",
                        "2026-01-01 10:01:00",
                        "2026-01-01 10:02:00",
                        "2026-01-01 10:03:00",
                        "2026-01-01 10:04:00",
                        "2026-01-01 10:05:00",
                    ]
                ),
                "status": ["LOSE", "PUSH", "WIN", "LOSE", "PUSH", "WIN"],
            }
        )
        got = features_mod.compute_consecutive_non_win_streak(df)
        self.assertListEqual(got.tolist(), [1, 2, 0, 1, 2, 0])

    def test_consecutive_non_win_wrapper_returns_dataframe_column(self) -> None:
        df = pd.DataFrame(
            {
                "canonical_id": ["A", "A"],
                "bet_id": [1, 2],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-01-01 10:00:00", "2026-01-01 10:01:00"]
                ),
                "status": ["LOSE", "PUSH"],
            }
        )
        out = features_mod.compute_consecutive_non_win_features(df)
        self.assertIn("consecutive_non_win_cnt", out.columns)
        self.assertListEqual(out["consecutive_non_win_cnt"].tolist(), [1, 2])

    def test_consecutive_non_win_streak_respects_lookback_hours(self) -> None:
        df = pd.DataFrame(
            {
                "canonical_id": ["A", "A", "A"],
                "bet_id": [1, 2, 3],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-01-01 10:00:00", "2026-01-01 10:05:00", "2026-01-01 10:10:00"]
                ),
                "status": ["LOSE", "PUSH", "LOSE"],
            }
        )
        got = features_mod.compute_consecutive_non_win_streak(df, lookback_hours=0.1)
        self.assertListEqual(got.tolist(), [1, 2, 2])


class TestLossStreakWrapper(unittest.TestCase):
    def test_loss_streak_wrapper_returns_dataframe_column(self) -> None:
        df = pd.DataFrame(
            {
                "canonical_id": ["A", "A", "A"],
                "bet_id": [1, 2, 3],
                "payout_complete_dtm": pd.to_datetime(
                    ["2026-01-01 10:00:00", "2026-01-01 10:01:00", "2026-01-01 10:02:00"]
                ),
                "status": ["LOSE", "PUSH", "WIN"],
            }
        )
        out = features_mod.compute_loss_streak_features(df)
        self.assertIn("loss_streak", out.columns)
        self.assertListEqual(out["loss_streak"].tolist(), [1, 1, 0])


class TestWave2PersonalizedBaselines(unittest.TestCase):
    def test_add_wave2_personalized_baselines(self) -> None:
        df = pd.DataFrame(
            {
                "minutes_since_run_start": [30.0],
                "avg_session_duration_min_30d": [60.0],
                "bets_in_run_so_far": [5],
                "num_bets_sum_30d": [120.0],
                "sessions_30d": [10.0],
                "bets_cnt_w15m": [6.0],
            }
        )
        out = features_mod.add_wave2_personalized_baselines(df)
        self.assertAlmostEqual(float(out.loc[0, "run_duration_vs_personal_avg"]), 0.5)
        self.assertAlmostEqual(float(out.loc[0, "bets_in_run_vs_personal_avg"]), 5.0 / 12.0)
        self.assertAlmostEqual(float(out.loc[0, "pace_vs_personal_baseline"]), 2.0)

    def test_add_wave2_personalized_baselines_default_mutates_in_place(self) -> None:
        df = pd.DataFrame({"minutes_since_run_start": [10.0]})
        out = features_mod.add_wave2_personalized_baselines(df)
        self.assertIs(out, df)


if __name__ == "__main__":
    unittest.main()
