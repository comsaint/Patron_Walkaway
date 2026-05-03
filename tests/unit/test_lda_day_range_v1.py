"""Tests for ``layered_data_assets.lda_day_range_v1``."""

import pytest

from layered_data_assets.lda_day_range_v1 import inclusive_iso_date_strings


def test_inclusive_iso_date_strings_single_day() -> None:
    assert inclusive_iso_date_strings("2026-01-05", "2026-01-05") == ["2026-01-05"]


def test_inclusive_iso_date_strings_three_days() -> None:
    assert inclusive_iso_date_strings("2026-01-01", "2026-01-03") == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    ]


def test_inclusive_iso_date_strings_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="on or after"):
        inclusive_iso_date_strings("2026-01-03", "2026-01-01")


def test_inclusive_iso_date_strings_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="date_from"):
        inclusive_iso_date_strings("01-01-2026", "2026-01-02")


def test_inclusive_iso_date_strings_rejects_impossible_date_from() -> None:
    with pytest.raises(ValueError, match=r"date_from invalid calendar date"):
        inclusive_iso_date_strings("2026-02-30", "2026-03-01")


def test_inclusive_iso_date_strings_rejects_impossible_date_to() -> None:
    with pytest.raises(ValueError, match=r"date_to invalid calendar date"):
        inclusive_iso_date_strings("2026-01-01", "2026-02-30")
