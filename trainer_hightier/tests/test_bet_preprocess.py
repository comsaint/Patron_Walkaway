"""L0 ``t_bet`` → cleaned parquet (DQ, registry synthetic, dedup)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import BetPreprocessConfig

_hpre = importlib.import_module("trainer_hightier.02_preprocess")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_REGISTRY = _REPO_ROOT / "schema" / "preprocess_l0_data_contract_registry.yaml"


def _bet_row(**kwargs):
    defaults = dict(
        bet_id=kwargs.get("bet_id", 1),
        session_id=kwargs.get("session_id", 10),
        player_id=kwargs.get("player_id", 100),
        game_id=kwargs.get("game_id", 1),
        table_id=kwargs.get("table_id", 2),
        payout_complete_dtm=kwargs["payout_complete_dtm"],
        gaming_day=kwargs["gaming_day"],
        __etl_insert_Dtm=kwargs["__etl_insert_Dtm"],
        wager=kwargs.get("wager", 1.0),
        status=kwargs.get("status", 1),
        casino_win=kwargs.get("casino_win", 0.0),
        payout_odds=kwargs.get("payout_odds", 1.0),
        base_ha=kwargs.get("base_ha", 1),
        is_back_bet=kwargs.get("is_back_bet", 0),
        position_idx=kwargs.get("position_idx", 1),
    )
    return defaults


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
    df = pd.DataFrame(
        [_bet_row(payout_complete_dtm=pay, gaming_day=pay.date(), __etl_insert_Dtm=etl)]
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
    assert got.iloc[0]["ingestion_episode_id"] == "BET-BULK-INGEST-2025-05-27"
