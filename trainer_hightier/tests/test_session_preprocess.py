"""Regression tests for session parquet preprocess (row-group read + DQ)."""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_hpre = importlib.import_module("trainer_hightier.02_preprocess")


def test_write_cleaned_session_parquet_default(tmp_path) -> None:
    df = pd.DataFrame({"session_id": [1], "x": [1.0]})
    out = _hpre.write_cleaned_session_parquet(df, output_path=tmp_path / "sub" / "cleaned__gmwds_t_session.parquet")
    assert out.is_file()
    r = pd.read_parquet(out)
    assert len(r) == 1 and int(r["session_id"].iloc[0]) == 1


def test_preprocess_sessions_manual_ghost_dedup(tmp_path_factory) -> None:
    """FND-01 dedup + FND-02 manual exclude + FND-04 ghost drop (trainer parity stub path)."""
    td = tmp_path_factory.mktemp("sess_pq")

    ws = datetime(2024, 1, 15, 0, 0, 0)
    rows = [
        dict(
            session_id=1,
            player_id=100,
            casino_player_id="x",
            lud_dtm=ws,
            session_start_dtm=ws,
            session_end_dtm=ws,
            table_id=None,
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=1,
            turnover=1.0,
            __etl_insert_Dtm=None,
        ),
        dict(
            session_id=1,
            player_id=100,
            casino_player_id="x",
            lud_dtm=datetime(2024, 1, 16),
            session_start_dtm=ws,
            session_end_dtm=ws,
            table_id=None,
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=1,
            turnover=10.0,
            __etl_insert_Dtm=None,
        ),
        dict(
            session_id=2,
            player_id=101,
            casino_player_id="y",
            lud_dtm=ws,
            session_start_dtm=ws,
            session_end_dtm=ws,
            table_id=None,
            is_manual=1,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=1,
            turnover=5.0,
            __etl_insert_Dtm=None,
        ),
        dict(
            session_id=3,
            player_id=102,
            casino_player_id="z",
            lud_dtm=ws,
            session_start_dtm=ws,
            session_end_dtm=ws,
            table_id=None,
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=0,
            turnover=0.0,
            __etl_insert_Dtm=None,
        ),
    ]

    pq_path = td / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), pq_path)

    out = _hpre.preprocess_sessions_from_parquet(pq_path)

    assert len(out) == 2
    sids = sorted(int(pd.to_numeric(x, errors="coerce")) for x in out["session_id"].tolist())
    assert sids == [1, 2]
    r1 = out.loc[out["session_id"].astype(float).eq(1)].iloc[0]
    assert float(r1["turnover"]) == 10.0


def test_etl_insert_synthetic_caps_per_registry(tmp_path_factory) -> None:
    """SESSION-INGEST-FIX-001: LEAST(etl, session_end + 636s)."""
    td = tmp_path_factory.mktemp("sess_cap")
    t0 = datetime(2024, 6, 1, 12, 0, 0)
    etl = t0 + timedelta(days=2)

    rows = [
        dict(
            session_id=1,
            player_id=1,
            casino_player_id="a",
            lud_dtm=t0,
            session_start_dtm=t0,
            session_end_dtm=t0,
            table_id=None,
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=1,
            turnover=1.0,
            __etl_insert_Dtm=etl,
        ),
    ]
    pq_path = td / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), pq_path)

    out = _hpre.preprocess_sessions_from_parquet(pq_path)
    cap = pd.Timestamp(t0) + timedelta(seconds=636)
    got = pd.Timestamp(out["__etl_insert_Dtm_synthetic"].iloc[0])
    assert got == cap


def test_session_end_imputed_from_start_for_synthetic(tmp_path_factory) -> None:
    """Null session_end_dtm filled from start before synthetic."""
    td = tmp_path_factory.mktemp("sess_impute")
    t0 = datetime(2024, 6, 1, 12, 0, 0)
    etl = t0 + timedelta(hours=2)

    rows = [
        dict(
            session_id=1,
            player_id=1,
            casino_player_id="a",
            lud_dtm=t0,
            session_start_dtm=t0,
            session_end_dtm=None,
            table_id=None,
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=1,
            turnover=10.0,
            __etl_insert_Dtm=etl,
        ),
    ]
    pq_path = td / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), pq_path)

    out = _hpre.preprocess_sessions_from_parquet(pq_path)
    assert len(out) == 1
    cap = pd.Timestamp(t0) + timedelta(seconds=636)
    got = pd.Timestamp(out["__etl_insert_Dtm_synthetic"].iloc[0])
    assert got == cap


def test_streaming_preprocess_fnd01_dedup_across_row_groups(tmp_path) -> None:
    """Two row groups each carry session_id=1; FND-01 keeps the row with latest ``lud_dtm``."""
    t_old = datetime(2024, 1, 1, 0, 0, 0)
    t_new = datetime(2024, 3, 1, 0, 0, 0)

    def _row(lud: datetime) -> dict:
        return dict(
            session_id=1,
            player_id=1,
            casino_player_id="a",
            lud_dtm=lud,
            session_start_dtm=t_old,
            session_end_dtm=t_old,
            table_id=None,
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=1,
            turnover=1.0,
            __etl_insert_Dtm=lud,
        )

    df = pd.DataFrame([_row(t_old), _row(t_new)])
    pq_path = tmp_path / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(df), pq_path, row_group_size=1)

    out_path = tmp_path / "cleaned.parquet"
    _hpre.preprocess_sessions_from_parquet_streaming(pq_path, out_path)
    out = pd.read_parquet(out_path)
    assert len(out) == 1
    assert pd.Timestamp(out["lud_dtm"].iloc[0]) == pd.Timestamp(t_new)
