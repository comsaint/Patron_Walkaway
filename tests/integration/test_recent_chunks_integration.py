"""Integration: run_pipeline 單視窗路徑（get_single_window_chunk）與 profile 視窗 wiring.

舊版 ``--recent-chunks`` / monthly trim 已移除；本檔保留檔名以降低 CI 清單變更，
但僅驗證單一視窗 mock 下 ensure_player_profile / process_chunk 契約。
"""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, patch

import pandas as pd
from zoneinfo import ZoneInfo

from trainer.trainer import run_pipeline

HK_TZ = ZoneInfo("Asia/Hong_Kong")


class TestSingleWindowProfileWiring(unittest.TestCase):
    @patch("trainer.trainer.CANONICAL_MAPPING_PARQUET")
    @patch("trainer.trainer.CANONICAL_MAPPING_CUTOFF_JSON")
    @patch("trainer.trainer.build_canonical_links_and_dummy_from_duckdb")
    @patch("trainer.trainer.build_canonical_mapping_from_links")
    @patch("trainer.trainer.ensure_player_profile_ready")
    @patch("trainer.trainer.load_player_profile")
    @patch("trainer.trainer.process_chunk")
    @patch("trainer.trainer.train_single_rated_model")
    @patch("trainer.trainer.save_artifact_bundle")
    @patch("trainer.trainer.get_single_window_chunk")
    def test_single_window_propagates_effective_window(
        self,
        mock_get_window,
        mock_save_bundle,
        mock_train,
        mock_process_chunk,
        mock_load_profile,
        mock_ensure_profile,
        mock_build_from_links,
        mock_links_and_dummy,
        mock_cutoff_json,
        mock_parquet_path,
    ):
        base_time = datetime(2025, 1, 1, tzinfo=HK_TZ)
        end_time = base_time + timedelta(days=60)
        fake_chunks = [
            {
                "window_start": base_time,
                "window_end": end_time,
                "extended_end": end_time + timedelta(days=1),
            }
        ]
        mock_get_window.return_value = fake_chunks

        expected_effective_start = fake_chunks[0]["window_start"].replace(tzinfo=None)
        expected_effective_end = fake_chunks[0]["window_end"].replace(tzinfo=None)

        mock_parquet_path.exists.return_value = False
        mock_cutoff_json.exists.return_value = False
        mock_links_and_dummy.return_value = (
            pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"]),
            set(),
        )
        mock_build_from_links.return_value = pd.DataFrame(columns=["player_id", "canonical_id"])
        mock_load_profile.return_value = pd.DataFrame()

        _fd, _tmp_parquet = tempfile.mkstemp(suffix=".parquet")
        os.close(_fd)
        try:
            with patch(
                "trainer.training.cross_entry_preflight.run_cross_entry_data_preflight"
            ), patch("trainer.trainer.STEP7_USE_DUCKDB", False), patch(
                "trainer.trainer.STEP7_KEEP_TRAIN_ON_DISK", False
            ), patch(
                "trainer.trainer.local_parquet_session_path_for_trainer",
                return_value=Path(_tmp_parquet),
            ), patch("trainer.trainer.pd.read_parquet") as mock_read_parquet:
                mock_read_parquet.return_value = pd.DataFrame(
                    {
                        "payout_complete_dtm": [datetime(2025, 2, 15, tzinfo=HK_TZ)],
                        "label": [1],
                        "is_rated": [True],
                        "canonical_id": ["C0"],
                        "bet_id": [1],
                    }
                )

                mock_process_chunk.return_value = _tmp_parquet
                mock_train.return_value = ({"model": None, "threshold": 0.5, "features": []}, None, {})

                args = argparse.Namespace(
                    start="2025-01-01",
                    end="2025-06-01",
                    days=None,
                    use_local_parquet=True,
                    force_recompute=False,
                    skip_optuna=True,
                    no_l2_auto_bundle=True,
                    l2_training_bundle=None,
                )
                run_pipeline(args)
        finally:
            try:
                os.unlink(_tmp_parquet)
            except OSError:
                pass

        mock_links_and_dummy.assert_called_once()
        _call_args = mock_links_and_dummy.call_args[0]
        _train_end = pd.Timestamp(_call_args[1])
        self.assertGreaterEqual(_train_end, expected_effective_start)
        self.assertLessEqual(_train_end, expected_effective_end)

        mock_ensure_profile.assert_called_once_with(
            expected_effective_start,
            expected_effective_end,
            use_local_parquet=True,
            canonical_id_whitelist=None,
            snapshot_interval_days=1,
            preload_sessions=True,
            canonical_map=ANY,
            max_lookback_days=365,
        )

        mock_load_profile.assert_called_once()
        call_args = mock_load_profile.call_args[0]
        self.assertEqual(call_args[0], expected_effective_start)
        self.assertEqual(call_args[1], expected_effective_end)

        self.assertEqual(mock_process_chunk.call_count, 1)
