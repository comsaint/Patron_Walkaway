"""Code Review — training-config-recommender 風險點轉成最小可重現測試.

STATUS.md「Code Review：training-config-recommender（2026-03-11）」：
將 Reviewer 列出的風險點轉為 tests-only 的最小可重現測試。
不修改 production code。預期行為尚未實作者使用 @unittest.expectedFailure。

對應風險：#1 ClickHouse 負 training_days → 負 total_chunk_bytes_estimate,
#2 CLI --days 0/負數未驗證, #3 TRAINING_AVAILABLE_RAM_GB=0,
#4 chunk_dir 不存在／為檔案時 fallback, #5 Parquet discovery 遇 OSError 未防護。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import trainer.training_config_recommender as rec


# ---------------------------------------------------------------------------
# Risk 1 — ClickHouse 負 training_days 導致負的 total_chunk_bytes_estimate
# ---------------------------------------------------------------------------

class TestRecommender_R1_ClickHouseNegativeDays(unittest.TestCase):
    """Review #1: build_data_profile_clickhouse(training_days<0) with CH data must yield total_chunk_bytes_estimate >= 0."""

    def test_negative_training_days_with_mock_ch_yields_non_negative_estimate(self):
        """Contract: when CH returns total_bytes and training_days<0, total_chunk_bytes_estimate must be >= 0. xfail until production clamps frac."""
        # Mock client: query returns result_set with t_bet + t_session rows so total_bytes > 0; frac = min(1, -30/365) < 0 → negative estimate in current code.
        mock_result = MagicMock()
        mock_result.result_set = [
            ("t_bet", 1_000_000_000, 10_000),
            ("t_session", 100_000_000, 5_000),
        ]
        mock_client_instance = MagicMock()
        mock_client_instance.query.return_value = mock_result

        def get_client():
            return mock_client_instance

        profile = rec.build_data_profile_clickhouse(
            training_days=-30,
            get_client=get_client,
            skip_ch_connect=False,
        )
        self.assertGreaterEqual(
            profile["total_chunk_bytes_estimate"],
            0,
            "total_chunk_bytes_estimate must be >= 0 when training_days < 0 (frac should be clamped)",
        )
        self.assertGreaterEqual(profile["chunk_count"], 1)

    def test_zero_training_days_skip_ch_connect_fallback_non_negative(self):
        """When skip_ch_connect=True and training_days=0, fallback path yields non-negative estimate (no CH frac used)."""
        profile = rec.build_data_profile_clickhouse(
            training_days=0,
            skip_ch_connect=True,
            estimated_bytes_per_chunk=100,
        )
        self.assertGreaterEqual(profile["total_chunk_bytes_estimate"], 0)
        self.assertGreaterEqual(profile["chunk_count"], 1)


# ---------------------------------------------------------------------------
# Risk 2 — CLI --days 0 或負數未驗證
# ---------------------------------------------------------------------------

class TestRecommender_R2_CLIDaysValidation(unittest.TestCase):
    """Review #2: CLI should reject --days 0 or --days < 0 (non-zero exit or error message)."""

    def test_cli_days_zero_exits_non_zero(self):
        """Contract: --data-source parquet --days 0 should result in non-zero exit. xfail until CLI validates --days >= 1."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "trainer.scripts.recommend_training_config",
                "--data-source",
                "parquet",
                "--days",
                "0",
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0, "CLI should reject --days 0 with non-zero exit")

    def test_cli_days_negative_exits_non_zero(self):
        """Contract: --days -1 should result in non-zero exit. xfail until CLI validates --days."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "trainer.scripts.recommend_training_config",
                "--data-source",
                "parquet",
                "--days",
                "-1",
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0, "CLI should reject --days -1 with non-zero exit")


# ---------------------------------------------------------------------------
# Risk 3 — TRAINING_AVAILABLE_RAM_GB=0 時 get_system_resources / suggest_config
# ---------------------------------------------------------------------------

class TestRecommender_R3_ZeroRamEnv(unittest.TestCase):
    """Review #3: get_system_resources with env TRAINING_AVAILABLE_RAM_GB=0; suggest_config with 0 RAM must not crash."""

    @patch("trainer.training_config_recommender.os.getenv")
    def test_get_system_resources_env_zero_returns_zero_ram(self, mock_getenv):
        """When TRAINING_AVAILABLE_RAM_GB=0, get_system_resources returns ram_available_gb == 0.0."""
        def getenv(k, default=None):
            if k == "TRAINING_AVAILABLE_RAM_GB":
                return "0"
            return os.environ.get(k, default)

        mock_getenv.side_effect = getenv
        resources = rec.get_system_resources()
        self.assertEqual(resources.get("ram_available_gb"), 0.0)

    def test_suggest_config_zero_ram_does_not_raise_returns_suggestions(self):
        """suggest_config(..., ram_available_gb: 0) must not raise and must return at least one suggestion."""
        profile: rec.DataProfile = {
            "data_source": "parquet",
            "training_days": 30,
            "chunk_count": 1,
            "total_chunk_bytes_estimate": 200_000_000,
            "session_data_bytes": 0,
            "has_existing_chunks": True,
        }
        resources = {"ram_available_gb": 0.0, "ram_total_gb": 0.0, "cpu_count": 1, "disk_available_gb": 50.0}
        estimates = {
            "step7_peak_ram_gb": 2.0,
            "step8_peak_ram_gb": 1.0,
            "step9_peak_ram_gb": 1.0,
        }
        suggestions = rec.suggest_config(profile, resources, estimates)
        self.assertIsInstance(suggestions, list)
        self.assertGreaterEqual(len(suggestions), 1, "suggest_config should return at least one suggestion when RAM=0")


# ---------------------------------------------------------------------------
# Risk 4 — chunk_dir 不存在或為檔案時 Parquet profile fallback
# ---------------------------------------------------------------------------

class TestRecommender_R4_ParquetNonexistentOrFile(unittest.TestCase):
    """Review #4: build_data_profile_parquet with nonexistent dir or file path must use fallback and not crash."""

    def test_nonexistent_chunk_dir_returns_fallback_profile(self):
        """When chunk_dir does not exist, profile has has_existing_chunks=False, chunk_count>=1, total_chunk_bytes_estimate>0."""
        nonexistent = Path(__file__).resolve().parent / "nonexistent_chunk_dir_xyz_789"
        assert not nonexistent.exists(), "test path should not exist"
        profile = rec.build_data_profile_parquet(nonexistent, training_days=30)
        self.assertFalse(profile.get("has_existing_chunks", True))
        self.assertGreaterEqual(profile["chunk_count"], 1)
        self.assertGreater(profile["total_chunk_bytes_estimate"], 0)

    def test_chunk_dir_is_file_returns_fallback_no_crash(self):
        """When chunk_dir is an existing file (not dir), use fallback and do not crash."""
        file_path = Path(__file__).resolve()
        self.assertTrue(file_path.is_file(), "this file exists as a file")
        profile = rec.build_data_profile_parquet(file_path, training_days=30)
        self.assertFalse(profile.get("has_existing_chunks", True))
        self.assertGreaterEqual(profile["chunk_count"], 1)
        self.assertGreater(profile["total_chunk_bytes_estimate"], 0)


# ---------------------------------------------------------------------------
# Risk 5 — Parquet discovery 遇 OSError (e.g. permission) 未防護
# ---------------------------------------------------------------------------

class TestRecommender_R5_ParquetOSErrorResilience(unittest.TestCase):
    """Review #5: build_data_profile_parquet should not raise when glob/stat raises OSError; return reasonable fallback. xfail until production adds try/except."""

    def test_parquet_glob_raises_oserror_does_not_propagate(self):
        """When chunk_dir.glob('*.parquet') raises OSError, build_data_profile_parquet must not raise and must return profile with chunk_count>=1, total_chunk_bytes_estimate>=0."""
        # Use a path-like object (Path.glob is read-only on Windows, so we cannot patch.object it).
        class PathLikeWithFailingGlob:
            def is_dir(self):
                return True

            def glob(self, pattern):
                raise OSError(13, "Permission denied")

        chunk_dir = PathLikeWithFailingGlob()
        profile = rec.build_data_profile_parquet(chunk_dir, training_days=30)
        self.assertGreaterEqual(profile["chunk_count"], 1)
        self.assertGreaterEqual(profile["total_chunk_bytes_estimate"], 0)


if __name__ == "__main__":
    unittest.main()
