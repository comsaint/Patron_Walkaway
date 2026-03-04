"""tests/test_review_risks_round120.py
======================================
Minimal reproducible guardrail tests for Review Round 28 finding (R118).

Tests-only: no production code changes.
"""

from __future__ import annotations

import argparse
import pathlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
from zoneinfo import ZoneInfo

from trainer.trainer import run_pipeline

HK_TZ = ZoneInfo("Asia/Hong_Kong")


class TestR118NoPreloadWithoutFastModeWarning(unittest.TestCase):
    """R118: --fast-mode-no-preload without --fast-mode should warn as no-op."""

    @patch("trainer.trainer.load_local_parquet")
    @patch("trainer.trainer.apply_dq")
    @patch("trainer.trainer.build_canonical_mapping_from_df")
    @patch("trainer.trainer.ensure_player_profile_daily_ready")
    @patch("trainer.trainer.load_player_profile_daily")
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
