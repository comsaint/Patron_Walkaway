"""feat-consolidation Step 8 — 向後相容與 YAML 完整性測試.

- 向後相容：載入舊版 feature_list（含 "B"/"legacy"/"profile"）時 scorer 不報錯，
  且 profile 欄位（track "profile" 或 "track_profile"）維持 NaN 語義。
- Train-serve parity：同一批資料、同一套函式（features.compute_*），特徵值一致。
"""

from __future__ import annotations

import unittest


class TestScorerTrainServeParityTrackB(unittest.TestCase):
    """Step 8: Same batch of data + same Track B functions (features.py) -> same feature values (scorer path)."""

    def test_track_b_loss_streak_minutes_since_run_match_shared_functions(self):
        """Scorer build_features_for_scoring Track B columns match direct features.compute_* on same prepared bets."""
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.features import compute_loss_streak, compute_run_boundary
        from trainer.scorer import build_features_for_scoring

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        cutoff_naive = cutoff.replace(tzinfo=None)
        bets = pd.DataFrame({
            "bet_id": [1, 2, 3],
            "session_id": ["s1", "s1", "s1"],
            "player_id": [100, 100, 100],
            "table_id": ["t1", "t1", "t1"],
            "payout_complete_dtm": pd.to_datetime([
                "2026-03-01 11:00:00",
                "2026-03-01 11:05:00",
                "2026-03-01 11:10:00",
            ]),
            "wager": [10.0, 20.0, 15.0],
            "status": ["LOSE", "WIN", "LOSE"],
            "payout_odds": [1.9, 2.0, 1.95],
            "base_ha": [0.02, 0.02, 0.02],
            "is_back_bet": [1, 1, 1],
            "position_idx": [0, 1, 2],
        })
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})

        out = build_features_for_scoring(bets, sessions, canonical_map, cutoff)
        self.assertIn("loss_streak", out.columns)
        self.assertIn("minutes_since_run_start", out.columns)

        # Replicate scorer prep (merge + sort) and call shared Track B functions
        bets_df = bets.copy()
        for col in ["position_idx", "payout_odds", "base_ha", "is_back_bet", "wager"]:
            if col not in bets_df.columns:
                bets_df[col] = 0.0
        for col in ["position_idx", "payout_odds", "base_ha", "wager"]:
            bets_df[col] = pd.to_numeric(bets_df[col], errors="coerce").fillna(0)
        bets_df["is_back_bet"] = pd.to_numeric(bets_df["is_back_bet"], errors="coerce").fillna(0)
        bets_df["status"] = bets_df.get("status", pd.Series("", index=bets_df.index)).astype(str).str.upper()
        pcd = pd.to_datetime(bets_df["payout_complete_dtm"])
        if pcd.dt.tz is not None:
            pcd = pcd.dt.tz_convert(HK_TZ).dt.tz_localize(None)
        bets_df["payout_complete_dtm"] = pcd
        bets_df = bets_df.merge(
            canonical_map[["player_id", "canonical_id"]].drop_duplicates("player_id"),
            on="player_id",
            how="left",
        )
        bets_df["canonical_id"] = bets_df["canonical_id"].fillna(bets_df["player_id"].astype(str))
        bets_df = bets_df.sort_values(
            ["canonical_id", "payout_complete_dtm", "bet_id"], kind="stable"
        ).reset_index(drop=True)

        direct_streak = compute_loss_streak(bets_df, cutoff_time=cutoff_naive).fillna(0)
        rb = compute_run_boundary(bets_df, cutoff_time=cutoff_naive)
        direct_minutes = rb["minutes_since_run_start"] if "minutes_since_run_start" in rb.columns else pd.Series(0.0, index=bets_df.index)

        pd.testing.assert_series_equal(out["loss_streak"].reset_index(drop=True), direct_streak.reset_index(drop=True), check_names=False)
        pd.testing.assert_series_equal(out["minutes_since_run_start"].reset_index(drop=True), direct_minutes.reset_index(drop=True), check_names=False)

    def test_build_features_for_scoring_prep_contract(self):
        """R138-1: build_features_for_scoring output has canonical_id and is sorted by (canonical_id, payout_complete_dtm, bet_id)."""
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.scorer import build_features_for_scoring

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        bets = pd.DataFrame({
            "bet_id": [2, 1, 3],
            "session_id": ["s1", "s1", "s1"],
            "player_id": [100, 100, 100],
            "table_id": ["t1", "t1", "t1"],
            "payout_complete_dtm": pd.to_datetime(["2026-03-01 11:10:00", "2026-03-01 11:00:00", "2026-03-01 11:05:00"]),
            "wager": [15.0, 10.0, 20.0],
            "status": ["LOSE", "LOSE", "WIN"],
            "payout_odds": [1.9, 1.9, 2.0],
            "base_ha": [0.02, 0.02, 0.02],
            "is_back_bet": [1, 1, 1],
            "position_idx": [0, 1, 2],
        })
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})
        out = build_features_for_scoring(bets, sessions, canonical_map, cutoff)
        self.assertIn("canonical_id", out.columns)
        expected_order = out.sort_values(
            ["canonical_id", "payout_complete_dtm", "bet_id"], kind="stable"
        ).reset_index(drop=True)
        pd.testing.assert_frame_equal(
            out.reset_index(drop=True),
            expected_order,
            check_like=True,
        )

    def test_track_b_parity_two_players(self):
        """R138-2: Track B parity holds with two players / two canonical_ids."""
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.features import compute_loss_streak, compute_run_boundary
        from trainer.scorer import build_features_for_scoring

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        cutoff_naive = cutoff.replace(tzinfo=None)
        bets = pd.DataFrame({
            "bet_id": [1, 2, 3, 4],
            "session_id": ["s1", "s1", "s2", "s2"],
            "player_id": [100, 200, 100, 200],
            "table_id": ["t1", "t1", "t1", "t1"],
            "payout_complete_dtm": pd.to_datetime([
                "2026-03-01 11:00:00", "2026-03-01 11:02:00",
                "2026-03-01 11:05:00", "2026-03-01 11:07:00",
            ]),
            "wager": [10.0, 20.0, 15.0, 25.0],
            "status": ["LOSE", "WIN", "LOSE", "LOSE"],
            "payout_odds": [1.9, 2.0, 1.95, 1.9],
            "base_ha": [0.02] * 4,
            "is_back_bet": [1] * 4,
            "position_idx": [0, 0, 1, 1],
        })
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100, 200], "canonical_id": ["c100", "c200"]})

        out = build_features_for_scoring(bets, sessions, canonical_map, cutoff)
        self.assertIn("loss_streak", out.columns)
        self.assertIn("minutes_since_run_start", out.columns)

        bets_df = bets.copy()
        for col in ["position_idx", "payout_odds", "base_ha", "is_back_bet", "wager"]:
            if col not in bets_df.columns:
                bets_df[col] = 0.0
        for col in ["position_idx", "payout_odds", "base_ha", "wager"]:
            bets_df[col] = pd.to_numeric(bets_df[col], errors="coerce").fillna(0)
        bets_df["is_back_bet"] = pd.to_numeric(bets_df["is_back_bet"], errors="coerce").fillna(0)
        bets_df["status"] = bets_df.get("status", pd.Series("", index=bets_df.index)).astype(str).str.upper()
        pcd = pd.to_datetime(bets_df["payout_complete_dtm"])
        if pcd.dt.tz is not None:
            pcd = pcd.dt.tz_convert(HK_TZ).dt.tz_localize(None)
        bets_df["payout_complete_dtm"] = pcd
        bets_df = bets_df.merge(
            canonical_map[["player_id", "canonical_id"]].drop_duplicates("player_id"),
            on="player_id",
            how="left",
        )
        bets_df["canonical_id"] = bets_df["canonical_id"].fillna(bets_df["player_id"].astype(str))
        bets_df = bets_df.sort_values(
            ["canonical_id", "payout_complete_dtm", "bet_id"], kind="stable"
        ).reset_index(drop=True)

        direct_streak = compute_loss_streak(bets_df, cutoff_time=cutoff_naive).fillna(0)
        rb = compute_run_boundary(bets_df, cutoff_time=cutoff_naive)
        direct_minutes = rb["minutes_since_run_start"] if "minutes_since_run_start" in rb.columns else pd.Series(0.0, index=bets_df.index)

        pd.testing.assert_series_equal(out["loss_streak"].reset_index(drop=True), direct_streak.reset_index(drop=True), check_names=False)
        pd.testing.assert_series_equal(out["minutes_since_run_start"].reset_index(drop=True), direct_minutes.reset_index(drop=True), check_names=False)

    def test_track_b_parity_tz_aware_inputs(self):
        """R138-3: build_features_for_scoring with tz-aware payout_complete_dtm runs and produces sane Track B columns."""
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.scorer import build_features_for_scoring

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        ts = pd.to_datetime(["2026-03-01 11:00:00", "2026-03-01 11:05:00", "2026-03-01 11:10:00"], utc=True).tz_convert(HK_TZ)
        bets = pd.DataFrame({
            "bet_id": [1, 2, 3],
            "session_id": ["s1", "s1", "s1"],
            "player_id": [100, 100, 100],
            "table_id": ["t1", "t1", "t1"],
            "payout_complete_dtm": ts,
            "wager": [10.0, 20.0, 15.0],
            "status": ["LOSE", "WIN", "LOSE"],
            "payout_odds": [1.9, 2.0, 1.95],
            "base_ha": [0.02, 0.02, 0.02],
            "is_back_bet": [1, 1, 1],
            "position_idx": [0, 1, 2],
        })
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})

        out = build_features_for_scoring(bets, sessions, canonical_map, cutoff)
        self.assertEqual(len(out), 3, "tz-aware payout_complete_dtm should produce one row per bet")
        self.assertIn("loss_streak", out.columns)
        self.assertIn("minutes_since_run_start", out.columns)


class TestScorerBackwardCompatFeatureList(unittest.TestCase):
    """Step 8: Scorer accepts legacy feature_list.json (track B/legacy/profile)."""

    def test_score_df_with_legacy_track_profile_does_not_crash(self):
        """feature_list_meta with track 'profile' (legacy) or 'track_profile' treats those as profile."""
        import numpy as np
        import pandas as pd

        from trainer.scorer import _score_df

        # Minimal df: one non-profile + one profile column; profile has NaN
        df = pd.DataFrame({
            "wager": [10.0, 20.0],
            "days_since_last_session": [np.nan, 5.0],
            "is_rated": [True, True],
        })
        feature_list = ["wager", "days_since_last_session"]
        # Old format: track "B" and legacy "profile" (not "track_profile")
        artifacts = {
            "rated": None,
            "feature_list_meta": [
                {"name": "wager", "track": "B"},
                {"name": "days_since_last_session", "track": "profile"},
            ],
        }
        out = _score_df(df, artifacts, feature_list)
        self.assertIn("score", out)
        self.assertIn("days_since_last_session", out)
        # Profile column should still have NaN where it was NaN (not zero-filled)
        self.assertTrue(pd.isna(out["days_since_last_session"].iloc[0]))

    def test_score_df_with_track_b_and_legacy_does_not_crash(self):
        """feature_list_meta with only track 'B' and 'legacy' runs without error."""
        import pandas as pd

        from trainer.scorer import _score_df

        df = pd.DataFrame({
            "wager": [10.0],
            "base_ha": [0.02],
            "is_rated": [True],
        })
        feature_list = ["wager", "base_ha"]
        artifacts = {
            "rated": None,
            "feature_list_meta": [
                {"name": "wager", "track": "B"},
                {"name": "base_ha", "track": "legacy"},
            ],
        }
        out = _score_df(df, artifacts, feature_list)
        self.assertIn("score", out)
        self.assertEqual(out["score"].iloc[0], 0.0)

    def test_r144_scorer_accepts_legacy_track_and_distinguishes_profile_vs_non_profile(self):
        """Round 144 Review P2: feature_list with track 'B'/'legacy' → non-profile (zero-filled);
        track 'profile'/'track_profile' → profile (NaN preserved). Scorer must load and apply correctly."""
        import numpy as np
        import pandas as pd

        from trainer.scorer import _score_df

        df = pd.DataFrame({
            "wager": [10.0, 20.0],
            "base_ha": [0.02, 0.03],
            "days_since_last_session": [np.nan, 5.0],
            "is_rated": [True, True],
        })
        feature_list = ["wager", "base_ha", "days_since_last_session"]
        artifacts = {
            "rated": None,
            "feature_list_meta": [
                {"name": "wager", "track": "B"},
                {"name": "base_ha", "track": "legacy"},
                {"name": "days_since_last_session", "track": "profile"},
            ],
        }
        out = _score_df(df, artifacts, feature_list)
        self.assertIn("score", out)
        # Non-profile (B/legacy): absent values get 0; present stay as-is (no NaN in these cols here).
        self.assertIn("wager", out.columns)
        self.assertIn("base_ha", out.columns)
        # Profile (track "profile"): must preserve NaN where input was NaN (R74/R79).
        self.assertTrue(pd.isna(out["days_since_last_session"].iloc[0]))
        self.assertEqual(out["days_since_last_session"].iloc[1], 5.0)


class TestScorerNoSessionComputesFeatureList(unittest.TestCase):
    """Step 8: When session is empty, scorer can still compute all feature_list features (no session dependency)."""

    def test_build_features_with_empty_sessions_has_required_columns(self):
        """build_features_for_scoring(bets, empty_sessions, ...) produces columns needed for a session-free feature_list."""
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.scorer import build_features_for_scoring

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        # Minimal bets: no session data needed for Track B + legacy raw columns
        bets = pd.DataFrame({
            "bet_id": [1, 2],
            "session_id": ["s1", "s1"],
            "player_id": [100, 100],
            "table_id": ["t1", "t1"],
            "payout_complete_dtm": pd.to_datetime(["2026-03-01 11:00:00", "2026-03-01 11:05:00"]),
            "wager": [10.0, 20.0],
            "status": ["LOSE", "WIN"],
            "payout_odds": [1.9, 2.0],
            "base_ha": [0.02, 0.02],
            "is_back_bet": [1, 1],
            "position_idx": [0, 1],
        })
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})

        out = build_features_for_scoring(bets, sessions, canonical_map, cutoff)
        self.assertFalse(out.empty, "build_features_for_scoring should return non-empty DataFrame")
        # feature_list that does not depend on session (Track B + legacy passthrough)
        feature_list = ["wager", "loss_streak", "minutes_since_run_start"]
        for col in feature_list:
            self.assertIn(col, out.columns, f"feature_list column {col} should be present when session is empty")

    def test_score_df_after_build_features_no_session(self):
        """With features built from empty sessions, _score_df still produces scores for session-free feature_list."""
        import numpy as np
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.scorer import build_features_for_scoring, _score_df

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        bets = pd.DataFrame({
            "bet_id": [1],
            "session_id": ["s1"],
            "player_id": [100],
            "table_id": ["t1"],
            "payout_complete_dtm": pd.to_datetime(["2026-03-01 11:00:00"]),
            "wager": [15.0],
            "status": ["LOSE"],
            "payout_odds": [1.9],
            "base_ha": [0.02],
            "is_back_bet": [1],
            "position_idx": [0],
        })
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})
        features_df = build_features_for_scoring(bets, sessions, canonical_map, cutoff)
        feature_list = ["wager", "loss_streak", "minutes_since_run_start"]
        artifacts = {
            "rated": None,
            "feature_list_meta": [
                {"name": "wager", "track": "legacy"},
                {"name": "loss_streak", "track": "track_human"},
                {"name": "minutes_since_run_start", "track": "track_human"},
            ],
        }
        out = _score_df(features_df, artifacts, feature_list)
        self.assertIn("score", out)
        self.assertEqual(len(out), 1)
        self.assertTrue(np.issubdtype(out["score"].dtype, np.floating), "score should be float")


class TestScorerRound135ReviewRisks(unittest.TestCase):
    """Round 135 Review: minimal reproducible tests for reviewer risk points (tests only, no production changes)."""

    def test_build_features_empty_bets_returns_early_no_exception(self):
        """R135-1: build_features_for_scoring with empty bets returns empty DataFrame, no exception."""
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.scorer import build_features_for_scoring

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        out = build_features_for_scoring(
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), cutoff
        )
        self.assertIsInstance(out, pd.DataFrame)
        self.assertEqual(len(out), 0)

    def test_build_features_empty_sessions_empty_canonical_map_still_has_feature_columns(self):
        """R135-2: Empty sessions + empty canonical_map (cold start) still produces feature columns and _score_df runs."""
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.scorer import build_features_for_scoring, _score_df

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        bets = pd.DataFrame({
            "bet_id": [1],
            "session_id": ["s1"],
            "player_id": [100],
            "table_id": ["t1"],
            "payout_complete_dtm": pd.to_datetime(["2026-03-01 11:00:00"]),
            "wager": [10.0],
            "status": ["LOSE"],
            "payout_odds": [1.9],
            "base_ha": [0.02],
            "is_back_bet": [1],
            "position_idx": [0],
        })
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame(columns=["player_id", "canonical_id"])

        out = build_features_for_scoring(bets, sessions, canonical_map, cutoff)
        for col in ["wager", "loss_streak", "minutes_since_run_start", "canonical_id"]:
            self.assertIn(col, out.columns, f"column {col} should be present with empty sessions + empty canonical_map")
        feature_list = ["wager", "loss_streak", "minutes_since_run_start"]
        artifacts = {
            "rated": None,
            "feature_list_meta": [
                {"name": "wager", "track": "legacy"},
                {"name": "loss_streak", "track": "track_human"},
                {"name": "minutes_since_run_start", "track": "track_human"},
            ],
        }
        scored = _score_df(out, artifacts, feature_list)
        self.assertIn("score", scored)

    def test_score_df_feature_list_includes_profile_column_no_session_fills_nan(self):
        """R135-3: feature_list includes profile column; input df has no profile col; _score_df fills NaN, no crash."""
        import pandas as pd

        from trainer.scorer import _score_df

        df = pd.DataFrame({"wager": [10.0], "is_rated": [True]})
        feature_list = ["wager", "days_since_last_session"]
        artifacts = {
            "rated": None,
            "feature_list_meta": [
                {"name": "wager", "track": "legacy"},
                {"name": "days_since_last_session", "track": "profile"},
            ],
        }
        out = _score_df(df, artifacts, feature_list)
        self.assertIn("score", out)
        self.assertIn("days_since_last_session", out.columns)
        self.assertTrue(pd.isna(out["days_since_last_session"].iloc[0]))

    def test_build_features_empty_sessions_session_derived_cols_zero(self):
        """R135-4: With empty sessions, session_duration_min and bets_per_minute are 0 (no inf/NaN)."""
        import pandas as pd
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from trainer.scorer import build_features_for_scoring

        HK_TZ = ZoneInfo("Asia/Hong_Kong")
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)
        bets = pd.DataFrame({
            "bet_id": [1],
            "session_id": ["s1"],
            "player_id": [100],
            "table_id": ["t1"],
            "payout_complete_dtm": pd.to_datetime(["2026-03-01 11:00:00"]),
            "wager": [10.0],
            "status": ["LOSE"],
            "payout_odds": [1.9],
            "base_ha": [0.02],
            "is_back_bet": [1],
            "position_idx": [0],
        })
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})
        out = build_features_for_scoring(bets, sessions, canonical_map, cutoff)
        self.assertTrue(out["session_duration_min"].eq(0.0).all())
        self.assertTrue(out["bets_per_minute"].notna().all())
        self.assertEqual(out["session_duration_min"].iloc[0], 0.0)
        self.assertEqual(out["bets_per_minute"].iloc[0], 0.0)


if __name__ == "__main__":
    unittest.main()
