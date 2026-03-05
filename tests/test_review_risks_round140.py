"""tests/test_review_risks_round140.py
======================================
Minimal reproducible guardrail tests for Round 34 review risks (R200-R207).

Tests-only: no production code changes.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import unittest
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
from zoneinfo import ZoneInfo

from trainer.trainer import ensure_player_profile_ready, run_pipeline

HK_TZ = ZoneInfo("Asia/Hong_Kong")


class TestR200SchemaHashHorizonGuardrail(unittest.TestCase):
    """R200: schema hash should differ when max_lookback_days differs."""

    def test_max_lookback_days_change_should_invalidate_existing_profile_cache(self):
        with TemporaryDirectory() as td:
            from pathlib import Path

            local_dir = Path(td)
            profile_path = local_dir / "player_profile.parquet"
            session_path = local_dir / "gmwds_t_session.parquet"
            sidecar_path = local_dir / "player_profile.schema_hash"

            profile_path.write_text("dummy", encoding="utf-8")
            session_path.write_text("dummy", encoding="utf-8")

            # Existing cache written under old/default horizon semantics.
            base_hash = "deadbeefdeadbeefdeadbeefdeadbeef"
            stored_hash = hashlib.md5((base_hash + "_full").encode()).hexdigest()
            sidecar_path.write_text(stored_hash, encoding="utf-8")

            with (
                patch("trainer.trainer.LOCAL_PARQUET_DIR", local_dir),
                patch("trainer.trainer.LOCAL_PROFILE_SCHEMA_HASH", sidecar_path),
                patch("trainer.trainer.compute_profile_schema_hash", return_value=base_hash),
                patch(
                    "trainer.trainer._parquet_date_range",
                    side_effect=[
                        # session range
                        (datetime(2025, 1, 1).date(), datetime(2025, 1, 31).date()),
                        # existing profile range
                        (datetime(2025, 1, 1).date(), datetime(2025, 1, 31).date()),
                    ],
                ),
            ):
                ensure_player_profile_ready(
                    window_start=datetime(2025, 1, 10, tzinfo=HK_TZ),
                    window_end=datetime(2025, 1, 20, tzinfo=HK_TZ),
                    use_local_parquet=True,
                    fast_mode=True,
                    max_lookback_days=30,
                )

            # Desired behavior: horizon changed (old cache hash without horizon tag),
            # so cache must be invalidated (deleted) before continuing.
            self.assertFalse(
                profile_path.exists(),
                "Profile cache should be invalidated when max_lookback_days changes",
            )


class TestR202FastModeHelpTextGuardrail(unittest.TestCase):
    """R202: CLI help text should describe DEC-017, not deprecated DEC-015 wording."""

    def test_fast_mode_help_mentions_dec017_not_dec015(self):
        import trainer.trainer as trainer_mod

        src = inspect.getsource(trainer_mod.main)
        self.assertIn("DEC-017", src)
        self.assertNotIn("DEC-015 Option B", src)


class TestR203HorizonZeroWarningGuardrail(unittest.TestCase):
    """R203: fast-mode horizon=0 should emit a warning for operator visibility."""

    @patch("trainer.trainer.get_monthly_chunks")
    @patch("trainer.trainer.get_train_valid_test_split")
    @patch("trainer.trainer.load_local_parquet")
    @patch("trainer.trainer.apply_dq")
    @patch("trainer.trainer.build_canonical_mapping_from_df")
    @patch("trainer.trainer.ensure_player_profile_ready")
    @patch("trainer.trainer.load_player_profile")
    @patch("trainer.trainer.process_chunk")
    @patch("trainer.trainer.train_dual_model")
    @patch("trainer.trainer.save_artifact_bundle")
    @patch("trainer.trainer.logger.warning")
    def test_fast_mode_zero_horizon_should_warn(
        self,
        mock_warn,
        mock_save_bundle,
        mock_train,
        mock_process_chunk,
        mock_load_profile,
        _mock_ensure_profile,
        mock_build_canonical,
        mock_apply_dq,
        mock_load_local,
        mock_split,
        mock_get_chunks,
    ):
        # window_start == window_end -> data_horizon_days = 0
        t0 = datetime(2025, 5, 1, tzinfo=HK_TZ)
        fake_chunk = {"window_start": t0, "window_end": t0, "extended_end": t0}
        mock_get_chunks.return_value = [fake_chunk]
        mock_split.return_value = {"train_chunks": [fake_chunk], "valid_chunks": [], "test_chunks": []}

        mock_load_local.return_value = (pd.DataFrame(), pd.DataFrame())
        mock_apply_dq.return_value = (pd.DataFrame(), pd.DataFrame())
        mock_build_canonical.return_value = pd.DataFrame(
            {"player_id": [1], "canonical_id": ["A"]}
        )
        mock_load_profile.return_value = pd.DataFrame()
        mock_process_chunk.return_value = "fake_path.parquet"
        mock_train.return_value = ({"model": None, "threshold": 0.5, "features": []}, None, {})

        with (
            patch("trainer.trainer.pd.read_parquet", return_value=pd.DataFrame(
                {
                    "payout_complete_dtm": [datetime(2025, 5, 1, tzinfo=HK_TZ)],
                    "label": [1],
                    "is_rated": [True],
                }
            )),
            patch("trainer.trainer.Path") as mock_path,
        ):
            mock_path.return_value.stat.return_value.st_size = 1000
            args = argparse.Namespace(
                start="2025-05-01",
                end="2025-05-01",
                days=None,
                use_local_parquet=True,
                force_recompute=False,
                skip_optuna=True,
                fast_mode=True,
                recent_chunks=1,
            )
            run_pipeline(args)

        warning_msgs = [" ".join(map(str, call.args)) for call in mock_warn.call_args_list]
        self.assertTrue(
            any("data_horizon_days" in msg for msg in warning_msgs),
            "Expected a warning mentioning data_horizon_days when horizon is 0",
        )


class TestR205SampleRatedOrthogonalityGuardrail(unittest.TestCase):
    """R205: fast-mode should not implicitly sample rated IDs without --sample-rated."""

    @patch("trainer.trainer.get_monthly_chunks")
    @patch("trainer.trainer.get_train_valid_test_split")
    @patch("trainer.trainer.load_local_parquet")
    @patch("trainer.trainer.apply_dq")
    @patch("trainer.trainer.build_canonical_mapping_from_df")
    @patch("trainer.trainer.ensure_player_profile_ready")
    @patch("trainer.trainer.load_player_profile")
    @patch("trainer.trainer.process_chunk")
    @patch("trainer.trainer.train_dual_model")
    @patch("trainer.trainer.save_artifact_bundle")
    def test_fast_mode_without_sample_flag_should_keep_whitelist_none(
        self,
        _mock_save_bundle,
        mock_train,
        mock_process_chunk,
        mock_load_profile,
        mock_ensure_profile,
        mock_build_canonical,
        mock_apply_dq,
        mock_load_local,
        mock_split,
        mock_get_chunks,
    ):
        t0 = datetime(2025, 4, 1, tzinfo=HK_TZ)
        t1 = datetime(2025, 5, 1, tzinfo=HK_TZ)
        fake_chunk = {"window_start": t0, "window_end": t1, "extended_end": t1}
        mock_get_chunks.return_value = [fake_chunk]
        mock_split.return_value = {"train_chunks": [fake_chunk], "valid_chunks": [], "test_chunks": []}

        mock_load_local.return_value = (pd.DataFrame(), pd.DataFrame())
        mock_apply_dq.return_value = (pd.DataFrame(), pd.DataFrame())
        mock_build_canonical.return_value = pd.DataFrame(
            {
                "player_id": [1, 2, 3],
                "canonical_id": ["A", "B", "C"],
            }
        )
        mock_load_profile.return_value = pd.DataFrame()
        mock_process_chunk.return_value = "fake_path.parquet"
        mock_train.return_value = ({"model": None, "threshold": 0.5, "features": []}, None, {})

        with (
            patch("trainer.trainer.pd.read_parquet", return_value=pd.DataFrame(
                {
                    "payout_complete_dtm": [datetime(2025, 4, 15, tzinfo=HK_TZ)],
                    "label": [1],
                    "is_rated": [True],
                }
            )),
            patch("trainer.trainer.Path") as mock_path,
        ):
            mock_path.return_value.stat.return_value.st_size = 1000
            args = argparse.Namespace(
                start="2025-04-01",
                end="2025-05-01",
                days=None,
                use_local_parquet=True,
                force_recompute=False,
                skip_optuna=True,
                fast_mode=True,
                recent_chunks=1,
            )
            run_pipeline(args)

        kwargs = mock_ensure_profile.call_args.kwargs
        self.assertIsNone(
            kwargs.get("canonical_id_whitelist"),
            "Without --sample-rated, fast-mode should not implicitly sample rated IDs",
        )


class TestR207FeatureTrackClassificationGuardrail(unittest.TestCase):
    """R207: save_artifact_bundle should classify profile subset as track='profile'."""

    def test_feature_list_track_classification_for_profile_subset(self):
        from pathlib import Path
        import json
        import trainer.trainer as trainer_mod

        feature_cols = ["loss_streak", "days_since_last_session", "sessions_30d"]

        with TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod.save_artifact_bundle(
                    rated=None,
                    nonrated=None,
                    feature_cols=feature_cols,
                    combined_metrics={},
                    model_version="test-v1",
                    fast_mode=True,
                )

            feature_list = json.loads((model_dir / "feature_list.json").read_text(encoding="utf-8"))
            track_by_name = {item["name"]: item["track"] for item in feature_list}
            self.assertEqual(track_by_name["loss_streak"], "B")
            self.assertEqual(track_by_name["days_since_last_session"], "profile")
            self.assertEqual(track_by_name["sessions_30d"], "profile")


class TestR118NoPreloadWithoutFastModeWarning(unittest.TestCase):
    """R118: --fast-mode-no-preload without --fast-mode should warn as no-op."""

    @patch("trainer.trainer.load_local_parquet")
    @patch("trainer.trainer.apply_dq")
    @patch("trainer.trainer.build_canonical_mapping_from_df")
    @patch("trainer.trainer.ensure_player_profile_ready")
    @patch("trainer.trainer.load_player_profile")
    @patch("trainer.trainer.process_chunk")
    @patch("trainer.trainer.train_dual_model")
    @patch("trainer.trainer.save_artifact_bundle")
    @patch("trainer.trainer.get_monthly_chunks")
    @patch("trainer.trainer.logger.warning")
    def test_no_preload_without_fast_mode_logs_warning(
        self,
        mock_warn,
        mock_get_chunks,
        mock_save_bundle,
        mock_train,
        mock_process_chunk,
        mock_load_profile,
        mock_ensure_profile,
        mock_build_canonical,
        mock_apply_dq,
        mock_load_local,
    ):
        base = datetime(2025, 1, 1, tzinfo=HK_TZ)
        fake_chunks = [
            {
                "window_start": base,
                "window_end": base + timedelta(days=30),
                "extended_end": base + timedelta(days=31),
            }
        ]
        mock_get_chunks.return_value = fake_chunks
        mock_load_local.return_value = (pd.DataFrame(), pd.DataFrame())
        mock_apply_dq.return_value = (pd.DataFrame(), pd.DataFrame())
        mock_build_canonical.return_value = pd.DataFrame(columns=["player_id", "canonical_id"])
        mock_load_profile.return_value = pd.DataFrame()
        mock_process_chunk.return_value = "fake_path.parquet"
        mock_train.return_value = ({"model": None, "threshold": 0.5, "features": []}, None, {})

        with patch("trainer.trainer.pd.read_parquet") as mock_read_parquet, patch(
            "trainer.trainer.Path"
        ) as mock_path:
            mock_read_parquet.return_value = pd.DataFrame(
                {
                    "payout_complete_dtm": [datetime(2025, 1, 15, tzinfo=HK_TZ)],
                    "label": [1],
                    "is_rated": [True],
                }
            )
            mock_path.return_value.stat.return_value.st_size = 1000

            args = argparse.Namespace(
                start="2025-01-01",
                end="2025-02-01",
                days=None,
                use_local_parquet=True,
                force_recompute=False,
                skip_optuna=True,
                recent_chunks=None,
                fast_mode=False,
                fast_mode_no_preload=True,
            )
            run_pipeline(args)

        warning_msgs = [str(call.args[0]) for call in mock_warn.call_args_list if call.args]
        self.assertTrue(
            any("no effect without --fast-mode" in m for m in warning_msgs),
            "R118: expected warning when --fast-mode-no-preload is used without --fast-mode",
        )


if __name__ == "__main__":
    unittest.main()

