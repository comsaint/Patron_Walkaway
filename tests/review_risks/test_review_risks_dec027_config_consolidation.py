"""Code Review — Config 集中化（DEC-027）風險點轉成最小可重現測試。

STATUS.md「Code Review — Config 集中化（DEC-027）變更（2026-03-11）」：
將 Reviewer 列出的風險點轉為 tests-only 的最小可重現測試或 source 契約。
不修改 production code。預期行為尚未實作者使用 @unittest.expectedFailure。

對應風險：#1 min/max_gb 正數, #2 negative available_bytes, #3 canonical threads 型別,
#4 invalid stage, #5 ETL fallback 語義, #7 Step 7 temp_dir 路徑安全, #8 極端 MAX_GB（可選）。
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

import trainer.config as config
import trainer.etl_player_profile as etl_mod
import trainer.trainer as trainer_mod

_GIB = 1024**3

# Reuse minimal session parquet helper pattern from R253/R389
_REQUIRED_VIEW_COLS = [
    "session_id", "player_id", "casino_player_id",
    "lud_dtm", "session_start_dtm", "session_end_dtm",
    "is_manual", "is_deleted", "is_canceled", "num_games_with_wager",
    "turnover",
]


def _minimal_valid_session_parquet(path: Path, train_end: datetime) -> None:
    """Minimal session parquet so build_canonical_links_and_dummy_from_duckdb schema passes."""
    one = pd.DataFrame([{
        "session_id": "s1",
        "player_id": 10,
        "casino_player_id": " C1 ",
        "lud_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "session_start_dtm": pd.Timestamp("2025-01-15 11:00:00"),
        "session_end_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "is_manual": 0,
        "is_deleted": 0,
        "is_canceled": 0,
        "num_games_with_wager": 1,
        "turnover": 10.0,
    }])
    cols = [c for c in _REQUIRED_VIEW_COLS if c in one.columns]
    one[cols].to_parquet(path, index=False)


# ---------------------------------------------------------------------------
# Risk 1 — get_duckdb_memory_limit_bytes 未驗證 min_gb/max_gb > 0
# ---------------------------------------------------------------------------

class TestDEC027_R1_MinGbZeroOrNegative(unittest.TestCase):
    """Review #1: When MIN_GB is 0 or negative, helper must not return 0 or negative (DuckDB undefined)."""

    def test_get_duckdb_memory_limit_bytes_min_gb_zero_returns_positive(self):
        """Patch DUCKDB_MEMORY_LIMIT_MIN_GB=0; get_duckdb_memory_limit_bytes(..., None) must return > 0."""
        with patch.object(config, "DUCKDB_MEMORY_LIMIT_MIN_GB", 0.0):
            result = config.get_duckdb_memory_limit_bytes("profile", None)
        self.assertGreater(result, 0, "DEC-027 Review #1: MIN_GB=0 must not yield 0 or negative bytes")

    def test_get_duckdb_memory_limit_bytes_min_gb_negative_returns_positive(self):
        """Patch DUCKDB_MEMORY_LIMIT_MIN_GB=-1; get_duckdb_memory_limit_bytes(..., None) must return > 0."""
        with patch.object(config, "DUCKDB_MEMORY_LIMIT_MIN_GB", -1.0):
            result = config.get_duckdb_memory_limit_bytes("profile", None)
        self.assertGreater(result, 0, "DEC-027 Review #1: MIN_GB=-1 must not yield negative bytes")


class TestDEC027_R1_MaxGbZeroOrNegative(unittest.TestCase):
    """Review #1: When MAX_GB is 0 or negative, result must remain in valid range (no 0/negative)."""

    def test_get_duckdb_memory_limit_bytes_max_gb_zero_returns_positive(self):
        """Patch PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB=0 with available_bytes set; return must be > 0 (after swap)."""
        with patch.object(config, "PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB", 0.0):
            result = config.get_duckdb_memory_limit_bytes("profile", 10 * _GIB)
        self.assertGreater(result, 0, "DEC-027 Review #1: MAX_GB=0 with available set must not yield 0")


# ---------------------------------------------------------------------------
# Risk 2 — available_bytes 負數時語義應與 None 一致或明確
# ---------------------------------------------------------------------------

class TestDEC027_R2_NegativeAvailableBytes(unittest.TestCase):
    """Review #2: Negative available_bytes should be treated like None (return _min) or raise/warn."""

    def test_get_duckdb_memory_limit_bytes_negative_available_equals_none_result(self):
        """get_duckdb_memory_limit_bytes("step7", -1) should equal get_duckdb_memory_limit_bytes("step7", None).

        Current behaviour: both return _min (no negative budget used). Locks consistency.
        """
        result_neg = config.get_duckdb_memory_limit_bytes("step7", -1)
        result_none = config.get_duckdb_memory_limit_bytes("step7", None)
        self.assertEqual(
            result_neg,
            result_none,
            "DEC-027 Review #2: negative available_bytes should behave like None (return _min)",
        )
        self.assertGreaterEqual(result_neg, 0, "Must not return negative bytes")


# ---------------------------------------------------------------------------
# Risk 3 — canonical_map threads 僅接受 int，與 Step 7 不一致
# ---------------------------------------------------------------------------

class TestDEC027_R3_CanonicalThreadsAcceptsNumericLikeStep7(unittest.TestCase):
    """Review #3: build_canonical_links_and_dummy_from_duckdb should accept float/str threads like Step 7."""

    def test_canonical_map_duckdb_threads_accepts_float_one(self):
        """With CANONICAL_MAP_DUCKDB_THREADS=1.0 (float), build_canonical_links must not raise ValueError."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_args = []

        def capture_execute(*args, **kwargs):
            call_args.append(args[0] if args else None)
            result = MagicMock()
            result.df.return_value = pd.DataFrame(
                columns=["player_id", "casino_player_id", "lud_dtm"]
            )
            return result

        mock_con.execute.side_effect = capture_execute
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.object(config, "CANONICAL_MAP_DUCKDB_THREADS", 1.0):
                with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))
        set_threads = [a for a in call_args if a and "SET threads" in str(a)]
        self.assertTrue(set_threads, "SET threads should be called")
        self.assertIn("1", set_threads[0], "DEC-027 Review #3: threads value should be 1")

    def test_canonical_map_duckdb_threads_accepts_str_one(self):
        """With CANONICAL_MAP_DUCKDB_THREADS='1' (str), build_canonical_links must not raise ValueError."""
        from trainer.trainer import build_canonical_links_and_dummy_from_duckdb

        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_args = []

        def capture_execute(*args, **kwargs):
            call_args.append(args[0] if args else None)
            result = MagicMock()
            result.df.return_value = pd.DataFrame(
                columns=["player_id", "casino_player_id", "lud_dtm"]
            )
            return result

        mock_con.execute.side_effect = capture_execute
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.object(config, "CANONICAL_MAP_DUCKDB_THREADS", "1"):
                with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                    build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))
        set_threads = [a for a in call_args if a and "SET threads" in str(a)]
        self.assertTrue(set_threads, "SET threads should be called")


# ---------------------------------------------------------------------------
# Risk 4 — get_duckdb_memory_config 未拒絕未知 stage
# ---------------------------------------------------------------------------

class TestDEC027_R4_InvalidStageRaises(unittest.TestCase):
    """Review #4: get_duckdb_memory_config(invalid_stage) should raise ValueError."""

    def test_get_duckdb_memory_config_invalid_stage_raises(self):
        """get_duckdb_memory_config('step6') or '' must raise ValueError with message containing 'stage'."""
        with self.assertRaises(ValueError) as ctx:
            config.get_duckdb_memory_config("step6")
        self.assertIn("stage", str(ctx.exception).lower(), "Error message should mention stage")

    def test_get_duckdb_memory_config_empty_stage_raises(self):
        """get_duckdb_memory_config('') must raise ValueError."""
        with self.assertRaises(ValueError):
            config.get_duckdb_memory_config("")


# ---------------------------------------------------------------------------
# Risk 5 — ETL fallback 路徑未套用 RAM_MAX_FRACTION（鎖定目前 fallback 語義）
# ---------------------------------------------------------------------------

class TestDEC027_R5_EtlFallbackUsesFixedMaxOnly(unittest.TestCase):
    """Review #5: When helper is patched away, ETL fallback must use fixed max only (lock current behaviour)."""

    def test_etl_fallback_budget_without_helper_uses_fixed_max_only(self):
        """With get_duckdb_memory_limit_bytes patched away, ETL returns clamp(available*frac, min_gb, max_gb); profile max=8G."""
        with patch.object(config, "get_duckdb_memory_limit_bytes", None):
            result = etl_mod._compute_duckdb_memory_limit_bytes(50 * _GIB)
        # Profile fallback: max_gb = PROFILE_DUCKDB_MEMORY_LIMIT_MAX_GB = 8.0
        expected_max = 8 * _GIB
        self.assertLessEqual(
            result,
            expected_max,
            "DEC-027 Review #5: ETL fallback without helper must use fixed profile max 8G",
        )
        self.assertGreater(result, 0, "Fallback must return positive bytes")


# ---------------------------------------------------------------------------
# Risk 7 — Step 7 temp_directory 未限制在 DATA_DIR 下（source 契約）
# ---------------------------------------------------------------------------

class TestDEC027_R7_Step7TempDirGuardedUnderDataDir(unittest.TestCase):
    """Review #7: Step 7 temp_directory must be guarded so path outside DATA_DIR falls back to duckdb_tmp."""

    def test_configure_step7_temp_dir_checks_path_under_data_dir_in_source(self):
        """_configure_step7_duckdb_runtime must guard temp_dir: path outside DATA_DIR → use DATA_DIR/duckdb_tmp.

        Prevents DuckDB writing spill files to arbitrary paths. R213 already guards cleanup;
        configure path must also restrict (e.g. relative_to(DATA_DIR) or resolve under DATA_DIR).
        """
        src = inspect.getsource(trainer_mod.run_pipeline)
        idx_def = src.find("def _configure_step7_duckdb_runtime")
        self.assertGreater(idx_def, -1, "_configure_step7_duckdb_runtime not found")
        # Extract function body up to next top-level "def " (same indent as "def _configure")
        rest = src[idx_def:]
        end = rest.find("\n    def _", 1)  # next nested def
        if end == -1:
            end = rest.find("\n    def _is_duckdb_oom", 1)
        body = rest[: end if end > 0 else len(rest)]
        # Require explicit guard that path is under DATA_DIR (e.g. relative_to), not only single-quote check
        has_relative_to_guard = "relative_to" in body and "DATA_DIR" in body
        self.assertTrue(
            has_relative_to_guard,
            "DEC-027 Review #7: _configure_step7_duckdb_runtime must check temp_dir under DATA_DIR "
            "(e.g. relative_to(DATA_DIR.resolve())); currently only single-quote is guarded.",
        )


# ---------------------------------------------------------------------------
# Risk 8 — 極端 MAX_GB 可選上限（可選）
# ---------------------------------------------------------------------------

class TestDEC027_R8_ExtremeMaxGbOptional(unittest.TestCase):
    """Review #8 (optional): Extreme MAX_GB should be capped or warned."""

    def test_get_duckdb_memory_limit_bytes_extreme_max_gb_capped_or_warned(self):
        """When DUCKDB_MEMORY_LIMIT_MAX_GB=1e6, result should be <= 1 TB or code should warn."""
        with patch.object(config, "DUCKDB_MEMORY_LIMIT_MAX_GB", 1e6):
            result = config.get_duckdb_memory_limit_bytes("step7", 2**62)
        one_tb = 1024 * _GIB
        self.assertLessEqual(
            result,
            one_tb,
            "DEC-027 Review #8 (optional): extreme MAX_GB should be capped to e.g. 1 TB",
        )


if __name__ == "__main__":
    unittest.main()
