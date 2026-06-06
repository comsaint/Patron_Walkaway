"""Tests for L1 recompute month selection (source manifest v2)."""

from __future__ import annotations

import pytest

from trainer_hightier.utils.cache_invalidation_v1 import (
    compute_l1_recompute_months,
    label_invalid_months,
    mid_term_invalid_months,
    shift_calendar_month,
    short_pit_invalid_months,
    union_changed_partition_months,
)


def test_union_changed_partition_months_merges_tables() -> None:
    got = union_changed_partition_months(
        {"t_bet": ["202503", "202504"], "t_session": ["202503"]},
    )
    assert got == {"202503", "202504"}


def test_union_changed_partition_months_rejects_bad_yyyymm() -> None:
    with pytest.raises(ValueError, match="YYYYMM digits"):
        union_changed_partition_months({"t_bet": ["20X503"], "t_session": []})


def test_compute_l1_recompute_single_modified_month() -> None:
    got = compute_l1_recompute_months(
        changed_partitions={"t_bet": ["202503"], "t_session": []},
        correction_months=(),
        backfill_month_count=0,
        available_months={"202501", "202502", "202503", "202504"},
    )
    assert got == ["202503"]


def test_compute_l1_recompute_backfill_and_correction() -> None:
    got = compute_l1_recompute_months(
        changed_partitions={"t_bet": ["202503"], "t_session": []},
        correction_months=("202401",),
        backfill_month_count=1,
        available_months={"202401", "202502", "202503"},
    )
    assert got == ["202401", "202502", "202503"]


def test_compute_l1_recompute_intersects_available_only() -> None:
    got = compute_l1_recompute_months(
        changed_partitions={"t_bet": ["202601"], "t_session": ["202602"]},
        correction_months=(),
        backfill_month_count=0,
        available_months={"202601"},
    )
    assert got == ["202601"]


def test_p3_t1_label_invalid_months_prev_dirty_next() -> None:
    got = label_invalid_months({"202503"})
    assert got == {"202502", "202503", "202504"}


def test_p3_t2_label_invalid_months_year_boundary() -> None:
    got = label_invalid_months({"202501"})
    assert got == {"202412", "202501", "202502"}


def test_p3_t3_short_pit_neighbor_backfill() -> None:
    got = short_pit_invalid_months({"202503"}, neighbor_backfill=1)
    assert got == {"202502", "202503"}


def test_shift_calendar_month_handles_rollover() -> None:
    assert shift_calendar_month("202501", delta_months=-1) == "202412"
    assert shift_calendar_month("202512", delta_months=1) == "202601"


def test_mid_term_invalid_months_lookback() -> None:
    got = mid_term_invalid_months({"202503"}, lookback_months=2)
    assert got == {"202501", "202502", "202503"}


def test_compute_l1_recompute_empty_diff_only_correction() -> None:
    got = compute_l1_recompute_months(
        changed_partitions={"t_bet": [], "t_session": []},
        correction_months=("202406",),
        backfill_month_count=0,
        available_months={"202406", "202407"},
    )
    assert got == ["202406"]
