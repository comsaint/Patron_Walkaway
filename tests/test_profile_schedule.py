"""Unit tests for trainer.profile_schedule (month_end_dates, latest_month_end_on_or_before).

Shared module used by trainer.ensure_player_profile_ready and etl_player_profile --month-end.
"""

from __future__ import annotations

import unittest
from datetime import date

from trainer.profile_schedule import latest_month_end_on_or_before, month_end_dates


class TestMonthEndDates(unittest.TestCase):
    def test_multi_month_returns_last_day_of_each_month(self):
        got = month_end_dates(date(2026, 1, 1), date(2026, 3, 31))
        self.assertEqual(got, [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)])

    def test_single_month_end_in_range(self):
        got = month_end_dates(date(2026, 2, 28), date(2026, 2, 28))
        self.assertEqual(got, [date(2026, 2, 28)])

    def test_intra_month_returns_empty_list(self):
        # 2026-02-01 .. 2026-02-15 has no month-end in range
        got = month_end_dates(date(2026, 2, 1), date(2026, 2, 15))
        self.assertEqual(got, [])

    def test_boundary_start_before_month_end_included(self):
        got = month_end_dates(date(2026, 1, 15), date(2026, 2, 28))
        self.assertEqual(got, [date(2026, 1, 31), date(2026, 2, 28)])

    def test_leap_year_february(self):
        got = month_end_dates(date(2024, 2, 1), date(2024, 2, 29))
        self.assertEqual(got, [date(2024, 2, 29)])

    def test_start_after_end_returns_empty_list(self):
        """Code review §2: when start_date > end_date, returns [] (reproducible)."""
        got = month_end_dates(date(2026, 3, 1), date(2026, 1, 1))
        self.assertEqual(got, [])


class TestLatestMonthEndOnOrBefore(unittest.TestCase):
    def test_same_day_as_month_end_returns_it(self):
        self.assertEqual(latest_month_end_on_or_before(date(2026, 1, 31)), date(2026, 1, 31))

    def test_mid_month_returns_previous_month_end(self):
        self.assertEqual(latest_month_end_on_or_before(date(2026, 2, 15)), date(2026, 1, 31))

    def test_first_of_month_returns_previous_month_end(self):
        self.assertEqual(latest_month_end_on_or_before(date(2026, 2, 1)), date(2026, 1, 31))

    def test_january_first_returns_previous_dec(self):
        self.assertEqual(latest_month_end_on_or_before(date(2026, 1, 1)), date(2025, 12, 31))
