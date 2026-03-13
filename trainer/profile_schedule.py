"""trainer/profile_schedule.py — shared month-end date helpers for player_profile ETL.

Used by trainer.ensure_player_profile_ready (DEC-019) and by etl_player_profile CLI
(--month-end) so both use the same schedule logic. Single source of truth for
month-end snapshot dates and intra-month anchor.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import List


def month_end_dates(start_date: date, end_date: date) -> List[date]:
    """Return the last calendar day of each month in [start_date, end_date].

    Used by DEC-019 to build a month-end profile snapshot schedule.
    At most one snapshot per month is produced; the PIT join in
    join_player_profile uses the most-recent snapshot <= bet_time,
    so bets mid-month will fall back to the previous month-end snapshot.

    Parameters
    ----------
    start_date, end_date:
        Inclusive date range.  Both must use HK-calendar dates.

    Returns
    -------
    Sorted list of date objects, each being the last day of its month,
    filtered to [start_date, end_date].
    """
    result: List[date] = []
    year, month = start_date.year, start_date.month
    while True:
        last_day = calendar.monthrange(year, month)[1]
        month_end = date(year, month, last_day)
        if month_end > end_date:
            break
        if month_end >= start_date:
            result.append(month_end)
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return result


def latest_month_end_on_or_before(ref_date: date) -> date:
    """Return the nearest month-end date that is <= ref_date."""
    year, month = ref_date.year, ref_date.month
    month_last = calendar.monthrange(year, month)[1]
    cand = date(year, month, month_last)
    if cand <= ref_date:
        return cand
    # Previous month-end.
    if month == 1:
        year -= 1
        month = 12
    else:
        month -= 1
    prev_last = calendar.monthrange(year, month)[1]
    return date(year, month, prev_last)
