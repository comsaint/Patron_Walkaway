"""Unit tests for Step 3 Feast month×group cache helpers (pure logic)."""

from __future__ import annotations

import importlib
from datetime import date

import pytest

_bt3 = importlib.import_module("trainer_hightier.03_build_training_data")


@pytest.mark.parametrize(
    "rel,want",
    [
        ("m/gaming_day_key=2024-06-07/bucket_0000.parquet", date(2024, 6, 7)),
        ("m\\gaming_day_key=2020-01-01\\f.parquet", date(2020, 1, 1)),
        ("noHiveHere.parquet", None),
        ("gaming_day_key=not-a-date/x.parquet", None),
    ],
)
def test_gaming_date_from_shard_rel(rel: str, want: date | None) -> None:
    """Hive ``gaming_day_key`` segment maps to UTC calendar dates for dirty diffusion."""
    got = _bt3._gaming_date_from_shard_rel(rel)
    assert got == want


def test_cleaned_token_partitioned_stable() -> None:
    blk = {"shard_list_sha256_hex": "ab" * 32}
    assert _bt3._cleaned_artifact_fingerprint_token(blk) == "ab" * 32


def test_dirty_dates_none_without_prev_and_empty_when_stable() -> None:
    """No prior fingerprint forces full invalidation; identical blocks short-circuit to empty."""

    cur = {"shard_list_sha256_hex": "aa" * 32, "shard_stats": [{"rel_path": "g=1"}], "manifest_storage_kind": None}
    assert _bt3._dirty_shard_calendar_dates(None, cur) is None

    prev = dict(cur)
    assert _bt3._dirty_shard_calendar_dates(prev, cur) == frozenset()


def test_affected_slow_group_spans_farther_than_trial_group() -> None:
    """180d expansion should touch distant months unlike 2-day trial windows."""

    months = [date(2025, 1, 1), date(2025, 6, 1), date(2025, 7, 1), date(2025, 12, 1)]
    dirt = frozenset({date(2025, 7, 3)})
    plan = (
        ("cleaned", "walkaway_bet_v1", 31),
        ("trial_clock", "walkaway_bet_trial_clock_v1", 2),
        ("slow_snap", "walkaway_bet_slow_snap_v1", 180),
    )
    aff = _bt3._affected_month_indices_by_group(months, dirt, plan)
    assert aff["trial_clock"] == {2}
    assert aff["trial_clock"] <= aff["slow_snap"]
    assert 0 in aff["slow_snap"]
