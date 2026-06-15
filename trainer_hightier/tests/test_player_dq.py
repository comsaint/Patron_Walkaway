"""Tests for player-level DQ artifacts and policy fingerprints."""

from __future__ import annotations

import importlib
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import (
    BetPreprocessConfig,
    DuckDbRuntimeConfig,
    PlayerDqConfig,
    TRAINING_DATA_SCOPE_TEST_UNBOUNDED,
)
from trainer_hightier.tests.test_bet_preprocess import _bet_row, read_cleaned_bet_dataset
from trainer_hightier.utils.bet_l0_preprocess import default_preprocess_registry_yaml_path
from trainer_hightier.utils.entity_set_v1 import (
    entity_set_policy_fingerprint_sha256_hex,
    materialize_entity_set_v1_cached,
)
from trainer_hightier.utils.bet_l0_preprocess import segment_cleaned_bet_from_base_parquet
from trainer_hightier.utils.player_dq import (
    flags_policy_fingerprint_sha256_hex,
    hard_exclude_anti_join_sql,
    hard_exclude_policy_fingerprint_sha256_hex,
    materialize_player_dq_cached,
    player_dq_cache_is_hit,
)
from trainer_hightier.utils.universe_cache_v1 import selected_universe_membership_fingerprint

_hpre = importlib.import_module("trainer_hightier.02_preprocess")


def _write_rank(path: Path, *, player_ids: list[int]) -> None:
    n = len(player_ids)
    pq.write_table(
        pa.table(
            {
                "canonical_id": [f"c{i}" for i in player_ids],
                "player_id": player_ids,
                "adt": [float(n - i) for i in range(n)],
                "adt_rank": list(range(1, n + 1)),
                "adt_percentile": [1.0 - i / max(n, 1) for i in range(n)],
                "has_slow_window_coverage": [True] * n,
            }
        ),
        path,
    )


def _preprocess_bets(raw: Path, out: Path) -> None:
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(
            data_scope=TRAINING_DATA_SCOPE_TEST_UNBOUNDED,
            preprocess_registry_yaml=default_preprocess_registry_yaml_path(),
            dedup_hash_buckets=1,
        ),
    )


def test_hard_policy_empty_when_disabled() -> None:
    cfg = PlayerDqConfig(enabled=False)
    assert hard_exclude_policy_fingerprint_sha256_hex(cfg) == ""


def test_review_threshold_does_not_change_hard_fingerprint() -> None:
    base = PlayerDqConfig(review_distinct_game_id_per_hour=120)
    changed = PlayerDqConfig(review_distinct_game_id_per_hour=100)
    assert hard_exclude_policy_fingerprint_sha256_hex(base) == hard_exclude_policy_fingerprint_sha256_hex(
        changed,
    )
    assert flags_policy_fingerprint_sha256_hex(base) != flags_policy_fingerprint_sha256_hex(changed)


def test_materialize_known_test_and_pace_flags(tmp_path: Path) -> None:
    t0 = pd.Timestamp("2025-05-27 09:00:00")
    rows = [_bet_row(bet_id=1, player_id=100, game_id=1, payout_complete_dtm=t0)]
    for gid in range(2, 243):
        rows.append(
            _bet_row(
                bet_id=gid,
                player_id=200,
                game_id=gid,
                payout_complete_dtm=t0 + pd.Timedelta(seconds=gid % 60),
            ),
        )
    for gid in range(243, 364):
        rows.append(
            _bet_row(
                bet_id=gid,
                player_id=300,
                game_id=gid,
                payout_complete_dtm=t0 + pd.Timedelta(seconds=gid % 60),
            ),
        )
    raw = tmp_path / "raw.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), raw)
    base = tmp_path / "base"
    _preprocess_bets(raw, base)
    cmap = tmp_path / "map.parquet"
    pq.write_table(
        pa.table(
            {
                "player_id": [100, 200, 300],
                "canonical_id": ["c100", "c200", "c300"],
                "casino_player_id": ["44440101", "vip1", "vip2"],
            }
        ),
        cmap,
    )
    dq_dir = tmp_path / "dq"
    meta = materialize_player_dq_cached(
        bet_base_parquet=base,
        canonical_mapping_parquet=cmap,
        cfg=PlayerDqConfig(artifacts_dir=dq_dir),
        duckdb_runtime=DuckDbRuntimeConfig(),
        use_cache=False,
    )
    assert meta["player_dq_hard_player_count"] == 2
    assert meta["player_dq_review_player_count"] == 1
    assert meta["player_dq_known_test_player_count"] == 1
    hard = pq.read_table(dq_dir / "player_dq_hard_exclude.parquet").to_pandas()
    assert set(hard["player_id"].astype(int).tolist()) == {100, 200}


def test_entity_set_excludes_hard_players(tmp_path: Path) -> None:
    t0 = pd.Timestamp("2025-05-27 09:00:00")
    rows = [
        _bet_row(bet_id=1, player_id=100, payout_complete_dtm=t0),
        _bet_row(bet_id=2, player_id=200, payout_complete_dtm=t0),
    ]
    raw = tmp_path / "raw.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), raw)
    base = tmp_path / "base"
    _preprocess_bets(raw, base)
    cmap = tmp_path / "map.parquet"
    pq.write_table(
        pa.table(
            {
                "player_id": [100, 200],
                "canonical_id": ["c100", "c200"],
                "casino_player_id": ["44440101", "vip"],
            }
        ),
        cmap,
    )
    rank_p = tmp_path / "rank.parquet"
    pq.write_table(
        pa.table(
            {
                "canonical_id": ["c100", "c200"],
                "player_id": [100, 200],
                "adt": [2.0, 1.0],
                "adt_rank": [1, 2],
                "adt_percentile": [1.0, 1.0],
                "has_slow_window_coverage": [True, True],
            }
        ),
        rank_p,
    )
    dq_dir = tmp_path / "dq"
    dq_meta = materialize_player_dq_cached(
        bet_base_parquet=base,
        canonical_mapping_parquet=cmap,
        cfg=PlayerDqConfig(artifacts_dir=dq_dir),
        duckdb_runtime=DuckDbRuntimeConfig(),
        use_cache=False,
    )
    out = tmp_path / "seg"
    rank_fp = selected_universe_membership_fingerprint(rank_p, quantile=0.01)
    hard_fp = str(dq_meta["hard_exclude_policy_fingerprint_sha256_hex"])
    materialize_entity_set_v1_cached(
        base_cleaned_parquet=base,
        rank_table_path=rank_p,
        selected_universe_fingerprint_sha256_hex=rank_fp,
        selected_quantile=0.01,
        training_scope=TRAINING_DATA_SCOPE_TEST_UNBOUNDED,
        source_manifest_v2_fingerprint_sha256_hex="abc",
        duckdb_runtime=DuckDbRuntimeConfig(),
        cache_root=tmp_path / "cache",
        output_parquet=out,
        use_cache=False,
        hard_exclude_parquet=Path(str(dq_meta["player_dq_hard_exclude_parquet"])),
        hard_exclude_policy_fingerprint_sha256_hex=hard_fp,
    )
    got = read_cleaned_bet_dataset(out)
    assert set(got["player_id"].astype(int).tolist()) == {200}


def test_entity_set_fingerprint_unchanged_when_disabled() -> None:
    fp_old = entity_set_policy_fingerprint_sha256_hex(
        selected_quantile=0.95,
        selected_universe_fingerprint_sha256_hex="u" * 64,
        source_manifest_v2_fingerprint_sha256_hex="s" * 64,
        bet_base_fingerprint_sha256_hex="b" * 64,
        training_scope_fingerprint_sha256_hex="t" * 64,
    )
    fp_disabled = entity_set_policy_fingerprint_sha256_hex(
        selected_quantile=0.95,
        selected_universe_fingerprint_sha256_hex="u" * 64,
        source_manifest_v2_fingerprint_sha256_hex="s" * 64,
        bet_base_fingerprint_sha256_hex="b" * 64,
        training_scope_fingerprint_sha256_hex="t" * 64,
        hard_exclude_policy_fingerprint_sha256_hex="",
    )
    assert fp_old == fp_disabled


def test_materialize_player_dq_disabled_skips_artifacts(tmp_path: Path) -> None:
    """Disabled DQ must short-circuit without writing parquet sidecars."""

    meta = materialize_player_dq_cached(
        bet_base_parquet=tmp_path / "missing_base.parquet",
        canonical_mapping_parquet=tmp_path / "missing_map.parquet",
        cfg=PlayerDqConfig(enabled=False, artifacts_dir=tmp_path / "dq"),
        duckdb_runtime=DuckDbRuntimeConfig(),
        use_cache=False,
    )
    assert meta["player_dq_enabled"] is False
    assert meta["player_dq_cache_hit"] is False
    assert meta["player_dq_hard_exclude_parquet"] is None
    assert meta["player_dq_flags_parquet"] is None
    assert meta["hard_exclude_policy_fingerprint_sha256_hex"] == ""


def test_player_dq_cache_hit_on_second_run(tmp_path: Path) -> None:
    """Manifest fingerprints should make the second materialize a cache hit."""

    t0 = pd.Timestamp("2025-05-27 09:00:00")
    rows = [
        _bet_row(bet_id=1, player_id=100, game_id=1, payout_complete_dtm=t0),
        _bet_row(bet_id=2, player_id=200, game_id=2, payout_complete_dtm=t0),
    ]
    raw = tmp_path / "raw.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), raw)
    base = tmp_path / "base"
    _preprocess_bets(raw, base)
    cmap = tmp_path / "map.parquet"
    pq.write_table(
        pa.table(
            {
                "player_id": [100, 200],
                "canonical_id": ["c100", "c200"],
                "casino_player_id": ["44440101", "vip"],
            }
        ),
        cmap,
    )
    dq_dir = tmp_path / "dq"
    cfg = PlayerDqConfig(artifacts_dir=dq_dir)
    first = materialize_player_dq_cached(
        bet_base_parquet=base,
        canonical_mapping_parquet=cmap,
        cfg=cfg,
        duckdb_runtime=DuckDbRuntimeConfig(),
        use_cache=False,
    )
    assert first["player_dq_cache_hit"] is False
    second = materialize_player_dq_cached(
        bet_base_parquet=base,
        canonical_mapping_parquet=cmap,
        cfg=cfg,
        duckdb_runtime=DuckDbRuntimeConfig(),
        use_cache=True,
    )
    assert second["player_dq_cache_hit"] is True
    assert second["player_dq_hard_player_count"] == first["player_dq_hard_player_count"]


def test_hard_exclude_anti_join_sql_filters_players(tmp_path: Path) -> None:
    """ADT segment projection must honor hard-DQ anti-join semantics."""

    hard_p = tmp_path / "hard.parquet"
    pq.write_table(pa.table({"player_id": [100]}), hard_p)
    hard_esc = str(hard_p.resolve()).replace("'", "''")
    clause = hard_exclude_anti_join_sql(hard_exclude_parquet_esc=hard_esc)
    bets = pd.DataFrame({"player_id": [100, 200], "wager": [1.0, 2.0]})
    con = duckdb.connect(database=":memory:")
    try:
        con.register("bets", bets)
        got = con.execute(f"SELECT player_id FROM bets AS b WHERE TRUE{clause}").fetchall()
    finally:
        con.close()
    assert [int(row[0]) for row in got] == [200]


def test_day_pace_hard_dq_excludes_player(tmp_path: Path) -> None:
    """Daily distinct-game pace can hard-DQ even when hourly pace stays below threshold."""

    t0 = pd.Timestamp("2025-05-27 09:00:00")
    rows = [_bet_row(bet_id=1, player_id=500, game_id=1, payout_complete_dtm=t0)]
    for gid in range(2, 52):
        rows.append(
            _bet_row(
                bet_id=gid,
                player_id=500,
                game_id=gid,
                payout_complete_dtm=t0 + pd.Timedelta(minutes=gid % 30),
            ),
        )
    raw = tmp_path / "raw.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), raw)
    base = tmp_path / "base"
    _preprocess_bets(raw, base)
    cmap = tmp_path / "map.parquet"
    pq.write_table(
        pa.table(
            {
                "player_id": [500],
                "canonical_id": ["c500"],
                "casino_player_id": ["vip500"],
            }
        ),
        cmap,
    )
    dq_dir = tmp_path / "dq"
    meta = materialize_player_dq_cached(
        bet_base_parquet=base,
        canonical_mapping_parquet=cmap,
        cfg=PlayerDqConfig(
            artifacts_dir=dq_dir,
            hard_distinct_game_id_per_hour=9999,
            hard_distinct_game_id_per_day=50,
            review_distinct_game_id_per_hour=9999,
            review_distinct_game_id_per_day=40,
        ),
        duckdb_runtime=DuckDbRuntimeConfig(),
        use_cache=False,
    )
    assert meta["player_dq_hard_player_count"] == 1
    hard = pq.read_table(dq_dir / "player_dq_hard_exclude.parquet").to_pandas()
    assert set(hard["player_id"].astype(int).tolist()) == {500}


def test_segment_cleaned_bet_applies_hard_exclude(tmp_path: Path) -> None:
    """Segment projection from base must drop hard-DQ players alongside ADT allowlist."""

    t0 = pd.Timestamp("2025-05-27 09:00:00")
    rows = [
        _bet_row(bet_id=1, player_id=100, payout_complete_dtm=t0),
        _bet_row(bet_id=2, player_id=200, payout_complete_dtm=t0),
    ]
    raw = tmp_path / "raw.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), raw)
    base = tmp_path / "base"
    _preprocess_bets(raw, base)
    allow = tmp_path / "allow.parquet"
    pq.write_table(pa.table({"player_id": [100, 200]}), allow)
    hard = tmp_path / "hard.parquet"
    pq.write_table(pa.table({"player_id": [100]}), hard)
    out = tmp_path / "segmented"
    segment_cleaned_bet_from_base_parquet(
        base,
        allow,
        out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        hard_exclude_parquet=hard,
    )
    got = read_cleaned_bet_dataset(out)
    assert set(got["player_id"].astype(int).tolist()) == {200}


def test_player_dq_cache_is_hit_false_when_manifest_missing(tmp_path: Path) -> None:
    """Cache probe must fail closed when artifacts are absent."""

    assert (
        player_dq_cache_is_hit(
            artifacts_dir=tmp_path / "dq",
            bet_base_fingerprint_sha256_hex="a" * 64,
            canonical_mapping_sha256_hex="b" * 64,
            flags_policy_fingerprint="c" * 64,
        )
        is False
    )
