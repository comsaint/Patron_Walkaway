"""Round 412 — rated-only early-prune behavior tests (not source-only contracts)."""

from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.serving.scorer as scorer_mod
import trainer.training.backtester as backtester_mod
import trainer.training.trainer as trainer_mod


class _MockPredictProba:
    def predict_proba(self, x):
        return np.array([[0.8, 0.2]] * len(x))


class TestR412TrainerBacktesterBehavior(unittest.TestCase):
    def _bets(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "bet_id": [1, 2],
                "player_id": [100, 999],  # 100 = rated, 999 = unrated
                "session_id": [10, 11],
                "table_id": [1, 1],
                "payout_complete_dtm": [
                    pd.Timestamp("2026-02-01 12:00:00"),
                    pd.Timestamp("2026-02-01 12:01:00"),
                ],
                "wager": [100.0, 50.0],
                "status": ["LOSE", "WIN"],
                "gaming_day": [pd.Timestamp("2026-02-01").date(), pd.Timestamp("2026-02-01").date()],
            }
        )

    def _sessions(self) -> pd.DataFrame:
        return pd.DataFrame({"session_id": [10, 11], "player_id": [100, 999]})

    def test_process_chunk_track_human_input_is_rated_only(self):
        bets = self._bets()
        sessions = self._sessions()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["100"]})
        seen: dict[str, set[str]] = {}

        def _capture_track_human(df: pd.DataFrame, *_args, **_kwargs):
            seen["cids"] = set(df["canonical_id"].astype(str).tolist())
            out = df.copy()
            out["run_id"] = 0
            out["minutes_since_run_start"] = 0.0
            out["bets_in_run_so_far"] = 1
            out["wager_sum_in_run_so_far"] = out.get("wager", 0.0)
            out["loss_streak"] = 0
            return out

        def _minimal_labels(bets_df: pd.DataFrame, window_end, extended_end):
            out = bets_df.copy()
            out["label"] = 0
            out["censored"] = False
            return out

        with tempfile.TemporaryDirectory() as td:
            out_path = Path(td) / "chunk.parquet"
            with (
                patch.object(trainer_mod, "_chunk_parquet_path", return_value=out_path),
                patch.object(trainer_mod, "load_clickhouse_data", return_value=(bets, sessions)),
                patch.object(trainer_mod, "normalize_bets_sessions", return_value=(bets, sessions)),
                patch.object(trainer_mod, "apply_dq", return_value=(bets, sessions)),
                patch.object(trainer_mod, "add_track_human_features", side_effect=_capture_track_human),
                patch.object(trainer_mod, "compute_labels", side_effect=_minimal_labels),
                patch.object(trainer_mod, "join_player_profile", side_effect=lambda x, y: x),
            ):
                chunk = {
                    "window_start": dt.datetime(2026, 2, 1, 0, 0, 0),
                    "window_end": dt.datetime(2026, 2, 2, 0, 0, 0),
                    "extended_end": dt.datetime(2026, 2, 2, 1, 0, 0),
                }
                ret = trainer_mod.process_chunk(
                    chunk=chunk,
                    canonical_map=canonical_map,
                    use_local_parquet=False,
                    force_recompute=True,
                    feature_spec=None,
                    profile_df=None,
                )
        self.assertEqual(seen.get("cids"), {"100"})
        self.assertIsNotNone(ret)

    def test_backtest_track_human_input_is_rated_only(self):
        bets = self._bets()
        sessions = self._sessions()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["100"]})
        seen: dict[str, set[str]] = {}

        def _capture_track_human(df: pd.DataFrame, *_args, **_kwargs):
            seen["cids"] = set(df["canonical_id"].astype(str).tolist())
            return df.copy()

        def _minimal_labels(bets_df: pd.DataFrame, window_end, extended_end):
            out = bets_df.copy()
            out["label"] = 0
            out["censored"] = False
            return out

        artifacts = {
            "rated": {"model": _MockPredictProba(), "features": ["feat_a", "feat_b"], "threshold": 0.5},
            "feature_list_meta": [],
        }
        with (
            patch.object(backtester_mod, "apply_dq", return_value=(bets, sessions)),
            patch.object(backtester_mod, "build_canonical_mapping_from_df", return_value=canonical_map),
            patch.object(backtester_mod, "add_track_human_features", side_effect=_capture_track_human),
            patch.object(backtester_mod, "load_feature_spec", return_value={"track_llm": {"candidates": []}}),
            patch.object(backtester_mod, "compute_track_llm_features", return_value=pd.DataFrame({"bet_id": [1]})),
            patch.object(backtester_mod, "compute_labels", side_effect=_minimal_labels),
            patch.object(backtester_mod, "load_player_profile", return_value=None),
            patch.object(backtester_mod, "join_player_profile", side_effect=lambda x, y: x),
        ):
            out = backtester_mod.backtest(
                bets_raw=bets,
                sessions_raw=sessions,
                artifacts=artifacts,
                window_start=dt.datetime(2026, 2, 1, 0, 0, 0),
                window_end=dt.datetime(2026, 2, 2, 0, 0, 0),
                run_optuna=False,
            )
        self.assertEqual(seen.get("cids"), {"100"})
        self.assertNotIn("error", out)


class TestR412ScorerBehavior(unittest.TestCase):
    def test_score_once_formal_fe_and_score_input_are_rated_only(self):
        now_hk_naive = pd.Timestamp.now(tz="Asia/Hong_Kong").tz_localize(None)
        bets = pd.DataFrame(
            {
                "bet_id": [1, 2],
                "player_id": [100, 999],  # 100 rated, 999 unrated
                "session_id": [10, 20],
                "table_id": [1, 1],
                "payout_complete_dtm": [
                    now_hk_naive - pd.Timedelta(minutes=2),
                    now_hk_naive - pd.Timedelta(minutes=1),
                ],
                "wager": [100.0, 50.0],
                "status": ["LOSE", "WIN"],
                "canonical_id": ["100", "999"],
            }
        )
        sessions = pd.DataFrame({"session_id": [10, 20], "player_id": [100, 999]})
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["100"]})
        seen: dict[str, set[str]] = {}

        def _capture_build_features(bets_df: pd.DataFrame, sessions_df, canonical_map_df, cutoff_time):
            seen["fe_player_ids"] = set(bets_df["player_id"].astype(str).tolist())
            out = bets_df.copy()
            out["canonical_id"] = out["player_id"].astype(str)
            out["is_rated"] = out["player_id"].astype(str).isin({"100"})
            out["feat_a"] = 0.0
            out["feat_b"] = 0.0
            return out

        def _capture_score_df(df: pd.DataFrame, artifacts, feature_list, rated_threshold=None):
            seen["score_player_ids"] = set(df["player_id"].astype(str).tolist())
            out = df.copy()
            out["score"] = 0.2
            out["is_rated_obs"] = out["is_rated"].fillna(False).astype(int)
            out["margin"] = -0.1
            return out

        artifacts = {
            "feature_list": ["feat_a", "feat_b"],
            "model_version": "test-v",
            "rated": {"model": _MockPredictProba(), "threshold": 0.5, "features": ["feat_a", "feat_b"]},
        }

        conn = sqlite3.connect(":memory:")
        try:
            with (
                patch.object(scorer_mod, "refresh_alert_history", return_value=None),
                patch.object(scorer_mod, "fetch_recent_data", return_value=(bets, sessions)),
                patch.object(scorer_mod, "normalize_bets_sessions", return_value=(bets, sessions)),
                patch.object(scorer_mod, "prune_old_state", return_value=None),
                patch.object(scorer_mod, "update_state_with_new_bets", return_value=bets.copy()),
                patch.object(scorer_mod, "build_canonical_mapping_from_df", return_value=canonical_map),
                patch.object(scorer_mod, "_select_incremental_bets_window", side_effect=lambda b, n, c: b.copy()),
                patch.object(scorer_mod, "build_features_for_scoring", side_effect=_capture_build_features),
                patch.object(scorer_mod, "_join_profile", None),
                patch.object(scorer_mod, "_score_df", side_effect=_capture_score_df),
            ):
                scorer_mod.score_once(
                    artifacts=artifacts,
                    lookback_hours=8,
                    alert_history=set(),
                    conn=conn,
                    rebuild_canonical_mapping=True,
                )
        finally:
            conn.close()

        self.assertEqual(seen.get("fe_player_ids"), {"100"})
        self.assertEqual(seen.get("score_player_ids"), {"100"})


if __name__ == "__main__":
    unittest.main()
