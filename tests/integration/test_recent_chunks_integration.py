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
    @patch("trainer.trainer.CANONICAL_MAPPING_PARQUET")
    @patch("trainer.trainer.CANONICAL_MAPPING_CUTOFF_JSON")
    @patch("trainer.trainer.build_canonical_links_and_dummy_from_duckdb")
    @patch("trainer.trainer.build_canonical_mapping_from_links")
    @patch("trainer.trainer.ensure_player_profile_ready")
    @patch("trainer.trainer.load_player_profile")
    @patch("trainer.trainer.process_chunk")
    @patch("trainer.trainer.train_single_rated_model")
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
        mock_build_from_links,
        mock_links_and_dummy,
        mock_cutoff_json,
        mock_parquet_path,
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

        # Step 3: no artifact on disk; DuckDB path returns empty links/dummy and empty map.
        mock_parquet_path.exists.return_value = False
        mock_cutoff_json.exists.return_value = False
        _empty_links = pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"])
        mock_links_and_dummy.return_value = (_empty_links, set())
        mock_build_from_links.return_value = pd.DataFrame(columns=["player_id", "canonical_id"])
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
            # Setup fake path: exists + is_file so OOM probe branch runs; patch
            # _oom_check_after_chunk1 so effective_frac < 1.0 and trainer reruns chunk 1.
            mock_path.return_value.stat.return_value.st_size = 1000
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.is_file.return_value = True

            mock_process_chunk.return_value = "fake_path.parquet"
            mock_train.return_value = ({"model": None, "threshold": 0.5, "features": []}, None, {})

            args = argparse.Namespace(
                start="2025-01-01",
                end="2025-06-01",
                days=None,
                use_local_parquet=True,
                force_recompute=False,
                skip_optuna=True,
                recent_chunks=2
            )
            with patch("trainer.trainer._oom_check_after_chunk1", return_value=0.5):
                run_pipeline(args)

        # 1. Assert canonical mapping used train_end from chunk split (DuckDB path).
        mock_links_and_dummy.assert_called_once()
        _call_args = mock_links_and_dummy.call_args[0]
        _train_end = pd.Timestamp(_call_args[1])
        self.assertGreaterEqual(_train_end, expected_effective_start, "train_end must be >= effective_start")
        self.assertLessEqual(_train_end, expected_effective_end, "train_end must be <= effective_end (max of train_chunks)")

        # 2. Assert ensure_player_profile_ready was called with effective window
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
            max_lookback_days=365,         # DEC-017: full horizon in normal mode
        )
        ensure_kwargs = mock_ensure_profile.call_args.kwargs
        passed_cmap = ensure_kwargs.get("canonical_map")
        self.assertIsInstance(passed_cmap, pd.DataFrame)
        self.assertListEqual(list(passed_cmap.columns), ["player_id", "canonical_id"])

        # 3. Assert load_player_profile was called with effective window
        mock_load_profile.assert_called_once()
        kwargs = mock_load_profile.call_args[1]
        call_args = mock_load_profile.call_args[0]
        self.assertEqual(call_args[0], expected_effective_start)
        self.assertEqual(call_args[1], expected_effective_end)
        self.assertEqual(kwargs.get("use_local_parquet"), True)

        # 4. Assert process_chunk was called: OOM probe (chunk 1) + rerun chunk 1 + chunk 2 when NEG_SAMPLE_FRAC_AUTO
        self.assertEqual(mock_process_chunk.call_count, 3,
                         "With recent_chunks=2 and OOM probe: probe(chunk[-2]) + rerun(chunk[-2]) + chunk[-1]")
        chunk_args_1 = mock_process_chunk.call_args_list[0][0][0]
        chunk_args_2 = mock_process_chunk.call_args_list[1][0][0]
        chunk_args_3 = mock_process_chunk.call_args_list[2][0][0]
        self.assertEqual(chunk_args_1, fake_chunks[-2])
        self.assertEqual(chunk_args_2, fake_chunks[-2])
        self.assertEqual(chunk_args_3, fake_chunks[-1])

if __name__ == "__main__":
    unittest.main()
