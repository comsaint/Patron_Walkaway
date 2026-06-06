"""Tests for L1 recompute month selection (source manifest v2)."""

from __future__ import annotations

import pytest

from trainer_hightier.utils.cache_invalidation_v1 import (
    compute_l1_recompute_months,
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


def test_compute_l1_recompute_empty_diff_only_correction() -> None:
    got = compute_l1_recompute_months(
        changed_partitions={"t_bet": [], "t_session": []},
        correction_months=("202406",),
        backfill_month_count=0,
        available_months={"202406", "202407"},
    )
    assert got == ["202406"]
