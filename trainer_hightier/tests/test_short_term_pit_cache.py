"""Tests for month-sharded short-term PIT cache reuse."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig, SHORT_TERM_TRIAL_BET_COLUMNS
from trainer_hightier.feature_experiment.short_term_pit_cache import (
    REASON_FORCE_REFRESH,
    REASON_UNIVERSE_CHANGED,
    _merge_delta_rows_into_shard,
    _shard_delta_fill_eligible,
    compute_shard_universe_fingerprint,
    list_training_payout_months,
    materialize_fe_derived_short_term_parquet_with_cache,
    plan_short_term_pit_cache,
    short_term_pit_cache_root,
)


def _write_training_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), path)


def _write_mapping(path: Path) -> None:
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame([{"player_id": 10, "canonical_id": "c10"}])),
        path,
    )


def _write_cleaned_bet_hive(root: Path, rows: list[dict[str, object]]) -> None:
    day = pd.Timestamp("2024-06-01 06:30:00", tz="UTC").tz_convert("Asia/Hong_Kong").date()
    enriched: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item.setdefault("gaming_day_event", day)
        item.setdefault("gaming_month", "202406")
        item.setdefault("gaming_day_key", "2024-06-01")
        enriched.append(item)
    out_dir = root / "gaming_month=202406" / "gaming_day_key=2024-06-01"
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(enriched)), out_dir / "part-000.parquet")


def _minimal_training_rows() -> list[dict[str, object]]:
    t0 = pd.Timestamp("2024-06-01 06:30:00", tz="UTC")
    t1 = t0 + pd.Timedelta(minutes=30)
    day = t0.tz_convert("Asia/Hong_Kong").date()
    base = {
        "session_id": 1,
        "table_id": 1,
        "gaming_day_event": day,
        "bet_type": "PLAYER",
        "type_of_bet": "MAIN",
    }
    return [
        {
            **base,
            "bet_id": 1.0,
            "player_id": 10,
            "payout_complete_dtm": t0,
            "wager": 100.0,
            "is_back_bet": 0,
            "payout_odds": 2.0,
            "casino_win": 0.0,
        },
        {
            **base,
            "bet_id": 2.0,
            "player_id": 10,
            "payout_complete_dtm": t1,
            "wager": 50.0,
            "is_back_bet": 1,
            "payout_odds": 2.0,
            "casino_win": 0.0,
        },
    ]


def _minimal_cleaned_rows() -> list[dict[str, object]]:
    t0 = pd.Timestamp("2024-06-01 06:30:00", tz="UTC")
    t1 = t0 + pd.Timedelta(minutes=30)
    t_prev = t0 - pd.Timedelta(minutes=20)
    day = t0.tz_convert("Asia/Hong_Kong").date()
    return [
        {
            "bet_id": 0.0,
            "player_id": 10,
            "session_id": 1,
            "table_id": 1,
            "gaming_day_event": day,
            "payout_complete_dtm": t_prev,
            "wager": 80.0,
            "is_back_bet": 0,
            "payout_odds": 2.0,
            "casino_win": 0.0,
            "theo_win": 0.8,
            "base_ha": 0.01,
            "bet_type": "PLAYER",
            "type_of_bet": "MAIN",
        },
        *_minimal_training_rows(),
    ]


@pytest.fixture
def short_cache_fixture(tmp_path: Path) -> dict[str, Path]:
    """Minimal training + cleaned bet layout for short-term cache tests."""
    training = tmp_path / "training_set.parquet"
    mapping = tmp_path / "canonical_player_mapping.parquet"
    cleaned = tmp_path / "cleaned_bet"
    out = tmp_path / "_main_trainer_fe_short_term.parquet"
    _write_training_parquet(training, _minimal_training_rows())
    _write_mapping(mapping)
    _write_cleaned_bet_hive(cleaned, _minimal_cleaned_rows())
    return {
        "training": training,
        "mapping": mapping,
        "cleaned": cleaned,
        "out": out,
    }


def test_list_training_payout_months_and_universe_fingerprint(short_cache_fixture: dict[str, Path]) -> None:
    runtime = DuckDbRuntimeConfig()
    months = list_training_payout_months(short_cache_fixture["training"], duckdb_runtime=runtime)
    assert months == ("202406",)
    fp, n = compute_shard_universe_fingerprint(
        short_cache_fixture["training"],
        yyyymm="202406",
        duckdb_runtime=runtime,
    )
    assert n == 2
    assert isinstance(fp, str) and len(fp) >= 8


def test_short_term_pit_cache_hit_on_second_run(short_cache_fixture: dict[str, Path]) -> None:
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    kwargs = {
        "cleaned_bet_parquet": short_cache_fixture["cleaned"],
        "training_parquet_for_bet_ids": short_cache_fixture["training"],
        "out_parquet": short_cache_fixture["out"],
        "duckdb_runtime": runtime,
        "canonical_mapping_parquet": short_cache_fixture["mapping"],
        "short_term_columns": ("fe__bets_cnt__w15m",),
        "trial_columns": trial_cols,
        "batch_size": 2000,
    }
    _, meta1 = materialize_fe_derived_short_term_parquet_with_cache(**kwargs)
    assert meta1["cache_miss_shards"]
    assert meta1["cache_hit"] is False

    _, meta2 = materialize_fe_derived_short_term_parquet_with_cache(**kwargs)
    assert meta2["cache_hit"] is True
    assert meta2["cache_miss_shards"] == []
    assert meta2["cache_hit_ratio"] == 1.0


def test_short_term_pit_cache_force_refresh_invalidates(short_cache_fixture: dict[str, Path]) -> None:
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    kwargs = {
        "cleaned_bet_parquet": short_cache_fixture["cleaned"],
        "training_parquet_for_bet_ids": short_cache_fixture["training"],
        "out_parquet": short_cache_fixture["out"],
        "duckdb_runtime": runtime,
        "canonical_mapping_parquet": short_cache_fixture["mapping"],
        "short_term_columns": ("fe__bets_cnt__w15m",),
        "trial_columns": trial_cols,
        "batch_size": 2000,
    }
    materialize_fe_derived_short_term_parquet_with_cache(**kwargs)
    _, meta = materialize_fe_derived_short_term_parquet_with_cache(force_refresh=True, **kwargs)
    assert meta["cache_hit"] is False
    assert meta["cache_reason_counts"].get(REASON_FORCE_REFRESH, 0) >= 1


def test_short_term_pit_cache_universe_change_misses_shard(short_cache_fixture: dict[str, Path]) -> None:
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    out_cols = ("bet_id", *trial_cols, "fe__bets_cnt__w15m")
    cache_root = short_term_pit_cache_root(short_cache_fixture["training"].parent)
    plan1 = plan_short_term_pit_cache(
        training_parquet=short_cache_fixture["training"],
        cache_root=cache_root,
        out_columns=out_cols,
        trial_columns=trial_cols,
        canonical_mapping_parquet=short_cache_fixture["mapping"],
        batch_size=2000,
        duckdb_runtime=runtime,
    )
    assert plan1.miss_shards == ("202406",)

    materialize_fe_derived_short_term_parquet_with_cache(
        cleaned_bet_parquet=short_cache_fixture["cleaned"],
        training_parquet_for_bet_ids=short_cache_fixture["training"],
        out_parquet=short_cache_fixture["out"],
        duckdb_runtime=runtime,
        canonical_mapping_parquet=short_cache_fixture["mapping"],
        short_term_columns=("fe__bets_cnt__w15m",),
        trial_columns=trial_cols,
        batch_size=2000,
    )
    plan2 = plan_short_term_pit_cache(
        training_parquet=short_cache_fixture["training"],
        cache_root=cache_root,
        out_columns=out_cols,
        trial_columns=trial_cols,
        canonical_mapping_parquet=short_cache_fixture["mapping"],
        batch_size=2000,
        duckdb_runtime=runtime,
    )
    assert plan2.hit_shards == ("202406",)
    assert plan2.miss_shards == ()

    rows = _minimal_training_rows()
    rows.append(
        {
            "bet_id": 3.0,
            "player_id": 10,
            "session_id": 1,
            "table_id": 1,
            "gaming_day_event": rows[0]["gaming_day_event"],
            "payout_complete_dtm": pd.Timestamp("2024-06-01 07:30:00", tz="UTC"),
            "wager": 10.0,
            "is_back_bet": 0,
            "payout_odds": 2.0,
            "casino_win": 0.0,
            "bet_type": "PLAYER",
            "type_of_bet": "MAIN",
        },
    )
    changed = short_cache_fixture["training"].parent / "training_changed.parquet"
    _write_training_parquet(changed, rows)
    plan3 = plan_short_term_pit_cache(
        training_parquet=changed,
        cache_root=cache_root,
        out_columns=out_cols,
        trial_columns=trial_cols,
        canonical_mapping_parquet=short_cache_fixture["mapping"],
        batch_size=2000,
        duckdb_runtime=runtime,
    )
    assert plan3.miss_shards == ("202406",)
    assert plan3.reason_counts.get(REASON_UNIVERSE_CHANGED, 0) >= 1


def test_merge_delta_rows_into_shard_prefers_delta_bet_id(tmp_path: Path) -> None:
    cols = ("bet_id", "fe__bets_cnt__w15m")
    base_p = tmp_path / "base.parquet"
    delta_p = tmp_path / "delta.parquet"
    out_p = tmp_path / "shard" / "data.parquet"
    out_p.parent.mkdir(parents=True)
    pq.write_table(pa.table({"bet_id": [1.0, 2.0], "fe__bets_cnt__w15m": [1, 2]}), base_p)
    pq.write_table(pa.table({"bet_id": [2.0], "fe__bets_cnt__w15m": [99]}), delta_p)
    shutil.copy(base_p, out_p)
    _merge_delta_rows_into_shard(
        shard_parquet=out_p,
        delta_parquet=delta_p,
        out_columns=cols,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    got = pq.read_table(out_p).to_pandas().sort_values("bet_id")
    assert int(got.loc[got["bet_id"] == 2.0, "fe__bets_cnt__w15m"].iloc[0]) == 99


def test_short_term_pit_cache_entity_set_fingerprint_misses(short_cache_fixture: dict[str, Path]) -> None:
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    kwargs = {
        "cleaned_bet_parquet": short_cache_fixture["cleaned"],
        "training_parquet_for_bet_ids": short_cache_fixture["training"],
        "out_parquet": short_cache_fixture["out"],
        "duckdb_runtime": runtime,
        "canonical_mapping_parquet": short_cache_fixture["mapping"],
        "short_term_columns": ("fe__bets_cnt__w15m",),
        "trial_columns": trial_cols,
        "batch_size": 2000,
        "entity_set_fingerprint_sha256_hex": "entity_fp_a" * 4,
    }
    materialize_fe_derived_short_term_parquet_with_cache(**kwargs)
    _, meta2 = materialize_fe_derived_short_term_parquet_with_cache(
        **{**kwargs, "entity_set_fingerprint_sha256_hex": "entity_fp_b" * 4},
    )
    assert meta2["cache_hit"] is False
    assert meta2["cache_miss_shards"] == ["202406"]


def test_p3_t6_registry_remove_feature_primitive_still_hits(short_cache_fixture: dict[str, Path]) -> None:
    """P3-T-6: removing registry ``fe__*`` columns should not invalidate primitive shards."""
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    base_kwargs = {
        "cleaned_bet_parquet": short_cache_fixture["cleaned"],
        "training_parquet_for_bet_ids": short_cache_fixture["training"],
        "out_parquet": short_cache_fixture["out"],
        "duckdb_runtime": runtime,
        "canonical_mapping_parquet": short_cache_fixture["mapping"],
        "trial_columns": trial_cols,
        "batch_size": 2000,
    }
    materialize_fe_derived_short_term_parquet_with_cache(
        **base_kwargs,
        short_term_columns=("fe__bets_cnt__w15m",),
    )
    _, meta = materialize_fe_derived_short_term_parquet_with_cache(
        **base_kwargs,
        short_term_columns=(),
    )
    assert meta["cache_hit"] is True
    assert meta["cache_miss_shards"] == []


def test_p3_t7_registry_add_derivable_feature_primitive_still_hits(
    short_cache_fixture: dict[str, Path],
) -> None:
    """P3-T-7: unchanged primitive request still hits when registry grows elsewhere."""
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    base_kwargs = {
        "cleaned_bet_parquet": short_cache_fixture["cleaned"],
        "training_parquet_for_bet_ids": short_cache_fixture["training"],
        "out_parquet": short_cache_fixture["out"],
        "duckdb_runtime": runtime,
        "canonical_mapping_parquet": short_cache_fixture["mapping"],
        "trial_columns": trial_cols,
        "batch_size": 2000,
        "short_term_columns": ("fe__bets_cnt__w15m",),
    }
    materialize_fe_derived_short_term_parquet_with_cache(**base_kwargs)
    _, meta = materialize_fe_derived_short_term_parquet_with_cache(**base_kwargs)
    assert meta["cache_hit"] is True
    assert meta["cache_hit_ratio"] == 1.0


def test_p3_t7_new_primitive_column_misses_when_not_in_shard(short_cache_fixture: dict[str, Path]) -> None:
    """Adding a new primitive column that is absent from cached shard must rematerialize."""
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    base_kwargs = {
        "cleaned_bet_parquet": short_cache_fixture["cleaned"],
        "training_parquet_for_bet_ids": short_cache_fixture["training"],
        "out_parquet": short_cache_fixture["out"],
        "duckdb_runtime": runtime,
        "canonical_mapping_parquet": short_cache_fixture["mapping"],
        "trial_columns": trial_cols,
        "batch_size": 2000,
    }
    materialize_fe_derived_short_term_parquet_with_cache(
        **base_kwargs,
        short_term_columns=("fe__bets_cnt__w15m",),
    )
    _, meta = materialize_fe_derived_short_term_parquet_with_cache(
        **base_kwargs,
        short_term_columns=("fe__bets_cnt__w15m", "fe__wager_sum__w15m"),
    )
    assert meta["cache_hit"] is False
    assert meta["cache_miss_shards"] == ["202406"]


def test_global_manifest_written(short_cache_fixture: dict[str, Path]) -> None:
    runtime = DuckDbRuntimeConfig()
    materialize_fe_derived_short_term_parquet_with_cache(
        cleaned_bet_parquet=short_cache_fixture["cleaned"],
        training_parquet_for_bet_ids=short_cache_fixture["training"],
        out_parquet=short_cache_fixture["out"],
        duckdb_runtime=runtime,
        canonical_mapping_parquet=short_cache_fixture["mapping"],
        short_term_columns=("fe__bets_cnt__w15m",),
        trial_columns=tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1]),
        batch_size=2000,
    )
    manifest = short_term_pit_cache_root(short_cache_fixture["training"].parent) / "manifest.json"
    assert manifest.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload.get("shard_months") == ["202406"]
    assert payload.get("schema_version") == 2
    assert payload.get("supplier_family") == "short_term:w1h"
