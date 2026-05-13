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
