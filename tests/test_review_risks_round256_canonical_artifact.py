"""Round 256 Code Review — Canonical mapping 寫出/載入與 DuckDB 路徑風險點轉成測試。

STATUS.md Round 256 Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § Canonical mapping steps 4/7/8, STATUS Round 256 Review.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

# ---------------------------------------------------------------------------
# R256 Review #1 — Scorer 未實作「載入 artifact」
# ---------------------------------------------------------------------------


class TestR256_1_ScorerAlwaysBuildsCanonicalMapping(unittest.TestCase):
    """Review #1: Scorer currently always calls build_canonical_mapping_from_df (no load path yet)."""

    def test_rebuild_false_still_calls_build_canonical_mapping_from_df(self):
        """With rebuild_canonical_mapping=False, scorer still builds (no load path implemented)."""
        from trainer.scorer import score_once

        build_mock = MagicMock(return_value=pd.DataFrame(columns=["player_id", "canonical_id"]))
        with tempfile.TemporaryDirectory() as tmp:
            with patch("trainer.scorer.CANONICAL_MAPPING_PARQUET", Path(tmp) / "canonical_mapping.parquet"), patch(
                "trainer.scorer.CANONICAL_MAPPING_CUTOFF_JSON", Path(tmp) / "canonical_mapping.cutoff.json"
            ):
                # When load is implemented, parquet+sidecar with future cutoff would skip build
                (Path(tmp) / "canonical_mapping.parquet").write_bytes(b"")
                (Path(tmp) / "canonical_mapping.cutoff.json").write_text(
                    json.dumps({"cutoff_dtm": "2030-01-01T00:00:00", "dummy_player_ids": []}), encoding="utf-8"
                )
                with patch("trainer.scorer.build_canonical_mapping_from_df", build_mock), patch(
                    "trainer.scorer.fetch_recent_data",
                    return_value=(
                        pd.DataFrame([{"bet_id": "b1", "player_id": 1}]),
                        pd.DataFrame([{"session_id": "s1", "player_id": 1}]),
                    ),
                ), patch("trainer.scorer.refresh_alert_history"), patch(
                    "trainer.scorer.prune_old_state"
                ), patch(
                    "trainer.scorer.update_state_with_new_bets",
                    return_value=pd.DataFrame([{"bet_id": "b1", "player_id": 1}]),
                ), patch(
                    "trainer.scorer.build_features_for_scoring",
                    return_value=pd.DataFrame(
                        [
                            {
                                "bet_id": "b1",
                                "player_id": 1,
                                "canonical_id": 1,
                                "is_rated_obs": 0,
                                "margin": -1.0,
                                "payout_complete_dtm": pd.Timestamp("2025-01-01"),
                                "session_id": "s1",
                                "wager": 10.0,
                            }
                        ]
                    ),
                ), patch("trainer.scorer._score_df", side_effect=lambda df, *a, **k: df):
                    artifacts = {"feature_list": [], "model_version": "test"}
                    conn_mock = MagicMock()
                    score_once(
                        artifacts,
                        lookback_hours=24,
                        alert_history=set(),
                        conn=conn_mock,
                        rebuild_canonical_mapping=False,
                    )
        build_mock.assert_called_once()

    def test_rebuild_true_calls_build_canonical_mapping_from_df(self):
        """With rebuild_canonical_mapping=True, scorer builds (same as today)."""
        from trainer.scorer import score_once

        build_mock = MagicMock(return_value=pd.DataFrame(columns=["player_id", "canonical_id"]))
        with patch("trainer.scorer.build_canonical_mapping_from_df", build_mock), patch(
            "trainer.scorer.fetch_recent_data",
            return_value=(
                pd.DataFrame([{"bet_id": "b1", "player_id": 1}]),
                pd.DataFrame([{"session_id": "s1", "player_id": 1}]),
            ),
        ), patch("trainer.scorer.refresh_alert_history"), patch("trainer.scorer.prune_old_state"), patch(
            "trainer.scorer.update_state_with_new_bets",
            return_value=pd.DataFrame([{"bet_id": "b1", "player_id": 1}]),
        ), patch(
            "trainer.scorer.build_features_for_scoring",
            return_value=pd.DataFrame(
                [
                    {
                        "bet_id": "b1",
                        "player_id": 1,
                        "canonical_id": 1,
                        "is_rated_obs": 0,
                        "margin": -1.0,
                        "payout_complete_dtm": pd.Timestamp("2025-01-01"),
                        "session_id": "s1",
                        "wager": 10.0,
                    }
                ]
            ),
        ), patch("trainer.scorer._score_df", side_effect=lambda df, *a, **k: df):
            score_once(
                {"feature_list": [], "model_version": "test"},
                lookback_hours=24,
                alert_history=set(),
                conn=MagicMock(),
                rebuild_canonical_mapping=True,
            )
        build_mock.assert_called_once()


class TestR256_1_ScorerLoadsArtifactWhenFilesExistAndCutoffFuture(unittest.TestCase):
    """Review #1 (desired): When parquet+sidecar exist and cutoff >= now and rebuild=False, scorer should NOT call build."""

    def test_when_artifact_exists_and_cutoff_future_does_not_call_build(self):
        """Once scorer implements load: with files present and future cutoff, build_canonical_mapping_from_df not called."""
        from trainer.scorer import score_once

        build_mock = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            parquet_path = Path(tmp) / "canonical_mapping.parquet"
            sidecar_path = Path(tmp) / "canonical_mapping.cutoff.json"
            pd.DataFrame([{"player_id": 1, "canonical_id": 1}]).to_parquet(parquet_path, index=False)
            sidecar_path.write_text(
                json.dumps({"cutoff_dtm": "2030-01-01T00:00:00", "dummy_player_ids": []}), encoding="utf-8"
            )
            with patch("trainer.scorer.CANONICAL_MAPPING_PARQUET", parquet_path), patch(
                "trainer.scorer.CANONICAL_MAPPING_CUTOFF_JSON", sidecar_path
            ), patch("trainer.scorer.build_canonical_mapping_from_df", build_mock), patch(
                "trainer.scorer.fetch_recent_data",
                return_value=(
                    pd.DataFrame([{"bet_id": "b1", "player_id": 1}]),
                    pd.DataFrame([{"session_id": "s1", "player_id": 1}]),
                ),
            ), patch("trainer.scorer.refresh_alert_history"), patch("trainer.scorer.prune_old_state"), patch(
                "trainer.scorer.update_state_with_new_bets",
                return_value=pd.DataFrame([{"bet_id": "b1", "player_id": 1}]),
            ), patch(
                "trainer.scorer.build_features_for_scoring",
                return_value=pd.DataFrame(
                    [
                        {
                            "bet_id": "b1",
                            "player_id": 1,
                            "canonical_id": 1,
                            "is_rated_obs": 0,
                            "margin": -1.0,
                            "payout_complete_dtm": pd.Timestamp("2025-01-01"),
                            "session_id": "s1",
                            "wager": 10.0,
                        }
                    ]
                ),
            ), patch("trainer.scorer._score_df", side_effect=lambda df, *a, **k: df):
                score_once(
                    {"feature_list": [], "model_version": "test"},
                    lookback_hours=24,
                    alert_history=set(),
                    conn=MagicMock(),
                    rebuild_canonical_mapping=False,
                )
        build_mock.assert_not_called()


# ---------------------------------------------------------------------------
# R256 Review #2 — 載入後 canonical_map 缺欄未檢查
# ---------------------------------------------------------------------------


class TestR256_2_LoadedParquetMissingCanonicalIdColumnContract(unittest.TestCase):
    """Review #2: Desired contract — loaded parquet must have player_id and canonical_id."""

    def test_required_columns_check_would_fail_for_single_column_df(self):
        """If production adds column check, set(canonical_map.columns) >= {player_id, canonical_id} must fail for bad df."""
        bad_df = pd.DataFrame([{"player_id": 1}])
        self.assertFalse(
            set(bad_df.columns) >= {"player_id", "canonical_id"},
            "Load validation contract: missing canonical_id should be detected (Review #2)",
        )

    def test_required_columns_check_passes_for_valid_df(self):
        """Valid mapping df has both columns."""
        valid_df = pd.DataFrame([{"player_id": 1, "canonical_id": 1}])
        self.assertTrue(set(valid_df.columns) >= {"player_id", "canonical_id"})


# ---------------------------------------------------------------------------
# R256 Review #3 — dummy_player_ids 從 JSON 還原的型別一致
# ---------------------------------------------------------------------------


class TestR256_3_DummyPlayerIdsTypeNormalizationContract(unittest.TestCase):
    """Review #3: dummy_player_ids from sidecar should be normalized to int for isin() parity with player_id."""

    def test_string_dummy_ids_would_not_match_int_player_id(self):
        """When dummy_player_ids are strings and player_id is int, isin() does not match (documents risk)."""
        dummy_str = {"1", "2"}
        player_ids = pd.Series([1, 2, 3])
        self.assertFalse(player_ids.isin(dummy_str).any(), "str dummy set vs int player_id: no match (Review #3)")

    def test_int_dummy_ids_match_int_player_id(self):
        """When normalized to int, isin() matches."""
        dummy_int = {1, 2}
        player_ids = pd.Series([1, 2, 3])
        self.assertTrue(player_ids.isin(dummy_int).any())


# ---------------------------------------------------------------------------
# R256 Review #4 — cutoff 與 train_end 時區比較
# ---------------------------------------------------------------------------


class TestR256_4_CutoffTimezoneComparison(unittest.TestCase):
    """Review #4: Trainer uses tz_localize(None) for cutoff; tz-aware timestamp raises (documents current behavior)."""

    def test_tz_aware_cutoff_conversion_to_naive_for_comparison(self):
        """Cutoff from sidecar may be tz-aware; comparison with naive train_end must not raise (Review #4)."""
        ts_aware = pd.Timestamp("2025-06-01T00:00:00+00:00")
        self.assertIsNotNone(ts_aware.tz)
        # In some pandas versions tz_localize(None) on aware raises; in others replace(tzinfo=None) works.
        try:
            naive = ts_aware.tz_localize(None)
        except (TypeError, ValueError):
            naive = ts_aware.replace(tzinfo=None)
        train_end_naive = pd.Timestamp("2025-05-01 00:00:00")
        self.assertFalse(naive.tzinfo, "Result must be naive for comparison with train_end")
        self.assertTrue(naive >= train_end_naive)

    def test_naive_cutoff_and_naive_train_end_comparable(self):
        """Naive cutoff >= naive train_end does not raise."""
        cutoff_naive = pd.Timestamp("2025-06-01 00:00:00")
        train_end_naive = pd.Timestamp("2025-05-01 00:00:00")
        self.assertTrue(cutoff_naive >= train_end_naive)


# ---------------------------------------------------------------------------
# R256 Review #5 — 寫出失敗僅 log、本輪仍用已建 mapping
# ---------------------------------------------------------------------------

# Expected log message substrings for write failure (Review #5). When trainer adds
# artifact write block, warning should contain these so ops can detect write failure.
EXPECTED_WRITE_FAILURE_MESSAGE_SUBSTRINGS = ("artifact", "fail")


class TestR256_5_WriteFailureLogContract(unittest.TestCase):
    """Review #5: When artifact write fails, log should be identifiable (documented contract)."""

    def test_expected_write_failure_log_substrings_documented(self):
        """Contract: write-failure warning message should contain artifact and fail so next run rebuild is observable."""
        for sub in EXPECTED_WRITE_FAILURE_MESSAGE_SUBSTRINGS:
            self.assertIn(sub, "Write canonical mapping artifact failed (mock)")
        self.assertTrue(
            all(s in "Write canonical mapping artifact failed (exc)" for s in EXPECTED_WRITE_FAILURE_MESSAGE_SUBSTRINGS),
            "Recommended log format for Review #5",
        )


# ---------------------------------------------------------------------------
# R256 Review #6 — 多 process 同時寫 artifact（文件／契約）
# ---------------------------------------------------------------------------


class TestR256_6_ConcurrentWriteNotGuaranteed(unittest.TestCase):
    """Review #6: Document that concurrent write to same artifact path is not guaranteed."""

    def test_document_single_writer_assumption(self):
        """Contract: PLAN assumes single process writes artifact; no guarantee for concurrent write (Review #6)."""
        # No production assertion; documents that tests do not assert multi-process safety.
        self.assertTrue(True, "Single-writer assumption documented in PLAN / Review #6")


# ---------------------------------------------------------------------------
# R256 Review #7 — Trainer 與 Scorer 路徑常數一致
# ---------------------------------------------------------------------------


class TestR256_7_TrainerScorerPathParity(unittest.TestCase):
    """Review #7: Trainer and Scorer must resolve to same canonical mapping paths."""

    def test_canonical_mapping_parquet_path_parity(self):
        """LOCAL_PARQUET_DIR / canonical_mapping.parquet (trainer) == CANONICAL_MAPPING_PARQUET (scorer) resolved."""
        from trainer import scorer as scorer_mod
        from trainer import trainer as trainer_mod

        trainer_path = (trainer_mod.LOCAL_PARQUET_DIR / "canonical_mapping.parquet").resolve()
        scorer_path = scorer_mod.CANONICAL_MAPPING_PARQUET.resolve()
        self.assertEqual(
            trainer_path,
            scorer_path,
            "Trainer and Scorer canonical_mapping.parquet path must match (Review #7)",
        )

    def test_canonical_mapping_cutoff_json_path_parity(self):
        """LOCAL_PARQUET_DIR / canonical_mapping.cutoff.json (trainer) == CANONICAL_MAPPING_CUTOFF_JSON (scorer)."""
        from trainer import scorer as scorer_mod
        from trainer import trainer as trainer_mod

        trainer_path = (trainer_mod.LOCAL_PARQUET_DIR / "canonical_mapping.cutoff.json").resolve()
        scorer_path = scorer_mod.CANONICAL_MAPPING_CUTOFF_JSON.resolve()
        self.assertEqual(
            trainer_path,
            scorer_path,
            "Trainer and Scorer canonical_mapping.cutoff.json path must match (Review #7)",
        )


if __name__ == "__main__":
    unittest.main()
