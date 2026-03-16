"""Minimal reproducible guardrail tests for Round 50 review risks (R600-R605).

Tests-only round: no production code changes.
Unfixed risks are encoded as expected failures so they remain visible
without blocking the suite.
"""

from __future__ import annotations

import datetime as dt
import inspect
import unittest
from unittest.mock import patch

import pandas as pd

import trainer.etl_player_profile as etl_mod
import trainer.profile_schedule as profile_schedule_mod
import trainer.trainer as trainer_mod


class TestR600MonthEndEmptyRangeFallback(unittest.TestCase):
    """R600: month-end schedule can become empty for intra-month missing ranges."""

    def test_month_end_dates_partial_month_returns_empty_list(self):
        # Minimal repro:
        # missing range 2026-02-01 -> 2026-02-13 has no month-end date inside.
        got = profile_schedule_mod.month_end_dates(dt.date(2026, 2, 1), dt.date(2026, 2, 13))
        self.assertEqual(got, [])

    def test_ensure_profile_should_have_explicit_empty_snapshot_dates_fallback(self):
        # Desired guardrail from review:
        # if _snap_dates == [], do not silently skip; fallback to interval path.
        src = inspect.getsource(trainer_mod.ensure_player_profile_ready)
        self.assertIn(
            "len(_snap_dates) == 0",
            src,
            "ensure_player_profile_ready should explicitly guard empty month-end schedule.",
        )


class TestR601SchemaHashScheduleIsolation(unittest.TestCase):
    """R601: schema hash should isolate month-end vs daily cache modes."""

    def test_schema_hash_should_include_schedule_tag_in_reader(self):
        src = inspect.getsource(trainer_mod.ensure_player_profile_ready)
        self.assertIn(
            "_sched_tag",
            src,
            "profile schema hash should include schedule mode tag (month-end/daily).",
        )

    def test_schema_hash_should_include_schedule_tag_in_writer(self):
        src = inspect.getsource(etl_mod._persist_local_parquet)
        self.assertIn(
            "_sched_tag",
            src,
            "profile schema hash writer should include schedule mode tag (month-end/daily).",
        )


class TestR602MonthEndPreloadMemoryRisk(unittest.TestCase):
    """R602: month-end schedule should avoid full-table preload on low-RAM machines."""

    def test_backfill_month_end_without_whitelist_should_not_preload(self):
        # Desired behavior (from review): month-end has ~12 dates/year, so
        # per-date pushdown reads are acceptable; avoid preloading 69M rows.
        with patch.object(etl_mod, "_preload_sessions_local", return_value=pd.DataFrame()) as preload_mock, patch.object(
            etl_mod, "build_player_profile", return_value=pd.DataFrame({"x": [1]})
        ):
            etl_mod.backfill(
                start_date=dt.date(2026, 1, 1),
                end_date=dt.date(2026, 2, 28),
                use_local_parquet=True,
                canonical_id_whitelist=None,
                snapshot_interval_days=1,
                preload_sessions=True,
                canonical_map=pd.DataFrame({"player_id": [1], "canonical_id": ["A"]}),
                snapshot_dates=[dt.date(2026, 1, 31), dt.date(2026, 2, 28)],
            )
        self.assertEqual(
            preload_mock.call_count,
            0,
            "month-end backfill should not trigger full-table preload by default.",
        )


class TestR603PreloadLogModeLabel(unittest.TestCase):
    """R603: preload log should not label month-end schedule as fast-mode."""

    def test_backfill_preload_log_should_be_schedule_aware(self):
        src = inspect.getsource(etl_mod.backfill)
        self.assertNotIn(
            "for fast-mode (interval=%d days)",
            src,
            "preload log message should be schedule-aware (month-end vs fast-mode).",
        )


class TestR605SampleRatedMonthEndInteraction(unittest.TestCase):
    """R605: interaction is expected to be correct; keep a green regression guard."""

    def test_backfill_snapshot_dates_processes_only_filtered_sorted_dates(self):
        calls: list[dt.date] = []

        def _record_call(snapshot_date, **kwargs):
            calls.append(snapshot_date)
            return pd.DataFrame({"snapshot_date": [snapshot_date]})

        with patch.object(etl_mod, "build_player_profile", side_effect=_record_call), patch.object(
            etl_mod, "_preload_sessions_local", return_value=None
        ):
            etl_mod.backfill(
                start_date=dt.date(2026, 1, 1),
                end_date=dt.date(2026, 2, 28),
                use_local_parquet=True,
                canonical_id_whitelist={"A"},
                snapshot_interval_days=1,
                preload_sessions=False,
                canonical_map=pd.DataFrame({"player_id": [1], "canonical_id": ["A"]}),
                # intentionally unsorted + with out-of-range date
                snapshot_dates=[dt.date(2026, 2, 28), dt.date(2025, 12, 31), dt.date(2026, 1, 31)],
            )

        self.assertEqual(calls, [dt.date(2026, 1, 31), dt.date(2026, 2, 28)])


if __name__ == "__main__":
    unittest.main()
