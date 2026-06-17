"""Unit tests for Step 3 Feast month×group cache helpers (pure logic)."""

from __future__ import annotations

import importlib
import json
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig

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


def test_affected_slow_group_spans_farther_than_cleaned_group() -> None:
    """180d slow expansion should touch distant months unlike 31-day cleaned windows."""

    months = [date(2025, 1, 1), date(2025, 6, 1), date(2025, 7, 1), date(2025, 12, 1)]
    dirt = frozenset({date(2025, 7, 3)})
    plan = (
        ("cleaned", "walkaway_bet_v1", 31),
        ("slow_snap", "walkaway_canonical_slow_snap_v1", 180),
    )
    aff = _bt3._affected_month_indices_by_group(months, dirt, plan)
    assert 2 in aff["cleaned"]
    assert aff["cleaned"] <= aff["slow_snap"]
    assert 0 in aff["slow_snap"]


def test_feast_group_plan_excludes_trial_clock() -> None:
    plan = _bt3._feast_group_plan("walkaway_bet_trial_v1")
    gids = {g[0] for g in plan}
    assert gids == {"cleaned", "slow_snap"}


def test_slow_parquet_grain_canonical_vs_bet(tmp_path: Path) -> None:
    canon = tmp_path / "slow_canon.parquet"
    pd.DataFrame(
        {
            "canonical_id": ["c1"],
            "anchor_gaming_day_event": [date(2026, 4, 30)],
            "patron__theo_win_sum__w180d_m1snap": [1.0],
            "patron__gaming_days_cnt__w180d_m1snap": [2],
            "patron__adt__w180d_m1snap": [0.5],
        }
    ).to_parquet(canon, index=False)
    assert _bt3._slow_parquet_grain(canon) == "canonical"

    bet = tmp_path / "slow_bet.parquet"
    pd.DataFrame({"bet_id": [1.0], "patron__theo_win_sum__w180d_m1snap": [1.0]}).to_parquet(bet, index=False)
    assert _bt3._slow_parquet_grain(bet) == "bet"


def test_attach_canonical_slow_snap_for_entities(tmp_path: Path) -> None:
    ent = tmp_path / "entity.parquet"
    pd.DataFrame({"bet_id": [10.0], "event_timestamp": [pd.Timestamp("2024-07-01", tz="UTC")]}).to_parquet(
        ent, index=False
    )
    bet = tmp_path / "bet.parquet"
    pd.DataFrame({"bet_id": [10.0], "player_id": [99]}).to_parquet(bet, index=False)
    cmap = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [99], "canonical_id": ["c1"]}).to_parquet(cmap, index=False)
    slow = tmp_path / "slow.parquet"
    pd.DataFrame(
        {
            "canonical_id": ["c1"],
            "anchor_gaming_day_event": [date(2026, 4, 30)],
            "patron__theo_win_sum__w180d_m1snap": [42.0],
            "patron__gaming_days_cnt__w180d_m1snap": [3],
            "patron__adt__w180d_m1snap": [14.0],
        }
    ).to_parquet(slow, index=False)
    out = tmp_path / "out.parquet"
    _bt3._attach_canonical_slow_snap_for_entities(
        entity_parquet=ent,
        cleaned_bet_parquet=bet,
        canonical_mapping_parquet=cmap,
        slow_parquet=slow,
        output_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    row = duckdb.sql(f"SELECT * FROM read_parquet('{out.as_posix()}')").fetchone()
    assert row is not None
    assert float(row[0]) == 10.0
    assert float(row[2]) == 42.0


def test_group_cache_manifest_blob_excludes_target_scope(tmp_path: Path) -> None:
    """Primitive cache key/manifest must not depend on training target scope."""
    manifest_path = tmp_path / "202603.parquet.manifest.json"
    _bt3._write_group_cache_manifest(
        manifest_path,
        aggregate_name="walkaway_bet_trial_v1",
        group_id="slow_snap",
        feast_subgroup="walkaway_canonical_slow_snap_v1",
        month_yyyymm="202603",
        cleaned_token="aa" * 32,
        derived_stat={"path": "/tmp/slow.parquet"},
        code_fp="bb" * 64,
        out_stat={"num_rows": 123},
    )
    blob = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "target_scope" not in blob
    assert set(blob.keys()) == {
        "schema_version",
        "aggregate_feature_service",
        "group_id",
        "feast_subgroup_service",
        "month_yyyymm",
        "cleaned_fingerprint_token",
        "derived_source_stat",
        "code_fingerprint",
        "output_parquet_stat",
    }
