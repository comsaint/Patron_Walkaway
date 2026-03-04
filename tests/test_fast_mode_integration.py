"""Integration tests for DEC-017 fast-mode and --sample-rated flag.

These tests exercise run_pipeline() with mocked heavy I/O so they run in
milliseconds without real Parquet files.  They verify the *wiring* between
CLI flags and the downstream helpers (ensure_player_profile_daily_ready,
load_player_profile_daily, process_chunk) rather than the computations
inside those helpers.

Covered scenarios
-----------------
1. FastModeHorizonPropagation  — fast_mode=True with recent_chunks=1:
   - snapshot_interval_days == FAST_MODE_SNAPSHOT_INTERVAL_DAYS (7)
   - max_lookback_days == data_horizon_days (not 365)
   - fast_mode=True forwarded to ensure_player_profile_daily_ready
   - ensure called with the trimmed effective window, not the original window

2. FastModeNoPreload  — fast_mode=True + fast_mode_no_preload=True:
   - preload_sessions=False forwarded to ensure_player_profile_daily_ready

3. SampleRatedWhitelist  — sample_rated=N with canonical_map containing M IDs:
   - canonical_id_whitelist has min(N, M) entries
   - fast_mode=False  (sample_rated is orthogonal to fast_mode)

4. FastModePlusSampleRated  — both flags together:
   - fast_mode semantics (horizon) AND whitelist both forwarded
"""

import argparse
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch, MagicMock, call, ANY

import pandas as pd
from zoneinfo import ZoneInfo

from trainer.trainer import run_pipeline, FAST_MODE_SNAPSHOT_INTERVAL_DAYS

HK_TZ = ZoneInfo("Asia/Hong_Kong")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_chunks(n: int, base: datetime | None = None):
    """Return n monthly fake chunks starting from *base*."""
    base = base or datetime(2025, 1, 1, tzinfo=HK_TZ)
    chunks = []
    for i in range(n):
        ws = base + timedelta(days=30 * i)
        we = base + timedelta(days=30 * (i + 1))
        chunks.append({"window_start": ws, "window_end": we, "extended_end": we + timedelta(days=1)})
    return chunks


def _canonical_map(n: int = 10) -> pd.DataFrame:
    """Return a minimal canonical_map DataFrame with *n* distinct canonical_ids."""
    return pd.DataFrame(
        {
            "player_id": list(range(n)),
            "canonical_id": [f"C{i:03d}" for i in range(n)],
        }
    )


def _fake_chunk_df(ts: datetime) -> pd.DataFrame:
    """Minimal DataFrame that process_chunk / concat step needs."""
    return pd.DataFrame(
        {
            "payout_complete_dtm": [ts],
            "label": [0],
            "is_rated": [True],
            "canonical_id": ["C000"],
            "run_id": [1],
        }
    )


class _PipelineMixin:
    """Common patching context for run_pipeline integration tests."""

    # Subclasses may override these to inject specific return values.
    _fake_chunks: list | None = None
    _canonical_map_df: pd.DataFrame | None = None
    _sample_rated: int | None = None
    _fast_mode: bool = False
    _no_preload: bool = False
    _recent_chunks: int | None = None
    _extra_args: dict | None = None

    def _run_pipeline_with_mocks(self) -> dict:
        """Patch all heavy I/O, run run_pipeline, return call-arg dict."""
        chunks = self._fake_chunks or _make_chunks(3)
        cmap = self._canonical_map_df if self._canonical_map_df is not None else _canonical_map()
        fake_df = _fake_chunk_df(chunks[-1]["window_start"])

        patches = {
            "get_monthly_chunks": patch("trainer.trainer.get_monthly_chunks", return_value=chunks),
            "load_local_parquet": patch("trainer.trainer.load_local_parquet", return_value=(pd.DataFrame(), pd.DataFrame())),
            "apply_dq": patch("trainer.trainer.apply_dq", return_value=(pd.DataFrame(), pd.DataFrame())),
            "build_canonical_mapping_from_df": patch("trainer.trainer.build_canonical_mapping_from_df", return_value=cmap),
            "get_dummy_player_ids_from_df": patch("trainer.trainer.get_dummy_player_ids_from_df", return_value=set()),
            "ensure_profile": patch("trainer.trainer.ensure_player_profile_daily_ready"),
            "load_profile": patch("trainer.trainer.load_player_profile_daily", return_value=None),
            "process_chunk": patch("trainer.trainer.process_chunk", return_value="fake.parquet"),
            "read_parquet": patch("trainer.trainer.pd.read_parquet", return_value=fake_df),
            "train_dual_model": patch("trainer.trainer.train_dual_model",
                                     return_value=({"model": None, "threshold": 0.5, "features": []}, None, {})),
            "save_bundle": patch("trainer.trainer.save_artifact_bundle"),
            "path_stat": patch("trainer.trainer.Path",
                               **{"return_value.stat.return_value.st_size": 500}),
        }

        with (
            patches["get_monthly_chunks"],
            patches["load_local_parquet"],
            patches["apply_dq"],
            patches["build_canonical_mapping_from_df"],
            patches["get_dummy_player_ids_from_df"],
            patches["ensure_profile"] as mock_ensure,
            patches["load_profile"] as mock_load_profile,
            patches["process_chunk"] as mock_proc,
            patches["read_parquet"],
            patches["train_dual_model"],
            patches["save_bundle"],
            patches["path_stat"],
        ):
            start_date = chunks[0]["window_start"].strftime("%Y-%m-%d")
            end_date = chunks[-1]["window_end"].strftime("%Y-%m-%d")
            ns = {
                "start": start_date,
                "end": end_date,
                "days": None,
                "use_local_parquet": True,
                "force_recompute": False,
                "skip_optuna": True,
                "recent_chunks": self._recent_chunks,
                "fast_mode": self._fast_mode,
                "fast_mode_no_preload": self._no_preload,
                "sample_rated": self._sample_rated,
            }
            if self._extra_args:
                ns.update(self._extra_args)
            args = argparse.Namespace(**ns)
            run_pipeline(args)

        return {
            "ensure_kwargs": mock_ensure.call_args.kwargs if mock_ensure.called else {},
            "ensure_args": mock_ensure.call_args.args if mock_ensure.called else (),
            "chunks": chunks,
            "mock_proc_call_count": mock_proc.call_count,
            "mock_load_profile": mock_load_profile,
        }


# ---------------------------------------------------------------------------
# Test 1: fast_mode=True forwards correct horizon params
# ---------------------------------------------------------------------------

class TestFastModeHorizonPropagation(_PipelineMixin, unittest.TestCase):

    def setUp(self):
        # 3 chunks — ask for most recent 1 only so horizon ~ 30 days
        self._fake_chunks = _make_chunks(3)
        self._fast_mode = True
        self._recent_chunks = 1

    def test_snapshot_interval_days_is_fast_mode_constant(self):
        result = self._run_pipeline_with_mocks()
        kwargs = result["ensure_kwargs"]
        self.assertEqual(
            kwargs.get("snapshot_interval_days"),
            FAST_MODE_SNAPSHOT_INTERVAL_DAYS,
            "fast_mode should set snapshot_interval_days to FAST_MODE_SNAPSHOT_INTERVAL_DAYS",
        )

    def test_fast_mode_forwarded_to_ensure_profile(self):
        result = self._run_pipeline_with_mocks()
        self.assertTrue(
            result["ensure_kwargs"].get("fast_mode"),
            "fast_mode=True must be forwarded to ensure_player_profile_daily_ready",
        )

    def test_max_lookback_days_equals_data_horizon_not_365(self):
        result = self._run_pipeline_with_mocks()
        kwargs = result["ensure_kwargs"]
        mlb = kwargs.get("max_lookback_days")
        self.assertIsNotNone(mlb, "max_lookback_days must be passed")
        self.assertLess(
            mlb, 365,
            "fast_mode max_lookback_days should be data_horizon_days (~30), not 365",
        )

    def test_effective_window_uses_trimmed_chunk(self):
        """ensure_player_profile_daily_ready should receive the trimmed window (1 chunk).

        DEC-018 strips tz from effective_start/effective_end before passing them
        to downstream helpers.  Compare as tz-naive to stay robust to that normalization
        while still verifying the correct chunk window is used.
        """
        result = self._run_pipeline_with_mocks()
        chunks = result["chunks"]
        ensure_args = result["ensure_args"]
        # positional args: window_start, window_end
        eff_start, eff_end = ensure_args[0], ensure_args[1]
        # DEC-018: effective_start/end are tz-normalised to naive inside run_pipeline;
        # strip tz from the reference chunk datetimes to compare values only.
        exp_start = chunks[-1]["window_start"].replace(tzinfo=None)
        exp_end   = chunks[-1]["window_end"].replace(tzinfo=None)
        self.assertEqual(eff_start, exp_start,
                         "effective_start should match last chunk start (recent_chunks=1)")
        self.assertEqual(eff_end, exp_end,
                         "effective_end should match last chunk end")

    def test_process_chunk_called_once_for_one_chunk(self):
        result = self._run_pipeline_with_mocks()
        self.assertEqual(result["mock_proc_call_count"], 1,
                         "With recent_chunks=1, only 1 chunk should be processed")


# ---------------------------------------------------------------------------
# Test 2: fast_mode_no_preload=True -> preload_sessions=False
# ---------------------------------------------------------------------------

class TestFastModeNoPreload(_PipelineMixin, unittest.TestCase):

    def setUp(self):
        self._fake_chunks = _make_chunks(2)
        self._fast_mode = True
        self._no_preload = True
        self._recent_chunks = None

    def test_preload_sessions_is_false(self):
        result = self._run_pipeline_with_mocks()
        self.assertFalse(
            result["ensure_kwargs"].get("preload_sessions"),
            "fast_mode_no_preload=True must forward preload_sessions=False",
        )

    def test_preload_default_is_true_without_flag(self):
        self._no_preload = False
        result = self._run_pipeline_with_mocks()
        self.assertTrue(
            result["ensure_kwargs"].get("preload_sessions"),
            "Without no_preload, preload_sessions should be True",
        )


# ---------------------------------------------------------------------------
# Test 3: --sample-rated creates canonical_id_whitelist (orthogonal to fast_mode)
# ---------------------------------------------------------------------------

class TestSampleRatedWhitelist(_PipelineMixin, unittest.TestCase):

    def setUp(self):
        self._fake_chunks = _make_chunks(3)
        # canonical_map has 10 IDs; sample 3
        self._canonical_map_df = _canonical_map(10)
        self._sample_rated = 3
        self._fast_mode = False
        self._recent_chunks = None

    def test_whitelist_size_is_n(self):
        result = self._run_pipeline_with_mocks()
        wl = result["ensure_kwargs"].get("canonical_id_whitelist")
        self.assertIsNotNone(wl, "canonical_id_whitelist must be passed when --sample-rated is set")
        self.assertEqual(len(wl), 3, "whitelist should contain exactly sample_rated IDs")

    def test_whitelist_capped_at_available_ids(self):
        """When N > available IDs, whitelist should not exceed map size."""
        self._sample_rated = 50  # more than the 10 in canonical_map
        result = self._run_pipeline_with_mocks()
        wl = result["ensure_kwargs"].get("canonical_id_whitelist")
        self.assertIsNotNone(wl)
        self.assertLessEqual(len(wl), 10, "whitelist capped by available canonical IDs")

    def test_normal_mode_no_snapshot_interval_change(self):
        """sample_rated alone should NOT change snapshot_interval_days (not fast_mode)."""
        result = self._run_pipeline_with_mocks()
        kwargs = result["ensure_kwargs"]
        self.assertEqual(
            kwargs.get("snapshot_interval_days"), 1,
            "--sample-rated without --fast-mode should keep snapshot_interval_days=1",
        )

    def test_max_lookback_is_365_without_fast_mode(self):
        result = self._run_pipeline_with_mocks()
        self.assertEqual(
            result["ensure_kwargs"].get("max_lookback_days"), 365,
            "Without --fast-mode, max_lookback_days must be 365 even with --sample-rated",
        )


# ---------------------------------------------------------------------------
# Test 4: fast_mode + sample_rated combined
# ---------------------------------------------------------------------------

class TestFastModePlusSampleRated(_PipelineMixin, unittest.TestCase):

    def setUp(self):
        self._fake_chunks = _make_chunks(3)
        self._canonical_map_df = _canonical_map(10)
        self._fast_mode = True
        self._sample_rated = 5
        self._recent_chunks = 1

    def test_both_flags_forwarded_simultaneously(self):
        result = self._run_pipeline_with_mocks()
        kwargs = result["ensure_kwargs"]
        # fast_mode semantics
        self.assertTrue(kwargs.get("fast_mode"))
        self.assertEqual(kwargs.get("snapshot_interval_days"), FAST_MODE_SNAPSHOT_INTERVAL_DAYS)
        self.assertLess(kwargs.get("max_lookback_days", 365), 365)
        # sample_rated semantics
        wl = kwargs.get("canonical_id_whitelist")
        self.assertIsNotNone(wl)
        self.assertEqual(len(wl), 5)


if __name__ == "__main__":
    unittest.main()
