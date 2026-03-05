import argparse
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch, ANY

import pandas as pd
from zoneinfo import ZoneInfo

# Import run_pipeline and its helpers from trainer
from trainer.trainer import run_pipeline

HK_TZ = ZoneInfo("Asia/Hong_Kong")


class TestRecentChunksIntegration(unittest.TestCase):
    @patch("trainer.trainer.load_local_parquet")
    @patch("trainer.trainer.apply_dq")
    @patch("trainer.trainer.build_canonical_mapping_from_df")
    @patch("trainer.trainer.ensure_player_profile_daily_ready")
    @patch("trainer.trainer.load_player_profile_daily")
    @patch("trainer.trainer.process_chunk")
    @patch("trainer.trainer.train_dual_model")
    @patch("trainer.trainer.save_artifact_bundle")
    @patch("trainer.trainer.get_monthly_chunks")
    def test_recent_chunks_propagates_effective_window(
        self,
        mock_get_chunks,
        mock_save_bundle,
        mock_train,
        mock_process_chunk,
        mock_load_profile,
        mock_ensure_profile,
        mock_build_canonical,
        mock_apply_dq,
        mock_load_local
    ):
        # Setup fake chunks. Let's say we have 5 months of data originally.
        base_time = datetime(2025, 1, 1, tzinfo=HK_TZ)
        fake_chunks = []
        for i in range(5):
            start = base_time + timedelta(days=30*i)
            end = base_time + timedelta(days=30*(i+1))
            ext_end = end + timedelta(days=1)
            fake_chunks.append({
                "window_start": start,
                "window_end": end,
                "extended_end": ext_end
            })
        
        mock_get_chunks.return_value = fake_chunks

        # We will request only the recent 2 chunks.
        # So effective_start should be chunk index -2 start
        # effective_end should be chunk index -1 end
        # DEC-018: run_pipeline strips tz from effective_start/end before passing
        # them to downstream helpers.  Use tz-naive expected values for comparisons.
        expected_effective_start = fake_chunks[-2]["window_start"].replace(tzinfo=None)
        expected_effective_end   = fake_chunks[-1]["window_end"].replace(tzinfo=None)

        # Setup mock returns to let pipeline flow through
        mock_load_local.return_value = (pd.DataFrame(), pd.DataFrame())
        mock_apply_dq.return_value = (pd.DataFrame(), pd.DataFrame())
        mock_build_canonical.return_value = pd.DataFrame(columns=["player_id", "canonical_id"])
        mock_load_profile.return_value = pd.DataFrame()
        
        # process_chunk should return a fake path so concat doesn't fail immediately
        # Actually, let's mock pd.read_parquet and Path so we don't hit disk
        with patch("trainer.trainer.pd.read_parquet") as mock_read_parquet, \
             patch("trainer.trainer.Path") as mock_path:
             
            # Setup fake parquet read
            mock_read_parquet.return_value = pd.DataFrame({
                "payout_complete_dtm": [datetime(2025, 5, 15, tzinfo=HK_TZ)],
                "label": [1],
                "is_rated": [True]
            })
            # Setup fake path stat
            mock_path.return_value.stat.return_value.st_size = 1000
            
            mock_process_chunk.return_value = "fake_path.parquet"
            mock_train.return_value = ({"model": None, "threshold": 0.5, "features": []}, None, {})

            # Run pipeline with args
            args = argparse.Namespace(
                start="2025-01-01",
                end="2025-06-01",
                days=None,
                use_local_parquet=True,
                force_recompute=False,
                skip_optuna=True,
                recent_chunks=2
            )
            
            run_pipeline(args)

        # 1. Assert load_local_parquet was called with effective window for mapping
        mock_load_local.assert_called_once()
        call_args = mock_load_local.call_args[0]
        self.assertEqual(call_args[0], expected_effective_start)
        self.assertEqual(call_args[1], expected_effective_end + timedelta(days=1))

        # 2. Assert ensure_player_profile_daily_ready was called with effective window
        # (canonical_id_whitelist=None, snapshot_interval_days=1, preload_sessions=True
        # are the normal-mode defaults; canonical_map matched via ANY since its content
        # depends on build_canonical_mapping_from_df mock output — DEC-017 bug fix)
        mock_ensure_profile.assert_called_once_with(
            expected_effective_start,
            expected_effective_end,
            use_local_parquet=True,
            canonical_id_whitelist=None,
            snapshot_interval_days=1,
            preload_sessions=True,
            canonical_map=ANY,
            fast_mode=False,               # DEC-017: non-fast-mode default
            max_lookback_days=365,         # DEC-017: full horizon in normal mode
            use_month_end_snapshots=True,  # DEC-019: default when flag not passed
        )
        ensure_kwargs = mock_ensure_profile.call_args.kwargs
        passed_cmap = ensure_kwargs.get("canonical_map")
        self.assertIsInstance(passed_cmap, pd.DataFrame)
        self.assertListEqual(list(passed_cmap.columns), ["player_id", "canonical_id"])

        # 3. Assert load_player_profile_daily was called with effective window
        mock_load_profile.assert_called_once()
        kwargs = mock_load_profile.call_args[1]
        call_args = mock_load_profile.call_args[0]
        self.assertEqual(call_args[0], expected_effective_start)
        self.assertEqual(call_args[1], expected_effective_end)
        self.assertEqual(kwargs.get("use_local_parquet"), True)

        # 4. Assert process_chunk was only called for the 2 recent chunks
        self.assertEqual(mock_process_chunk.call_count, 2)
        chunk_args_1 = mock_process_chunk.call_args_list[0][0][0]
        chunk_args_2 = mock_process_chunk.call_args_list[1][0][0]
        self.assertEqual(chunk_args_1, fake_chunks[-2])
        self.assertEqual(chunk_args_2, fake_chunks[-1])

if __name__ == "__main__":
    unittest.main()
