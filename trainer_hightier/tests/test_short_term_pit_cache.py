"""Tests for month-sharded short-term PIT cache reuse."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    SHORT_TERM_TRIAL_BET_COLUMNS,
    STEP35_MISS_PATH_BOUNDED,
    STEP35_MISS_PATH_INDEXED_REPLAY,
)
from trainer_hightier.feature_experiment.short_term_pit_cache import (
    MATERIALIZE_MODE_DELTA_FILL,
    MATERIALIZE_MODE_EXACT_HIT,
    MATERIALIZE_MODE_SUBSET_HIT,
    REASON_ENTITY_DELTA_FILL,
    REASON_FORCE_REFRESH,
    REASON_SUBSET_HIT,
    REASON_UNIVERSE_CHANGED,
    _merge_delta_rows_into_shard,
    _shard_delta_fill_eligible,
    _shard_manifest_path,
    _shard_parquet_path,
    _validate_published_shard,
    compute_shard_universe_fingerprint,
    list_training_payout_months,
    materialize_fe_derived_short_term_parquet_with_cache,
    plan_short_term_pit_cache,
    short_term_pit_cache_root,
)

# Minimal fixtures use bounded DuckDB materializer (indexed replay needs full-month infra).
_STEP35_MISS_KW = {"step35_miss_path": STEP35_MISS_PATH_BOUNDED}


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
        **_STEP35_MISS_KW,
    }
    _, meta1 = materialize_fe_derived_short_term_parquet_with_cache(**kwargs)
    assert meta1["cache_miss_shards"]
    assert meta1["cache_hit"] is False

    _, meta2 = materialize_fe_derived_short_term_parquet_with_cache(**kwargs)
    assert meta2["cache_hit"] is True
    assert meta2["cache_miss_shards"] == []
    assert meta2["cache_hit_ratio"] == 1.0
    assert meta2["short_term_pit_exact_hit_shards"] == ["202406"]
    assert meta2["step35_materializer_by_shard"]["202406"] == MATERIALIZE_MODE_EXACT_HIT
    shard_manifest = json.loads(
        _shard_manifest_path(short_term_pit_cache_root(short_cache_fixture["training"].parent), "202406").read_text(
            encoding="utf-8",
        ),
    )
    assert shard_manifest["materialize_mode"] == MATERIALIZE_MODE_EXACT_HIT


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
        **_STEP35_MISS_KW,
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
        step35_miss_path=STEP35_MISS_PATH_BOUNDED,
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
        step35_miss_path=STEP35_MISS_PATH_BOUNDED,
    )
    plan2 = plan_short_term_pit_cache(
        training_parquet=short_cache_fixture["training"],
        cache_root=cache_root,
        out_columns=out_cols,
        trial_columns=trial_cols,
        canonical_mapping_parquet=short_cache_fixture["mapping"],
        batch_size=2000,
        duckdb_runtime=runtime,
        step35_miss_path=STEP35_MISS_PATH_BOUNDED,
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
        step35_miss_path=STEP35_MISS_PATH_BOUNDED,
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
        **_STEP35_MISS_KW,
    }
    materialize_fe_derived_short_term_parquet_with_cache(**kwargs)
    _, meta2 = materialize_fe_derived_short_term_parquet_with_cache(
        **{**kwargs, "entity_set_fingerprint_sha256_hex": "entity_fp_b" * 4},
    )
    assert meta2["cache_hit"] is True
    assert meta2["cache_miss_shards"] == ["202406"]
    assert meta2["short_term_pit_subset_hit_shards"] == ["202406"]
    assert meta2["cache_reason_counts"].get(REASON_SUBSET_HIT, 0) >= 1
    assert meta2["step35_materializer_by_shard"]["202406"] == MATERIALIZE_MODE_SUBSET_HIT


def test_shard_delta_fill_eligible_without_added_player_ids(
    short_cache_fixture: dict[str, Path],
) -> None:
    """Delta fill can auto-detect missing bets when parent entity fp matches."""
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    out_cols = ("bet_id", *trial_cols, "fe__bets_cnt__w15m")
    cache_root = short_term_pit_cache_root(short_cache_fixture["training"].parent)
    strict_fp = "entity_fp_strict" * 4
    loose_fp = "entity_fp_loose" * 4

    subset_training = short_cache_fixture["training"].parent / "training_strict.parquet"
    _write_training_parquet(subset_training, [_minimal_training_rows()[0]])
    materialize_fe_derived_short_term_parquet_with_cache(
        cleaned_bet_parquet=short_cache_fixture["cleaned"],
        training_parquet_for_bet_ids=subset_training,
        out_parquet=short_cache_fixture["out"],
        duckdb_runtime=runtime,
        canonical_mapping_parquet=short_cache_fixture["mapping"],
        short_term_columns=("fe__bets_cnt__w15m",),
        trial_columns=trial_cols,
        batch_size=2000,
        entity_set_fingerprint_sha256_hex=strict_fp,
        **_STEP35_MISS_KW,
    )

    from trainer_hightier.feature_experiment.short_term_pit_cache import (
        _code_fingerprint,
        _columns_fingerprint,
        _policy_fingerprint,
        _primitive_schema_fingerprint,
        _sha256_file,
    )

    assert _shard_delta_fill_eligible(
        cache_root=cache_root,
        yyyymm="202406",
        previous_entity_set_fp=strict_fp,
        training_parquet=short_cache_fixture["training"],
        duckdb_runtime=runtime,
        code_fp=_code_fingerprint(),
        policy_fp=_policy_fingerprint(batch_size=2000, step35_miss_path=STEP35_MISS_PATH_BOUNDED),
        mapping_sha256=_sha256_file(short_cache_fixture["mapping"]),
        columns_fp=_columns_fingerprint(out_cols),
        primitive_fp=_primitive_schema_fingerprint(trial_cols),
        out_columns=out_cols,
    )
    assert not _shard_delta_fill_eligible(
        cache_root=cache_root,
        yyyymm="202406",
        previous_entity_set_fp=loose_fp,
        training_parquet=short_cache_fixture["training"],
        duckdb_runtime=runtime,
        code_fp=_code_fingerprint(),
        policy_fp=_policy_fingerprint(batch_size=2000, step35_miss_path=STEP35_MISS_PATH_BOUNDED),
        mapping_sha256=_sha256_file(short_cache_fixture["mapping"]),
        columns_fp=_columns_fingerprint(out_cols),
        primitive_fp=_primitive_schema_fingerprint(trial_cols),
        out_columns=out_cols,
    )


def test_short_term_pit_cache_delta_fill_on_looser_universe(
    short_cache_fixture: dict[str, Path],
) -> None:
    """Looser universe (more target bets) extends a stricter shard via delta fill."""
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    strict_fp = "entity_fp_strict" * 4
    loose_fp = "entity_fp_loose" * 4
    kwargs = {
        "cleaned_bet_parquet": short_cache_fixture["cleaned"],
        "out_parquet": short_cache_fixture["out"],
        "duckdb_runtime": runtime,
        "canonical_mapping_parquet": short_cache_fixture["mapping"],
        "short_term_columns": ("fe__bets_cnt__w15m",),
        "trial_columns": trial_cols,
        "batch_size": 2000,
        **_STEP35_MISS_KW,
    }

    subset_training = short_cache_fixture["training"].parent / "training_strict.parquet"
    _write_training_parquet(subset_training, [_minimal_training_rows()[0]])
    materialize_fe_derived_short_term_parquet_with_cache(
        **kwargs,
        training_parquet_for_bet_ids=subset_training,
        entity_set_fingerprint_sha256_hex=strict_fp,
    )

    _, meta = materialize_fe_derived_short_term_parquet_with_cache(
        **kwargs,
        training_parquet_for_bet_ids=short_cache_fixture["training"],
        entity_set_fingerprint_sha256_hex=loose_fp,
        previous_entity_set_fingerprint_sha256_hex=strict_fp,
    )
    assert meta["short_term_pit_delta_fill_shards"] == ["202406"]
    assert meta["short_term_pit_cold_build_shards"] == []
    assert meta["step35_materializer_by_shard"]["202406"] == MATERIALIZE_MODE_DELTA_FILL
    assert meta["cache_hit"] is True
    assert meta["cache_reason_counts"].get(REASON_ENTITY_DELTA_FILL, 0) >= 1

    cache_root = short_term_pit_cache_root(short_cache_fixture["training"].parent)
    shard_manifest = json.loads(_shard_manifest_path(cache_root, "202406").read_text(encoding="utf-8"))
    assert shard_manifest["materialize_mode"] == MATERIALIZE_MODE_DELTA_FILL
    assert shard_manifest["training_universe_num_rows"] == 2
    assert shard_manifest["parent_entity_set_fingerprint"] == strict_fp


def test_validate_published_shard_rejects_duplicate_bet_ids(
    short_cache_fixture: dict[str, Path],
) -> None:
    """TA-WP-3.11: duplicate bet_id rows fail publish validation."""
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    out_cols = ("bet_id", *trial_cols, "fe__bets_cnt__w15m")
    shard_p = short_cache_fixture["training"].parent / "dup_shard.parquet"
    pq.write_table(
        pa.table({"bet_id": [1.0, 1.0, 2.0], "fe__bets_cnt__w15m": [1, 2, 3], trial_cols[0]: [0, 0, 0]}),
        shard_p,
    )
    assert not _validate_published_shard(
        shard_parquet=shard_p,
        training_parquet=short_cache_fixture["training"],
        yyyymm="202406",
        out_columns=out_cols,
        duckdb_runtime=runtime,
    )


def test_subset_reuse_rejects_duplicate_bet_ids_in_parent_shard(
    short_cache_fixture: dict[str, Path],
) -> None:
    """TA-WP-3.11: subset reuse falls back when parent shard has duplicate bet_ids."""
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
        "entity_set_fingerprint_sha256_hex": "entity_fp_loose" * 4,
        **_STEP35_MISS_KW,
    }
    materialize_fe_derived_short_term_parquet_with_cache(**kwargs)

    cache_root = short_term_pit_cache_root(short_cache_fixture["training"].parent)
    shard_p = _shard_parquet_path(cache_root, "202406")
    dup_df = pq.read_table(shard_p).to_pandas()
    dup_df = pd.concat([dup_df, dup_df.iloc[[0]]], ignore_index=True)
    pq.write_table(pa.Table.from_pandas(dup_df), shard_p)

    subset_rows = [_minimal_training_rows()[0]]
    subset_training = short_cache_fixture["training"].parent / "training_subset_dup.parquet"
    _write_training_parquet(subset_training, subset_rows)
    _, meta = materialize_fe_derived_short_term_parquet_with_cache(
        **{
            **kwargs,
            "training_parquet_for_bet_ids": subset_training,
            "entity_set_fingerprint_sha256_hex": "entity_fp_strict" * 4,
        },
    )
    assert meta["short_term_pit_subset_hit_shards"] == []
    assert meta["short_term_pit_cold_build_shards"] == ["202406"]


def _fake_indexed_replay_materialize(**kwargs: object) -> tuple[Path, dict[str, object]]:
    """Write minimal indexed-replay-shaped output for miss-path wiring tests."""
    out = Path(str(kwargs["out_parquet"])).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    trial_col = SHORT_TERM_TRIAL_BET_COLUMNS[0]
    pq.write_table(
        pa.table(
            {
                "bet_id": [1.0, 2.0],
                trial_col: [0, 0],
                "fe__bets_cnt__w15m": [1, 2],
            },
        ),
        out,
    )
    return out, {}


@patch(
    "trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype."
    "materialize_short_term_replay_indexed_prototype",
    side_effect=_fake_indexed_replay_materialize,
)
def test_miss_path_wires_indexed_replay_on_cold_build(
    mock_replay,
    short_cache_fixture: dict[str, Path],
) -> None:
    """TA-WP-3.7: shard miss with indexed replay miss-path calls replay materializer."""
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
        "force_refresh": True,
        "step35_miss_path": STEP35_MISS_PATH_INDEXED_REPLAY,
    }
    _, meta = materialize_fe_derived_short_term_parquet_with_cache(**kwargs)
    mock_replay.assert_called_once()
    assert meta["step35_miss_path"] == STEP35_MISS_PATH_INDEXED_REPLAY
    assert meta["step35_materializer_by_shard"]["202406"] == STEP35_MISS_PATH_INDEXED_REPLAY
    assert meta["short_term_pit_cold_build_shards"] == ["202406"]


@patch(
    "trainer_hightier.feature_experiment.short_term_pit_replay_indexed_prototype."
    "materialize_short_term_replay_indexed_prototype",
)
def test_indexed_replay_failure_fail_fast(
    mock_replay,
    short_cache_fixture: dict[str, Path],
) -> None:
    """TA-WP-3.4/3.7: indexed replay failure propagates without bounded fallback."""
    mock_replay.side_effect = RuntimeError("indexed replay failed")
    runtime = DuckDbRuntimeConfig()
    trial_cols = tuple(SHORT_TERM_TRIAL_BET_COLUMNS[:1])
    with pytest.raises(RuntimeError, match="indexed replay failed"):
        materialize_fe_derived_short_term_parquet_with_cache(
            cleaned_bet_parquet=short_cache_fixture["cleaned"],
            training_parquet_for_bet_ids=short_cache_fixture["training"],
            out_parquet=short_cache_fixture["out"],
            duckdb_runtime=runtime,
            canonical_mapping_parquet=short_cache_fixture["mapping"],
            short_term_columns=("fe__bets_cnt__w15m",),
            trial_columns=trial_cols,
            batch_size=2000,
            force_refresh=True,
            step35_miss_path=STEP35_MISS_PATH_INDEXED_REPLAY,
        )


def test_short_term_pit_cache_subset_reuse_on_stricter_universe(
    short_cache_fixture: dict[str, Path],
) -> None:
    """Stricter universe (fewer target bets) reuses looser shard via subset filter."""
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
        "entity_set_fingerprint_sha256_hex": "entity_fp_loose" * 4,
        **_STEP35_MISS_KW,
    }
    materialize_fe_derived_short_term_parquet_with_cache(**kwargs)

    subset_rows = [_minimal_training_rows()[0]]
    subset_training = short_cache_fixture["training"].parent / "training_subset.parquet"
    _write_training_parquet(subset_training, subset_rows)
    _, meta = materialize_fe_derived_short_term_parquet_with_cache(
        **{
            **kwargs,
            "training_parquet_for_bet_ids": subset_training,
            "entity_set_fingerprint_sha256_hex": "entity_fp_strict" * 4,
        },
    )
    assert meta["short_term_pit_subset_hit_shards"] == ["202406"]
    assert meta["step35_materializer_by_shard"]["202406"] == MATERIALIZE_MODE_SUBSET_HIT
    assert meta["step35_indexed_replay_shard_seconds"] == {}
    assert meta["cache_hit"] is True

    cache_root = short_term_pit_cache_root(short_cache_fixture["training"].parent)
    shard_manifest = json.loads(_shard_manifest_path(cache_root, "202406").read_text(encoding="utf-8"))
    assert shard_manifest["materialize_mode"] == MATERIALIZE_MODE_SUBSET_HIT
    assert shard_manifest["training_universe_num_rows"] == 1
    assert shard_manifest["parent_entity_set_fingerprint"] == "entity_fp_loose" * 4


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
        **_STEP35_MISS_KW,
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
        **_STEP35_MISS_KW,
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
        **_STEP35_MISS_KW,
    )
    manifest = short_term_pit_cache_root(short_cache_fixture["training"].parent) / "manifest.json"
    assert manifest.is_file()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload.get("shard_months") == ["202406"]
    assert payload.get("schema_version") == 2
    assert payload.get("supplier_family") == "short_term:w1h"
