"""Tests for etl_player_profile CLI --month-end (backfill called with correct snapshot_dates)."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import trainer.etl_player_profile as etl_mod


class TestEtlPlayerProfileMonthEndCli(unittest.TestCase):
    def test_month_end_cli_calls_backfill_with_month_end_dates(self):
        with patch.object(etl_mod, "backfill") as mock_backfill, patch.object(
            etl_mod, "_parse_args"
        ) as mock_parse:
            mock_parse.return_value = unittest.mock.MagicMock(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 3, 31),
                local_parquet=True,
                no_progress=True,
                month_end=True,
                snapshot_interval_days=1,
                snapshot_date=None,
                log_level="INFO",
            )
            etl_mod.main()
            mock_backfill.assert_called_once()
            call_kw = mock_backfill.call_args[1]
            self.assertIn("snapshot_dates", call_kw)
            self.assertEqual(
                call_kw["snapshot_dates"],
                [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)],
            )
            self.assertEqual(call_kw["use_local_parquet"], True)

    def test_month_end_cli_intra_month_calls_backfill_with_single_anchor(self):
        with patch.object(etl_mod, "backfill") as mock_backfill, patch.object(
            etl_mod, "_parse_args"
        ) as mock_parse:
            mock_parse.return_value = unittest.mock.MagicMock(
                start_date=date(2026, 2, 1),
                end_date=date(2026, 2, 15),
                local_parquet=True,
                no_progress=True,
                month_end=True,
                snapshot_interval_days=1,
                snapshot_date=None,
                log_level="INFO",
            )
            etl_mod.main()
            mock_backfill.assert_called_once()
            call_kw = mock_backfill.call_args[1]
            self.assertIn("snapshot_dates", call_kw)
            self.assertEqual(call_kw["snapshot_dates"], [date(2026, 1, 31)])
            # backfill_start should be min(2026-02-01, 2026-01-31) = 2026-01-31
            self.assertEqual(etl_mod.backfill.call_args[0][0], date(2026, 1, 31))
            self.assertEqual(etl_mod.backfill.call_args[0][1], date(2026, 2, 15))

    def test_etl_main_start_after_end_month_end_still_calls_backfill(self):
        """Code review §1: minimal reproducible — start_date > end_date with --month-end.
        Current behavior: backfill is called with anchor; when production adds
        start<=end check, change to expect SystemExit and backfill not called.
        """
        with patch.object(etl_mod, "backfill") as mock_backfill, patch.object(
            etl_mod, "_parse_args"
        ) as mock_parse:
            mock_parse.return_value = unittest.mock.MagicMock(
                start_date=date(2026, 3, 1),
                end_date=date(2026, 1, 1),
                local_parquet=True,
                no_progress=True,
                month_end=True,
                snapshot_interval_days=1,
                snapshot_date=None,
                log_level="INFO",
            )
            etl_mod.main()
            mock_backfill.assert_called_once()
            self.assertEqual(mock_backfill.call_args[0][0], date(2025, 12, 31))
            self.assertEqual(mock_backfill.call_args[0][1], date(2026, 1, 1))
            self.assertEqual(mock_backfill.call_args[1]["snapshot_dates"], [date(2025, 12, 31)])

    def test_etl_main_snapshot_interval_days_zero_passed_as_one(self):
        """Code review §3: when --snapshot-interval-days is 0, backfill receives 1."""
        with patch.object(etl_mod, "backfill") as mock_backfill, patch.object(
            etl_mod, "_parse_args"
        ) as mock_parse:
            mock_parse.return_value = unittest.mock.MagicMock(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 31),
                local_parquet=True,
                no_progress=True,
                month_end=False,
                snapshot_interval_days=0,
                snapshot_date=None,
                log_level="INFO",
            )
            etl_mod.main()
            mock_backfill.assert_called_once()
            self.assertEqual(mock_backfill.call_args[1]["snapshot_interval_days"], 1)

    def test_etl_main_snapshot_interval_days_negative_passed_as_one(self):
        """Code review §3: when --snapshot-interval-days is negative, backfill receives 1."""
        with patch.object(etl_mod, "backfill") as mock_backfill, patch.object(
            etl_mod, "_parse_args"
        ) as mock_parse:
            mock_parse.return_value = unittest.mock.MagicMock(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 31),
                local_parquet=True,
                no_progress=True,
                month_end=False,
                snapshot_interval_days=-1,
                snapshot_date=None,
                log_level="INFO",
            )
            etl_mod.main()
            mock_backfill.assert_called_once()
            self.assertEqual(mock_backfill.call_args[1]["snapshot_interval_days"], 1)
