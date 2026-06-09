"""L0 ``t_bet`` → cleaned parquet (DQ, registry synthetic, dedup)."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.bet_contract import BET_INGEST_READ_COLS_ORDERED

from trainer_hightier.config import (
    BetPreprocessConfig,
    DuckDbRuntimeConfig,
    L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
)
from trainer_hightier.preprocess_bet_fix_registry import (
    load_preprocess_bet_ingestion_fix_registry,
    resolve_bet_ingest_fix004_cap_binding,
)
from trainer_hightier.utils.bet_l0_preprocess import (
    _consolidate_staged_bucket_partition_dirs,
    _partitioned_parquet_footer_row_count,
    default_preprocess_registry_yaml_path,
)
from trainer_hightier.utils.patron_session_metrics import materialize_adt_allowed_players_parquet

_hpre = importlib.import_module("trainer_hightier.02_preprocess")
_DEFAULT_REGISTRY = default_preprocess_registry_yaml_path()


def read_cleaned_bet_dataset(path: Path | str) -> pd.DataFrame:
    """Load trainer bet preprocess output (Hive-partitioned dir or legacy single Parquet)."""

    root = Path(path).resolve()
    if root.is_file():
        return pd.read_parquet(root)
    shards = sorted(root.rglob("*.parquet"))
    if not shards:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(p) for p in shards], ignore_index=True)


def _bet_row(**kwargs: object) -> dict[str, object]:
    """Minimal full-contract bet row aligned with `_BET_INGEST_READ_COLS_ORDERED`."""
    pay_dtm = kwargs["payout_complete_dtm"]
    merged: dict[str, object] = dict(
        bet_id=1,
        session_id=10,
        player_id=100,
        game_id=1,
        table_id=2,
        payout_complete_dtm=pay_dtm,
        __etl_insert_Dtm=kwargs.get("__etl_insert_Dtm", pay_dtm),
        wager=1.0,
        wager_nn=0.0,
        status="WIN",
        casino_win=0.0,
        payout_odds=1.0,
        payout_ha=0.05,
        base_ha=1.0,
        is_back_bet=0,
        position_idx=1,
        position_code="PLAYER_03",
        position_label="3",
        bet_type="BANKER",
        type_of_bet="MAIN_BET",
        commission=0.0,
        max_wager=100000.0,
        std_dev=1.0,
        theo_win=10.0,
        theo_win_cash=10.0,
        true_odds=2.0,
        adjusted_theo_win=10.0,
        is_settled=1,
        bet_payout_type="",
        mixed_stack=0,
        auto_resolve_stack=1,
        __ts_ms=1,
        __op="c",
        __deleted="False",
    )
    merged.update(kwargs)
    return {c: merged[c] for c in BET_INGEST_READ_COLS_ORDERED}


@pytest.fixture
def registry_path() -> Path:
    if not _DEFAULT_REGISTRY.is_file():
        pytest.skip(f"registry missing {_DEFAULT_REGISTRY}")
    return _DEFAULT_REGISTRY


@pytest.fixture
def cap_sec(registry_path: Path) -> int:
    doc = load_preprocess_bet_ingestion_fix_registry(registry_path.resolve())
    cap, _, _, _ = resolve_bet_ingest_fix004_cap_binding(doc)
    return int(cap)


def test_preprocess_bet_dedup_keeps_latest_synthetic(registry_path: Path, tmp_path) -> None:
    t_pay = pd.Timestamp("2025-05-27 18:00:00")
    t_old = pd.Timestamp("2025-05-27 18:01:00")
    t_new = pd.Timestamp("2025-05-27 18:02:00")
    df = pd.DataFrame(
        [
            _bet_row(bet_id=7, payout_complete_dtm=t_pay, __etl_insert_Dtm=t_old),
            _bet_row(bet_id=7, payout_complete_dtm=t_pay, __etl_insert_Dtm=t_new),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned_ds"
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,preprocess_registry_yaml=registry_path),
    )
    got = read_cleaned_bet_dataset(out)
    assert len(got) == 1
    assert int(got.iloc[0]["bet_id"]) == 7


def test_preprocess_bet_synthetic_caps_after_event(registry_path: Path, cap_sec: int, tmp_path) -> None:
    t_pay = pd.Timestamp("2025-06-01 12:00:00")
    t_etl_far = pd.Timestamp("2025-06-03 08:00:00")
    df = pd.DataFrame(
        [
            _bet_row(
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_etl_far,
            )
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned_ds"
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,preprocess_registry_yaml=registry_path),
    )
    got = read_cleaned_bet_dataset(out)
    assert len(got) == 1
    expected = pd.Timestamp(t_pay) + pd.Timedelta(seconds=cap_sec)
    actual = pd.to_datetime(got.iloc[0]["__etl_insert_Dtm_synthetic"], utc=False)
    delta_s = abs((actual - expected).total_seconds())
    assert delta_s < 2


def test_preprocess_bet_prediction_visible_ts_cf(registry_path: Path, tmp_path) -> None:
    """``prediction_visible_ts_cf`` matches DuckDB ceil-on-epoch formula in preprocess."""
    from trainer_hightier.config import BET_AVAIL_DELAY_MIN, SCORER_POLL_INTERVAL_SECONDS

    t_pay = pd.Timestamp("2025-06-01 12:00:00")
    df = pd.DataFrame(
        [_bet_row(payout_complete_dtm=t_pay, gaming_day=t_pay.date(), __etl_insert_Dtm=t_pay)]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned_ds"
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
            preprocess_registry_yaml=registry_path,
            dedup_hash_buckets=1,
        ),
    )
    got = read_cleaned_bet_dataset(out)
    assert len(got) == 1
    assert "prediction_visible_ts_cf" in got.columns
    pcd = got.iloc[0]["payout_complete_dtm"]
    syn = got.iloc[0]["__etl_insert_Dtm_synthetic"]
    adm = int(BET_AVAIL_DELAY_MIN)
    poll = int(SCORER_POLL_INTERVAL_SECONDS)
    row = duckdb.sql(
        f"""
        SELECT to_timestamp(
          ceil(
            epoch(
              GREATEST(
                COALESCE(?::TIMESTAMP, ?::TIMESTAMP + INTERVAL {adm} MINUTE),
                ?::TIMESTAMP + INTERVAL {adm} MINUTE
              )
            ) / {poll}::DOUBLE
          ) * {poll}
        ) AS pv
        """,
        params=[syn, pcd, pcd],
    ).fetchone()
    assert row is not None
    exp = row[0]
    pv_raw = got.iloc[0]["prediction_visible_ts_cf"]
    pv = pd.Timestamp(pv_raw)
    exp_ts = pd.Timestamp(exp)
    assert abs((pv - exp_ts).total_seconds()) < 2


def test_preprocess_bet_drops_zero_wager(registry_path: Path, tmp_path) -> None:
    t0 = pd.Timestamp("2025-06-02 09:00:00")
    df = pd.DataFrame(
        [
            _bet_row(
                payout_complete_dtm=t0,
                gaming_day=t0.date(),
                __etl_insert_Dtm=t0,
                wager=0,
            ),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned_ds"
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,preprocess_registry_yaml=registry_path),
    )
    got = read_cleaned_bet_dataset(out)
    assert len(got) == 0


def test_bulk_episode_day_tags(registry_path: Path, tmp_path) -> None:
    """Rows with synthetic observed calendar day 2025-05-27 get ingestion_episode_id from registry."""
    pay = pd.Timestamp("2025-05-27 10:30:00")
    etl = pd.Timestamp("2025-05-27 14:30:00")
    df = pd.DataFrame([_bet_row(payout_complete_dtm=pay, gaming_day=pay.date(), __etl_insert_Dtm=etl)])
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned_ds"
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,preprocess_registry_yaml=registry_path),
    )
    got = read_cleaned_bet_dataset(out)
    assert len(got) == 1
    assert got.iloc[0]["ingestion_episode_id"] == "BET-BULK-INGEST-2025-05-27"


def test_preprocess_bet_hash_buckets_matches_single_pass(registry_path: Path, tmp_path) -> None:
    """Hash-bucketed dedup must match single-pass (same survivor per bet_id)."""
    base = pd.Timestamp("2025-05-27 09:00:00")
    rows: list[dict[str, object]] = []
    for i in range(1, 11):
        pay = base + pd.Timedelta(minutes=i)
        rows.append(
            _bet_row(
                bet_id=i,
                payout_complete_dtm=pay,
                gaming_day=pay.date(),
                __etl_insert_Dtm=pay,
            )
        )
    pay5 = base + pd.Timedelta(minutes=5)
    rows.append(
        _bet_row(
            bet_id=5,
            payout_complete_dtm=pay5,
            gaming_day=pay5.date(),
            __etl_insert_Dtm=pay5 + pd.Timedelta(hours=3),
        )
    )
    df = pd.DataFrame(rows)
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out1 = tmp_path / "cleaned_b1_ds"
    out8 = tmp_path / "cleaned_b8_ds"
    _, b1 = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out1,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
            preprocess_registry_yaml=registry_path,
            dedup_hash_buckets=1,
        ),
    )
    _, b8 = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out8,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
            preprocess_registry_yaml=registry_path,
            dedup_hash_buckets=8,
        ),
    )
    assert b1 == 1 and b8 == 8
    g1 = read_cleaned_bet_dataset(out1).sort_values(["bet_id"]).reset_index(drop=True)
    g8 = read_cleaned_bet_dataset(out8).sort_values(["bet_id"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(g1, g8)


def test_preprocess_bet_dedup_prefers_newer_raw_etl_when_synthetic_tied(
    registry_path: Path, cap_sec: int, tmp_path
) -> None:
    """When synthetic + payout tie, ORDER BY raw ``__etl_insert_Dtm`` breaks ties."""
    t_pay = pd.Timestamp("2025-06-01 12:00:00")
    t_cap = t_pay + pd.Timedelta(seconds=cap_sec)
    t_etl_early = t_cap + pd.Timedelta(hours=1)
    t_etl_late = t_cap + pd.Timedelta(hours=2)
    df = pd.DataFrame(
        [
            _bet_row(
                bet_id=42,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_etl_early,
                __ts_ms=1,
            ),
            _bet_row(
                bet_id=42,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_etl_late,
                __ts_ms=2,
            ),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned_ds"
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,preprocess_registry_yaml=registry_path, dedup_hash_buckets=3),
    )
    got = read_cleaned_bet_dataset(out)
    assert len(got) == 1
    assert pd.to_datetime(got.iloc[0]["__etl_insert_Dtm"], utc=False) == t_etl_late


def test_preprocess_bet_adt_segment_keeps_only_top_quantile_patrons(registry_path: Path, tmp_path) -> None:
    """Patrons below patron-profile ADT ``quantile_cont`` cutoff drop before heavy bet pipeline."""
    profile_csv = tmp_path / "canonical_patron_profile.csv"
    mapping_pq = tmp_path / "canonical_mapping.parquet"
    prof_rows = [{"canonical_id": f"c{i}", "adt": float(i)} for i in range(1, 51)]
    prof_rows.append({"canonical_id": "vip", "adt": 1_000_000.0})
    pd.DataFrame(prof_rows).to_csv(profile_csv, index=False)
    pd.DataFrame(
        [
            {"player_id": 100, "canonical_id": "vip"},
            {"player_id": 200, "canonical_id": "c1"},
        ]
    ).to_parquet(mapping_pq)
    allowed_pq = tmp_path / "adt_allowed.parquet"
    materialize_adt_allowed_players_parquet(
        profile_csv,
        mapping_pq,
        quantile=0.99,
        duckdb_runtime=DuckDbRuntimeConfig(),
        output_parquet=allowed_pq,
    )
    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    df = pd.DataFrame(
        [
            _bet_row(
                bet_id=1,
                player_id=100,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_pay,
            ),
            _bet_row(
                bet_id=2,
                player_id=200,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_pay,
            ),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned_ds"
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
            preprocess_registry_yaml=registry_path,
            adt_filter_quantile=0.99,
            patron_profile_csv=profile_csv,
            canonical_mapping_parquet=mapping_pq,
            adt_allowed_players_parquet=allowed_pq,
        ),
    )
    got = read_cleaned_bet_dataset(out)
    assert len(got) == 1
    assert int(got.iloc[0]["player_id"]) == 100


def test_adt_allowlist_fingerprint_stable_reorder_dup_and_trunc(tmp_path: Path) -> None:
    """Content hash matches DuckDB DISTINCT BIGINT semantics (order / duplicates / float vs int)."""
    from trainer_hightier.utils.bet_l0_preprocess import _adt_allowlist_distinct_player_ids_fingerprint

    p1 = tmp_path / "al1.parquet"
    pd.DataFrame({"player_id": [3, 1, 3, 2]}).to_parquet(p1, index=False)
    h1, n1 = _adt_allowlist_distinct_player_ids_fingerprint(p1)
    p2 = tmp_path / "al2.parquet"
    pd.DataFrame({"player_id": [2, 3, 1]}).to_parquet(p2, index=False)
    h2, n2 = _adt_allowlist_distinct_player_ids_fingerprint(p2)
    assert h1 == h2
    assert n1 == n2 == 3

    p3 = tmp_path / "al3.parquet"
    pd.DataFrame({"player_id": [42.0, 42]}).to_parquet(p3, index=False)
    h3, n3 = _adt_allowlist_distinct_player_ids_fingerprint(p3)
    p_ref = tmp_path / "al_ref.parquet"
    pd.DataFrame({"player_id": [42]}).to_parquet(p_ref, index=False)
    href, nref = _adt_allowlist_distinct_player_ids_fingerprint(p_ref)
    assert n3 == nref == 1
    assert h3 == href


def test_build_bet_clean_adt_record_matches_when_only_profile_csv_differs(
    registry_path: Path, tmp_path: Path
) -> None:
    """ADT bet-cache key uses allowlist player_id set; profile CSV path/content may differ."""
    profile_a = tmp_path / "profile_a.csv"
    profile_b = tmp_path / "profile_b.csv"
    pd.DataFrame([{"canonical_id": "x", "adt": 1.0}]).to_csv(profile_a, index=False)
    pd.DataFrame([{"canonical_id": "y", "adt": 9.0}]).to_csv(profile_b, index=False)
    allowed = tmp_path / "allowed.parquet"
    pd.DataFrame({"player_id": [100, 200]}).to_parquet(allowed, index=False)
    raw = tmp_path / "gmwds_t_bet.parquet"
    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    _bet_row(
                        bet_id=1,
                        player_id=100,
                        payout_complete_dtm=t_pay,
                        gaming_day=t_pay.date(),
                        __etl_insert_Dtm=t_pay,
                    ),
                ]
            )
        ),
        raw,
    )
    rec_a = _hpre.build_bet_clean_cache_record(
        raw,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=2,
        cleaned_session_parquet=None,
        adt_filter_quantile=0.99,
        patron_profile_csv=profile_a,
        canonical_mapping_parquet=None,
        adt_allowed_players_parquet=allowed,
    )
    rec_b = _hpre.build_bet_clean_cache_record(
        raw,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=2,
        cleaned_session_parquet=None,
        adt_filter_quantile=0.99,
        patron_profile_csv=profile_b,
        canonical_mapping_parquet=None,
        adt_allowed_players_parquet=allowed,
    )
    assert rec_a == rec_b
    assert rec_a["adt_segment"]["adt_allowlist_distinct_player_id_count"] == 2


def test_bet_clean_cache_hit_legacy_cleaned_session_dependency_ignored(
    registry_path: Path, tmp_path: Path
) -> None:
    """Sidecars from older runs may include cleaned_session_dependency; compare ignores it.

    Cache hit must not require cleaned_session.parquet on disk when semantic keys match.
    """
    import trainer_hightier.utils.bet_l0_preprocess as bl0

    allowed = tmp_path / "allowed.parquet"
    pd.DataFrame({"player_id": [100, 200]}).to_parquet(allowed, index=False)
    raw = tmp_path / "gmwds_t_bet.parquet"
    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    _bet_row(
                        bet_id=1,
                        player_id=100,
                        payout_complete_dtm=t_pay,
                        gaming_day=t_pay.date(),
                        __etl_insert_Dtm=t_pay,
                    ),
                ]
            )
        ),
        raw,
    )
    rec_cur = bl0.build_bet_clean_cache_record(
        raw,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=2,
        adt_filter_quantile=0.99,
        adt_allowed_players_parquet=allowed,
    )
    legacy = dict(rec_cur)
    legacy["manifest_version"] = 8
    legacy["cleaned_session_dependency"] = {
        "path": str(tmp_path / "ghost_session.parquet"),
        "mtime_ns": 1,
        "size_bytes": 2,
        "num_rows": 3,
    }

    cleaned_root = tmp_path / "cleaned_seg"
    cleaned_root.mkdir()
    stub = cleaned_root / "shard.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame([{"bet_id": 1.0}])), stub)
    mp = bl0.bet_clean_cache_manifest_path(cleaned_root)
    mp.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")

    missing_session = tmp_path / "no_such_cleaned_session.parquet"
    assert not missing_session.is_file()
    assert bl0.bet_clean_cache_is_hit(
        raw,
        cleaned_root,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=2,
        cleaned_session_parquet=missing_session,
        adt_filter_quantile=0.99,
        adt_allowed_players_parquet=allowed,
    )


def test_bet_clean_cache_miss_when_allowlist_player_id_set_changes(
    registry_path: Path, tmp_path: Path
) -> None:
    import trainer_hightier.utils.bet_l0_preprocess as bl0

    allowed_a = tmp_path / "allowed_a.parquet"
    allowed_b = tmp_path / "allowed_b.parquet"
    pd.DataFrame({"player_id": [100]}).to_parquet(allowed_a, index=False)
    pd.DataFrame({"player_id": [200]}).to_parquet(allowed_b, index=False)
    raw = tmp_path / "gmwds_t_bet.parquet"
    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    _bet_row(
                        bet_id=1,
                        player_id=100,
                        payout_complete_dtm=t_pay,
                        gaming_day=t_pay.date(),
                        __etl_insert_Dtm=t_pay,
                    ),
                ]
            )
        ),
        raw,
    )
    rec_a = bl0.build_bet_clean_cache_record(
        raw,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=2,
        adt_filter_quantile=0.99,
        adt_allowed_players_parquet=allowed_a,
    )
    cleaned_root = tmp_path / "cleaned_seg"
    cleaned_root.mkdir()
    stub = cleaned_root / "shard.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame([{"bet_id": 1.0}])), stub)
    mp = bl0.bet_clean_cache_manifest_path(cleaned_root)
    mp.write_text(json.dumps(rec_a, sort_keys=True), encoding="utf-8")

    assert not bl0.bet_clean_cache_is_hit(
        raw,
        cleaned_root,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=2,
        adt_filter_quantile=0.99,
        adt_allowed_players_parquet=allowed_b,
    )


def test_bet_clean_cache_miss_when_source_bet_row_stat_differs(
    registry_path: Path, tmp_path: Path
) -> None:
    import trainer_hightier.utils.bet_l0_preprocess as bl0

    allowed = tmp_path / "allowed.parquet"
    pd.DataFrame({"player_id": [100]}).to_parquet(allowed, index=False)
    raw = tmp_path / "gmwds_t_bet.parquet"
    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    _bet_row(
                        bet_id=1,
                        player_id=100,
                        payout_complete_dtm=t_pay,
                        gaming_day=t_pay.date(),
                        __etl_insert_Dtm=t_pay,
                    ),
                ]
            )
        ),
        raw,
    )
    rec_ok = bl0.build_bet_clean_cache_record(
        raw,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=2,
        adt_filter_quantile=0.99,
        adt_allowed_players_parquet=allowed,
    )
    stale = dict(rec_ok)
    stale_sb = dict(stale["source_bet"])
    stale_sb["num_rows"] = int(stale_sb["num_rows"]) + 999_999
    stale["source_bet"] = stale_sb

    cleaned_root = tmp_path / "cleaned_seg"
    cleaned_root.mkdir()
    stub = cleaned_root / "shard.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame([{"bet_id": 1.0}])), stub)
    mp = bl0.bet_clean_cache_manifest_path(cleaned_root)
    mp.write_text(json.dumps(stale, sort_keys=True), encoding="utf-8")

    assert not bl0.bet_clean_cache_is_hit(
        raw,
        cleaned_root,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=2,
        adt_filter_quantile=0.99,
        adt_allowed_players_parquet=allowed,
    )


def test_trial_bet_behavior_1h_window_counts(tmp_path: Path) -> None:
    """1h RANGE window: prior rows with pcd in [current_pcd - 1h, current_pcd)."""
    from trainer_hightier.utils.trial_bet_behavior_1h import materialize_trial_bet_behavior_1h

    t0 = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
    pit = t0 + pd.Timedelta(hours=3)
    df = pd.DataFrame(
        [
            {
                "bet_id": 1.0,
                "player_id": 1,
                "payout_complete_dtm": t0,
                "wager": 10.0,
                "is_back_bet": 1,
                "payout_odds": 2.0,
                "prediction_visible_ts_cf": pit,
                "__etl_insert_Dtm_synthetic": pit,
            },
            {
                "bet_id": 2.0,
                "player_id": 1,
                "payout_complete_dtm": t0 + pd.Timedelta(minutes=30),
                "wager": 20.0,
                "is_back_bet": 0,
                "payout_odds": 3.0,
                "prediction_visible_ts_cf": pit,
                "__etl_insert_Dtm_synthetic": pit,
            },
            {
                "bet_id": 3.0,
                "player_id": 1,
                "payout_complete_dtm": t0 + pd.Timedelta(minutes=45),
                "wager": 99.0,
                "is_back_bet": 0,
                "payout_odds": 9.0,
                "prediction_visible_ts_cf": pit,
                "__etl_insert_Dtm_synthetic": pit,
            },
            {
                "bet_id": 4.0,
                "player_id": 1,
                "payout_complete_dtm": t0 + pd.Timedelta(hours=2),
                "wager": 1.0,
                "is_back_bet": 1,
                "payout_odds": 1.0,
                "prediction_visible_ts_cf": pit,
                "__etl_insert_Dtm_synthetic": pit,
            },
        ]
    )
    src = tmp_path / "cleaned_mini.parquet"
    cmap = tmp_path / "canonical_player_mapping.parquet"
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame([{"player_id": 1, "canonical_id": "c_unit"}])),
        cmap,
    )
    out = tmp_path / "trial_bet_behavior_1h.parquet"
    pq.write_table(pa.Table.from_pandas(df), src)

    materialize_trial_bet_behavior_1h(
        cleaned_bet_parquet=src,
        out_parquet=out,
        canonical_mapping_parquet=cmap,
    )

    got = read_cleaned_bet_dataset(out).sort_values("bet_id", kind="mergesort")
    assert len(got) == 4

    r1 = got[got["bet_id"] == 1.0].iloc[0]
    assert int(r1["bet__bets_cnt__w1h"]) == 0
    assert float(r1["bet__wager_sum__w1h"]) == 0.0

    r2 = got[got["bet_id"] == 2.0].iloc[0]
    assert int(r2["bet__bets_cnt__w1h"]) == 1
    assert float(r2["bet__wager_sum__w1h"]) == 10.0
    assert abs(float(r2["bet__back_bet_ratio__w1h"]) - 1.0) < 1e-9
    assert abs(float(r2["bet__payout_odds_avg__w1h"]) - 2.0) < 1e-9

    r3 = got[got["bet_id"] == 3.0].iloc[0]
    assert int(r3["bet__bets_cnt__w1h"]) == 2
    assert float(r3["bet__wager_sum__w1h"]) == 30.0
    assert abs(float(r3["bet__back_bet_ratio__w1h"]) - 0.5) < 1e-9
    assert abs(float(r3["bet__payout_odds_avg__w1h"]) - 2.5) < 1e-9

    r4 = got[got["bet_id"] == 4.0].iloc[0]
    assert int(r4["bet__bets_cnt__w1h"]) == 0


def test_trial_bet_behavior_1h_cross_player_same_canonical(tmp_path: Path) -> None:
    """1h window counts bets from another player_id under the same canonical_id."""
    from trainer_hightier.utils.trial_bet_behavior_1h import materialize_trial_bet_behavior_1h

    t0 = pd.Timestamp("2024-01-01 12:00:00", tz="UTC")
    pit = t0 + pd.Timedelta(hours=1)
    df = pd.DataFrame(
        [
            {
                "bet_id": 1.0,
                "player_id": 1,
                "payout_complete_dtm": t0,
                "wager": 10.0,
                "is_back_bet": 0,
                "payout_odds": 2.0,
                "prediction_visible_ts_cf": pit,
                "__etl_insert_Dtm_synthetic": pit,
            },
            {
                "bet_id": 2.0,
                "player_id": 2,
                "payout_complete_dtm": t0 + pd.Timedelta(minutes=20),
                "wager": 5.0,
                "is_back_bet": 1,
                "payout_odds": 3.0,
                "prediction_visible_ts_cf": pit,
                "__etl_insert_Dtm_synthetic": pit,
            },
        ]
    )
    cmap = tmp_path / "canonical_player_mapping.parquet"
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    {"player_id": 1, "canonical_id": "same_patron"},
                    {"player_id": 2, "canonical_id": "same_patron"},
                ]
            )
        ),
        cmap,
    )
    src = tmp_path / "cleaned_mini.parquet"
    out = tmp_path / "trial_cross.parquet"
    pq.write_table(pa.Table.from_pandas(df), src)

    materialize_trial_bet_behavior_1h(
        cleaned_bet_parquet=src,
        out_parquet=out,
        canonical_mapping_parquet=cmap,
    )

    got = read_cleaned_bet_dataset(out).sort_values("bet_id", kind="mergesort")
    r2 = got[got["bet_id"] == 2.0].iloc[0]
    assert int(r2["bet__bets_cnt__w1h"]) == 1
    assert float(r2["bet__wager_sum__w1h"]) == 10.0


def test_metamorphic_overlap_invariant_full_vs_player_subset(registry_path: Path, tmp_path: Path) -> None:
    """All-players preprocess matches per-player preprocess on overlapping ``bet_id`` rows."""
    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    df = pd.DataFrame(
        [
            _bet_row(
                bet_id=1,
                player_id=100,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_pay,
            ),
            _bet_row(
                bet_id=2,
                player_id=200,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_pay,
            ),
        ]
    )
    raw_full = tmp_path / "gmwds_t_bet_full.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw_full)
    raw_sub = tmp_path / "gmwds_t_bet_sub.parquet"
    pq.write_table(pa.Table.from_pandas(df[df["player_id"] == 100].copy()), raw_sub)

    out_full = tmp_path / "clean_full_ds"
    out_sub = tmp_path / "clean_sub_ds"
    cfg = BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,preprocess_registry_yaml=registry_path)
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(raw_full, out_full, cfg=cfg)
    _, _bet_b2 = _hpre.preprocess_bets_from_parquet_streaming(raw_sub, out_sub, cfg=cfg)

    full = read_cleaned_bet_dataset(out_full).sort_values("bet_id", kind="mergesort")
    sub = read_cleaned_bet_dataset(out_sub).sort_values("bet_id", kind="mergesort")
    assert len(sub) == 1
    full_overlap = full[full["bet_id"].isin(sub["bet_id"])].reset_index(drop=True)
    pd.testing.assert_frame_equal(full_overlap, sub.reset_index(drop=True))


def test_metamorphic_threshold_expand_stable_bids_subset(registry_path: Path, tmp_path: Path) -> None:
    """Wider ADT projection keeps prior segment rows identical (semi-join on larger allowlist)."""
    import trainer_hightier.utils.bet_l0_preprocess as bl0

    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    df = pd.DataFrame(
        [
            _bet_row(
                bet_id=1,
                player_id=100,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_pay,
            ),
            _bet_row(
                bet_id=2,
                player_id=200,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_pay,
            ),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    base = tmp_path / "clean_base_ds"
    cfg = BetPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,preprocess_registry_yaml=registry_path)
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(raw, base, cfg=cfg)

    allowed_old = tmp_path / "allow_old.parquet"
    allowed_new = tmp_path / "allow_new.parquet"
    pd.DataFrame({"player_id": [100]}).to_parquet(allowed_old, index=False)
    pd.DataFrame({"player_id": [100, 200]}).to_parquet(allowed_new, index=False)

    out_old = tmp_path / "seg_old_ds"
    out_new = tmp_path / "seg_new_ds"
    bl0.segment_cleaned_bet_from_base_parquet(base, allowed_old, out_old)
    bl0.segment_cleaned_bet_from_base_parquet(base, allowed_new, out_new)

    old = read_cleaned_bet_dataset(out_old).sort_values("bet_id", kind="mergesort").reset_index(drop=True)
    new_full = read_cleaned_bet_dataset(out_new).sort_values("bet_id", kind="mergesort").reset_index(drop=True)
    new_stable = (
        new_full[new_full["bet_id"].isin(old["bet_id"])]
        .sort_values("bet_id", kind="mergesort")
        .reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(old, new_stable)
    assert len(new_full) == 2


def test_bet_base_clean_cache_hit_legacy_cleaned_session_dependency_ignored(
    registry_path: Path, tmp_path: Path
) -> None:
    import trainer_hightier.utils.bet_l0_preprocess as bl0

    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    df = pd.DataFrame(
        [_bet_row(payout_complete_dtm=t_pay, gaming_day=t_pay.date(), __etl_insert_Dtm=t_pay)]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    base = tmp_path / "base.parquet"
    df.iloc[0:0].to_parquet(base, index=False)

    rec_cur = bl0.build_bet_base_clean_cache_record(
        [raw],
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=16,
    )
    legacy = dict(rec_cur)
    legacy["manifest_version"] = 8
    legacy["cleaned_session_dependency"] = {
        "path": str(tmp_path / "ghost_session.parquet"),
        "mtime_ns": 1,
        "size_bytes": 2,
        "num_rows": 3,
    }
    mp = bl0.bet_base_clean_cache_manifest_path(base)
    mp.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")

    ghost = tmp_path / "no_such_cleaned_session.parquet"
    assert not ghost.is_file()
    assert bl0.bet_base_clean_cache_is_hit(
        [raw],
        base,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=8,
        cleaned_session_parquet=ghost,
    )


def test_bet_base_clean_cache_hit_with_stored_higher_dedup_buckets(
    registry_path: Path, tmp_path: Path
) -> None:
    """Lower nominal ``dedup_hash_buckets`` still hits when manifest stored OOM-escalated count."""

    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    df = pd.DataFrame(
        [_bet_row(payout_complete_dtm=t_pay, gaming_day=t_pay.date(), __etl_insert_Dtm=t_pay)]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    base = tmp_path / "base.parquet"
    df.iloc[0:0].to_parquet(base, index=False)

    rec = _hpre.build_bet_base_clean_cache_record(
        [raw],
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=16,
    )
    mp = _hpre.bet_base_clean_cache_manifest_path(base)
    mp.write_text(json.dumps(rec, sort_keys=True), encoding="utf-8")

    assert _hpre.bet_base_clean_cache_is_hit(
        [raw],
        base,
        preprocess_registry_yaml=registry_path,
        dedup_hash_buckets=8,
    )


def test_materialize_walkaway_labels_matches_trainer_labels(tmp_path: Path) -> None:
    """Join cleaned bet + mapping, then parity with local :func:`~trainer_hightier.walkaway_compute_labels.compute_labels`."""

    from trainer_hightier.walkaway_compute_labels import compute_labels

    from trainer_hightier.utils.walkaway_labels import materialize_walkaway_labels_from_cleaned_bet

    t0 = pd.Timestamp("2024-06-01 12:00:00")
    horizon = t0 + pd.Timedelta(hours=3)
    df_b = pd.DataFrame(
        [
            {"bet_id": 1.0, "player_id": 100, "payout_complete_dtm": t0},
            {"bet_id": 2.0, "player_id": 100, "payout_complete_dtm": t0 + pd.Timedelta(minutes=35)},
        ]
    )
    df_m = pd.DataFrame([{"player_id": 100, "canonical_id": "c1"}])
    b_path = tmp_path / "bet_clean.parquet"
    m_path = tmp_path / "canonical_map.parquet"
    out_path = tmp_path / "walkaway_labels.parquet"
    pq.write_table(pa.Table.from_pandas(df_b), b_path)
    pq.write_table(pa.Table.from_pandas(df_m), m_path)

    materialize_walkaway_labels_from_cleaned_bet(
        cleaned_bet_parquet=b_path,
        canonical_mapping_parquet=m_path,
        out_parquet=out_path,
        window_end=horizon,
        extended_end=horizon,
    )
    got = pd.read_parquet(out_path).sort_values("bet_id", kind="mergesort")

    direct = compute_labels(
        pd.DataFrame(
            {
                "canonical_id": ["c1", "c1"],
                "bet_id": [1.0, 2.0],
                "payout_complete_dtm": [t0, t0 + pd.Timedelta(minutes=35)],
            }
        ),
        window_end=horizon,
        extended_end=horizon,
    ).sort_values("bet_id", kind="mergesort")

    pd.testing.assert_series_equal(got["label"].reset_index(drop=True), direct["label"].reset_index(drop=True))
    pd.testing.assert_series_equal(got["censored"].reset_index(drop=True), direct["censored"].reset_index(drop=True))


def test_preprocess_bet_end_to_end_row_count_matches_post_dq_dedup(
    registry_path: Path, tmp_path: Path
) -> None:
    """TA-WP-2.9: cleaned output row count equals expected unique rows after DQ + dedup."""
    t_pay = pd.Timestamp("2025-05-27 18:00:00")
    t_old = pd.Timestamp("2025-05-27 18:01:00")
    t_new = pd.Timestamp("2025-05-27 18:02:00")
    df = pd.DataFrame(
        [
            _bet_row(bet_id=1, payout_complete_dtm=t_pay, __etl_insert_Dtm=t_old),
            _bet_row(bet_id=1, payout_complete_dtm=t_pay, __etl_insert_Dtm=t_new),
            _bet_row(bet_id=2, payout_complete_dtm=t_pay, __etl_insert_Dtm=t_new),
            _bet_row(bet_id=3, payout_complete_dtm=t_pay, __etl_insert_Dtm=t_new),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned_ds"
    cfg = BetPreprocessConfig(
        data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
        preprocess_registry_yaml=registry_path,
    )
    _, _bet_b = _hpre.preprocess_bets_from_parquet_streaming(raw, out, cfg=cfg)
    expected_rows = 3
    assert _partitioned_parquet_footer_row_count(out) == expected_rows
    got = read_cleaned_bet_dataset(out)
    assert len(got) == expected_rows
    assert set(got["bet_id"].astype(float).tolist()) == {1.0, 2.0, 3.0}


def test_bet_clean_cache_miss_when_base_cleaned_row_count_poisoned(
    registry_path: Path, tmp_path: Path
) -> None:
    """TA-WP-2.10: segment cache must miss when base-cleaned artifact no longer matches manifest."""
    import trainer_hightier.utils.bet_l0_preprocess as bl0

    t_pay = pd.Timestamp("2025-05-27 09:00:00")
    df = pd.DataFrame(
        [
            _bet_row(
                bet_id=1,
                player_id=100,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_pay,
            ),
            _bet_row(
                bet_id=2,
                player_id=200,
                payout_complete_dtm=t_pay,
                gaming_day=t_pay.date(),
                __etl_insert_Dtm=t_pay,
            ),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    base = tmp_path / "clean_base_ds"
    cfg = BetPreprocessConfig(
        data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
        preprocess_registry_yaml=registry_path,
    )
    _hpre.preprocess_bets_from_parquet_streaming(raw, base, cfg=cfg)

    allowed = tmp_path / "allow.parquet"
    pd.DataFrame({"player_id": [100, 200]}).to_parquet(allowed, index=False)
    seg = tmp_path / "clean_seg_ds"
    bl0.segment_cleaned_bet_from_base_parquet(base, allowed, seg)

    bl0.write_bet_clean_cache_manifest(
        raw,
        seg,
        preprocess_registry_yaml=registry_path,
        adt_filter_quantile=0.5,
        adt_allowed_players_parquet=allowed,
        bet_base_cleaned_parquet=base,
    )
    assert bl0.bet_clean_cache_is_hit(
        raw,
        seg,
        preprocess_registry_yaml=registry_path,
        adt_filter_quantile=0.5,
        adt_allowed_players_parquet=allowed,
        bet_base_cleaned_parquet=base,
    )

    base_shards = sorted(base.rglob("*.parquet"))
    assert base_shards
    base_shards[0].unlink()
    assert not bl0.bet_clean_cache_is_hit(
        raw,
        seg,
        preprocess_registry_yaml=registry_path,
        adt_filter_quantile=0.5,
        adt_allowed_players_parquet=allowed,
        bet_base_cleaned_parquet=base,
    )


def test_consolidate_staged_bucket_partition_dirs_preserves_multi_shard_rows(tmp_path: Path) -> None:
    """Regression: multiple staged shards per bucket/day must not overwrite each other."""
    staged_root = tmp_path / "staged"
    final_root = tmp_path / "final"
    leaf = staged_root / "b0000" / "gaming_month=202606" / "gaming_day_key=2026-06-03"
    leaf.mkdir(parents=True)
    shard_a = leaf / "data_0.parquet"
    shard_b = leaf / "data_1.parquet"
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame({"bet_id": [1, 2], "value": ["a", "b"]})),
        shard_a,
    )
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame({"bet_id": [3, 4, 5], "value": ["c", "d", "e"]})),
        shard_b,
    )
    staged_rows = _partitioned_parquet_footer_row_count(staged_root)
    assert staged_rows == 5

    _consolidate_staged_bucket_partition_dirs(
        staged_root=staged_root,
        final_dataset_root=final_root,
        n_buckets=1,
    )

    final_shards = sorted(final_root.rglob("*.parquet"))
    assert len(final_shards) == 2
    assert {p.name for p in final_shards} == {
        "bucket_0000_part_0000.parquet",
        "bucket_0000_part_0001.parquet",
    }
    assert _partitioned_parquet_footer_row_count(final_root) == staged_rows
