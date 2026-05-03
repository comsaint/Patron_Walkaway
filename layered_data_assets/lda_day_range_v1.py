"""Inclusive calendar-day lists for LDA orchestration (YYYY-MM-DD strings)."""
from __future__ import annotations

import re
from datetime import date, timedelta


def inclusive_iso_date_strings(date_from: str, date_to: str) -> list[str]:
    """Return each calendar day from ``date_from`` through ``date_to`` inclusive as ``YYYY-MM-DD``.

    Raises:
        ValueError: if strings are not valid dates or ``date_to`` is before ``date_from``.
    """
    a = _parse_iso_date(date_from, param="date_from")
    b = _parse_iso_date(date_to, param="date_to")
    if b < a:
        raise ValueError(f"date_to {date_to!r} must be on or after date_from {date_from!r}")
    out: list[str] = []
    cur = a
    while cur <= b:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _parse_iso_date(value: str, *, param: str) -> date:
    """Parse ``YYYY-MM-DD`` into a :class:`datetime.date`.

    Raises:
        ValueError: if the string is not ``YYYY-MM-DD`` or not a real calendar day.
    """
    s = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"{param} must be YYYY-MM-DD, got {value!r}")
    y, m, d = (int(s[0:4]), int(s[5:7]), int(s[8:10]))
    try:
        return date(y, m, d)
    except ValueError as exc:
        raise ValueError(f"{param} invalid calendar date {value!r}: {exc}") from exc
