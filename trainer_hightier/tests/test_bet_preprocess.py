"""L0 ``t_bet`` → cleaned parquet (DQ, registry synthetic, dedup)."""

from __future__ import annotations

import importlib
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer.training.data_sources import _BET_INGEST_READ_COLS_ORDERED

from trainer_hightier.config import BetPreprocessConfig, DuckDbRuntimeConfig
from trainer_hightier.utils.patron_session_metrics import materialize_adt_allowed_players_parquet

_hpre = importlib.import_module("trainer_hightier.02_preprocess")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_REGISTRY = _REPO_ROOT / "schema" / "preprocess_l0_data_contract_registry.yaml"


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
    if merged.get("gaming_day") is None:
        merged["gaming_day"] = pd.Timestamp(merged["payout_complete_dtm"]).date()
    return {c: merged[c] for c in _BET_INGEST_READ_COLS_ORDERED}


@pytest.fixture
def registry_path() -> Path:
    if not _DEFAULT_REGISTRY.is_file():
        pytest.skip(f"registry missing {_DEFAULT_REGISTRY}")
    return _DEFAULT_REGISTRY


@pytest.fixture
def cap_sec(registry_path: Path) -> int:
    from pipelines.layered_data_assets.core.preprocess_bet_ingestion_fix_registry_v1 import (
        load_preprocess_bet_ingestion_fix_registry,
        resolve_bet_ingest_fix004_cap_binding,
    )

    doc = load_preprocess_bet_ingestion_fix_registry(registry_path.resolve())
    cap, _, _, _ = resolve_bet_ingest_fix004_cap_binding(doc)
    return int(cap)


def test_preprocess_bet_dedup_keeps_latest_synthetic(registry_path: Path, tmp_path) -> None:
    t_pay = pd.Timestamp("2025-05-27 18:00:00")
    t_old = pd.Timestamp("2025-05-27 17:50:00")
    t_new = pd.Timestamp("2025-05-27 17:58:00")
    df = pd.DataFrame(
        [
            _bet_row(bet_id=7, payout_complete_dtm=t_pay, gaming_day=t_pay.date(), __etl_insert_Dtm=t_old),
            _bet_row(bet_id=7, payout_complete_dtm=t_pay, gaming_day=t_pay.date(), __etl_insert_Dtm=t_new),
        ]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned.parquet"
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(preprocess_registry_yaml=registry_path),
    )
    got = pd.read_parquet(out)
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
    out = tmp_path / "cleaned.parquet"
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(preprocess_registry_yaml=registry_path),
    )
    got = pd.read_parquet(out)
    assert len(got) == 1
    expected = pd.Timestamp(t_pay) + pd.Timedelta(seconds=cap_sec)
    actual = pd.to_datetime(got.iloc[0]["__etl_insert_Dtm_synthetic"], utc=False)
    delta_s = abs((actual - expected).total_seconds())
    assert delta_s < 2


def test_preprocess_bet_prediction_visible_ts_cf(registry_path: Path, tmp_path) -> None:
    """``prediction_visible_ts_cf`` matches DuckDB ceil-on-epoch formula in preprocess."""
    from trainer.core._config_serving_runtime import SCORER_POLL_INTERVAL_SECONDS
    from trainer.core._config_training_domain import BET_AVAIL_DELAY_MIN

    t_pay = pd.Timestamp("2025-06-01 12:00:00")
    df = pd.DataFrame(
        [_bet_row(payout_complete_dtm=t_pay, gaming_day=t_pay.date(), __etl_insert_Dtm=t_pay)]
    )
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned.parquet"
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(
            preprocess_registry_yaml=registry_path,
            dedup_hash_buckets=1,
        ),
    )
    got = pd.read_parquet(out)
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
    out = tmp_path / "cleaned.parquet"
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(preprocess_registry_yaml=registry_path),
    )
    got = pd.read_parquet(out)
    assert len(got) == 0


def test_bulk_episode_day_tags(registry_path: Path, tmp_path) -> None:
    """Rows with synthetic observed calendar day 2025-05-27 get ingestion_episode_id from registry."""
    pay = pd.Timestamp("2025-05-27 10:30:00")
    etl = pd.Timestamp("2025-05-27 14:30:00")
    df = pd.DataFrame([_bet_row(payout_complete_dtm=pay, gaming_day=pay.date(), __etl_insert_Dtm=etl)])
    raw = tmp_path / "gmwds_t_bet.parquet"
    pq.write_table(pa.Table.from_pandas(df), raw)
    out = tmp_path / "cleaned.parquet"
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(preprocess_registry_yaml=registry_path),
    )
    got = pd.read_parquet(out)
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
    out1 = tmp_path / "cleaned_b1.parquet"
    out8 = tmp_path / "cleaned_b8.parquet"
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out1,
        cfg=BetPreprocessConfig(
            preprocess_registry_yaml=registry_path,
            dedup_hash_buckets=1,
        ),
    )
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out8,
        cfg=BetPreprocessConfig(
            preprocess_registry_yaml=registry_path,
            dedup_hash_buckets=8,
        ),
    )
    g1 = pd.read_parquet(out1).sort_values(["bet_id"]).reset_index(drop=True)
    g8 = pd.read_parquet(out8).sort_values(["bet_id"]).reset_index(drop=True)
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
    out = tmp_path / "cleaned.parquet"
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(preprocess_registry_yaml=registry_path, dedup_hash_buckets=3),
    )
    got = pd.read_parquet(out)
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
    out = tmp_path / "cleaned.parquet"
    _hpre.preprocess_bets_from_parquet_streaming(
        raw,
        out,
        cfg=BetPreprocessConfig(
            preprocess_registry_yaml=registry_path,
            adt_filter_quantile=0.99,
            patron_profile_csv=profile_csv,
            canonical_mapping_parquet=mapping_pq,
            adt_allowed_players_parquet=allowed_pq,
        ),
    )
    got = pd.read_parquet(out)
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
    out = tmp_path / "trial_bet_behavior_1h.parquet"
    pq.write_table(pa.Table.from_pandas(df), src)

    materialize_trial_bet_behavior_1h(cleaned_bet_parquet=src, out_parquet=out)

    got = pd.read_parquet(out).sort_values("bet_id", kind="mergesort")
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

    out_full = tmp_path / "clean_full.parquet"
    out_sub = tmp_path / "clean_sub.parquet"
    cfg = BetPreprocessConfig(preprocess_registry_yaml=registry_path)
    _hpre.preprocess_bets_from_parquet_streaming(raw_full, out_full, cfg=cfg)
    _hpre.preprocess_bets_from_parquet_streaming(raw_sub, out_sub, cfg=cfg)

    full = pd.read_parquet(out_full).sort_values("bet_id", kind="mergesort")
    sub = pd.read_parquet(out_sub).sort_values("bet_id", kind="mergesort")
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
    base = tmp_path / "clean_base.parquet"
    cfg = BetPreprocessConfig(preprocess_registry_yaml=registry_path)
    _hpre.preprocess_bets_from_parquet_streaming(raw, base, cfg=cfg)

    allowed_old = tmp_path / "allow_old.parquet"
    allowed_new = tmp_path / "allow_new.parquet"
    pd.DataFrame({"player_id": [100]}).to_parquet(allowed_old, index=False)
    pd.DataFrame({"player_id": [100, 200]}).to_parquet(allowed_new, index=False)

    out_old = tmp_path / "seg_old.parquet"
    out_new = tmp_path / "seg_new.parquet"
    bl0.segment_cleaned_bet_from_base_parquet(base, allowed_old, out_old)
    bl0.segment_cleaned_bet_from_base_parquet(base, allowed_new, out_new)

    old = pd.read_parquet(out_old).sort_values("bet_id", kind="mergesort").reset_index(drop=True)
    new_full = pd.read_parquet(out_new).sort_values("bet_id", kind="mergesort").reset_index(drop=True)
    new_stable = (
        new_full[new_full["bet_id"].isin(old["bet_id"])]
        .sort_values("bet_id", kind="mergesort")
        .reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(old, new_stable)
    assert len(new_full) == 2


def test_materialize_walkaway_labels_matches_trainer_labels(tmp_path: Path) -> None:
    """Join cleaned bet + mapping, then parity with ``trainer.labels.compute_labels``."""
    from trainer.labels import compute_labels

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
