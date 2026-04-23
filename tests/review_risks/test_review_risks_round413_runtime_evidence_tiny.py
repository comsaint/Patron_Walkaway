"""Round 413 — tiny-data runtime evidence for rated-only early-prune.

Goal: keep RAM/CPU usage minimal while proving heavy-path row-count reduction.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import trainer.serving.scorer as scorer_mod
import trainer.training.trainer as trainer_mod


class TestR413TinyRuntimeEvidence(unittest.TestCase):
    def _bets(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "bet_id": [1, 2],
                "player_id": [100, 999],  # 100 rated, 999 unrated
                "session_id": [10, 20],
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
        return pd.DataFrame({"session_id": [10, 20], "player_id": [100, 999]})

    def test_trainer_tiny_chunk_heavy_path_rows_reduce_2_to_1(self):
        bets = self._bets()
        sessions = self._sessions()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["100"]})
        observed = {"track_human_rows": -1, "elapsed_s": None}

        def _capture_track_human(df: pd.DataFrame, *_args, **_kwargs):
            observed["track_human_rows"] = len(df)
            out = df.copy()
            out["run_id"] = 0
            out["minutes_since_run_start"] = 0.0
            out["bets_in_run_so_far"] = 1
            out["wager_sum_in_run_so_far"] = out.get("wager", 0.0)
            out["loss_streak"] = 0
            return out

        def _labels_stub(bets_df: pd.DataFrame, window_end, extended_end):
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
                patch.object(trainer_mod, "compute_labels", side_effect=_labels_stub),
                patch.object(trainer_mod, "join_player_profile", side_effect=lambda x, y: x),
            ):
                chunk = {
                    "window_start": dt.datetime(2026, 2, 1, 0, 0, 0),
                    "window_end": dt.datetime(2026, 2, 2, 0, 0, 0),
                    "extended_end": dt.datetime(2026, 2, 2, 1, 0, 0),
                }
                t0 = time.perf_counter()
                ret = trainer_mod.process_chunk(
                    chunk=chunk,
                    canonical_map=canonical_map,
                    use_local_parquet=False,
                    force_recompute=True,
                    feature_spec=None,
                    profile_df=None,
                )
                observed["elapsed_s"] = time.perf_counter() - t0

        # Baseline pre-prune would feed 2 rows to heavy path; now reduced to 1 rated row.
        self.assertEqual(observed["track_human_rows"], 1)
        self.assertIsNotNone(ret)
        self.assertIsNotNone(observed["elapsed_s"])
        self.assertLess(observed["elapsed_s"], 5.0)

    def test_scorer_tiny_formal_fe_rows_reduce_2_to_1(self):
        bets = self._bets().copy()
        sessions = self._sessions().copy()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["100"]})
        observed = {"formal_fe_rows": -1}

        def _capture_build_features(bets_df: pd.DataFrame, sessions_df, canonical_map_df, cutoff_time):
            observed["formal_fe_rows"] = len(bets_df)
            out = bets_df.copy()
            out["canonical_id"] = out["player_id"].astype(str)
            out["is_rated"] = out["player_id"].astype(str).isin({"100"})
            out["feat_a"] = 0.0
            out["feat_b"] = 0.0
            return out

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
            ):
                # Stop flow immediately after FE section.
                with patch.object(scorer_mod, "_score_df", side_effect=RuntimeError("stop_after_fe")):
                    try:
                        scorer_mod.score_once(
                            artifacts={
                                "feature_list": ["feat_a", "feat_b"],
                                "model_version": "test-v",
                                "rated": {"model": object(), "threshold": 0.5, "features": ["feat_a", "feat_b"]},
                            },
                            lookback_hours=8,
                            alert_history=set(),
                            conn=conn,
                            rebuild_canonical_mapping=True,
                        )
                    except RuntimeError as exc:
                        self.assertEqual(str(exc), "stop_after_fe")
        finally:
            conn.close()

        self.assertEqual(observed["formal_fe_rows"], 1)


if __name__ == "__main__":
    unittest.main()
