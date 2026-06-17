"""Regression tests for session parquet preprocess (row-group read + DQ)."""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED, SessionPreprocessConfig

_hpre = importlib.import_module("trainer_hightier.02_preprocess")

_TEST_SESS_CFG = SessionPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED)


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
    gd_ws = ws.date()
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
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=0.0,
            gaming_day=gd_ws,
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
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=0.0,
            gaming_day=date(2024, 1, 16),
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
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=0.0,
            gaming_day=gd_ws,
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
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=0.0,
            gaming_day=gd_ws,
            __etl_insert_Dtm=None,
        ),
    ]

    pq_path = td / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), pq_path)

    out = _hpre.preprocess_sessions_from_parquet(pq_path, cfg=_TEST_SESS_CFG)

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
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=0.0,
            gaming_day=t0.date(),
            __etl_insert_Dtm=etl,
        ),
    ]
    pq_path = td / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), pq_path)

    out = _hpre.preprocess_sessions_from_parquet(pq_path, cfg=_TEST_SESS_CFG)
    cap = pd.Timestamp(t0) + timedelta(seconds=636)
    got = pd.Timestamp(out["__etl_insert_Dtm_synthetic"].iloc[0])
    assert got == cap


def test_session_null_end_excluded(tmp_path_factory) -> None:
    """Rows with null session_end_dtm are excluded (no session_start_dtm fallback)."""
    td = tmp_path_factory.mktemp("sess_null_end")
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
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=0.0,
            __etl_insert_Dtm=etl,
        ),
    ]
    pq_path = td / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), pq_path)

    out = _hpre.preprocess_sessions_from_parquet(pq_path, cfg=_TEST_SESS_CFG)
    assert len(out) == 0


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
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=0.0,
            gaming_day=lud.date(),
            __etl_insert_Dtm=lud,
        )

    df = pd.DataFrame([_row(t_old), _row(t_new)])
    pq_path = tmp_path / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(df), pq_path, row_group_size=1)

    out_path = tmp_path / "cleaned.parquet"
    _, _buck = _hpre.preprocess_sessions_from_parquet_streaming(
        pq_path,
        out_path,
        cfg=_TEST_SESS_CFG,
    )
    assert _buck >= 1
    out = pd.read_parquet(out_path)
    assert len(out) == 1
    assert pd.Timestamp(out["lud_dtm"].iloc[0]) == pd.Timestamp(t_new)


def test_streaming_session_hash_buckets_matches_single_pass(tmp_path) -> None:
    """Hash-bucketed FND-01 dedup must match single-pass (same survivor per session_id)."""
    base = datetime(2024, 1, 1, 0, 0, 0)

    def _row(session_id: int, lud: datetime) -> dict:
        return dict(
            session_id=session_id,
            player_id=session_id,
            casino_player_id=str(session_id),
            lud_dtm=lud,
            session_start_dtm=base,
            session_end_dtm=base,
            table_id=None,
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=1,
            turnover=1.0,
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=0.0,
            gaming_day=lud.date(),
            __etl_insert_Dtm=lud,
        )

    rows: list[dict] = []
    for sid in range(1, 11):
        rows.append(_row(sid, base + timedelta(hours=sid)))
        rows.append(_row(sid, base + timedelta(hours=sid, minutes=30)))
    dup_sid = 5
    pay = base + timedelta(hours=dup_sid, minutes=30)
    rows.append(
        _row(
            dup_sid,
            pay,
        )
        | {"__etl_insert_Dtm": pay + timedelta(hours=3)}
    )

    df = pd.DataFrame(rows)
    pq_path = tmp_path / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(df), pq_path)
    out1 = tmp_path / "cleaned_b1.parquet"
    out8 = tmp_path / "cleaned_b8.parquet"
    _, b1 = _hpre.preprocess_sessions_from_parquet_streaming(
        pq_path,
        out1,
        cfg=SessionPreprocessConfig(
            data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
            dedup_hash_buckets=1,
        ),
    )
    _, b8 = _hpre.preprocess_sessions_from_parquet_streaming(
        pq_path,
        out8,
        cfg=SessionPreprocessConfig(
            data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,
            dedup_hash_buckets=8,
        ),
    )
    assert b1 == 1 and b8 == 8
    g1 = pd.read_parquet(out1).sort_values(["session_id"]).reset_index(drop=True)
    g8 = pd.read_parquet(out8).sort_values(["session_id"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(g1, g8)


def test_session_preprocess_projection_drops_unlisted_wide_columns(tmp_path) -> None:
    """Explicit L0 projection: extra source columns never reach cleaned parquet."""
    t0 = datetime(2024, 7, 1, 15, 0, 0)
    rows = [
        dict(
            session_id=77,
            player_id=2,
            casino_player_id="wide",
            lud_dtm=t0,
            session_start_dtm=t0,
            session_end_dtm=t0,
            is_manual=0,
            is_deleted=0,
            is_canceled=0,
            num_games_with_wager=3,
            turnover=12.0,
            player_win=0.0,
            cash_buyins=0.0,
            num_bets=0,
            theo_win=1.5,
            gaming_day=t0.date(),
            __etl_insert_Dtm=t0,
            junk_metric_only_in_l0=999,
            blob_like_col_should_not_ship="drop_me",
        )
    ]
    pq_path = tmp_path / "gmwds_t_session.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), pq_path, row_group_size=1)

    out_path = tmp_path / "cleaned.parquet"
    _, eff_sess = _hpre.preprocess_sessions_from_parquet_streaming(
        pq_path,
        out_path,
        cfg=_TEST_SESS_CFG,
    )
    assert eff_sess == SessionPreprocessConfig(data_scope=L0_PREPROCESS_DATA_SCOPE_TEST_UNBOUNDED,).dedup_hash_buckets

    cols = pq.read_schema(out_path).names
    assert "junk_metric_only_in_l0" not in cols
    assert "blob_like_col_should_not_ship" not in cols
    assert frozenset(cols).issuperset(frozenset(_hpre.SESSION_PREPROCESS_READ_COLS_ORDERED))
    assert "__etl_insert_Dtm_synthetic" in cols


def test_degraded_runtime_config_after_oom_from_none_threads() -> None:
    """First degradation pins threads when DuckDB defaulted to unlimited."""
    import trainer_hightier.utils.duckdb_runtime as _drunner
    from trainer_hightier.config import DuckDbRuntimeConfig

    nxt = _drunner.degraded_runtime_config_after_oom(DuckDbRuntimeConfig(threads=None))
    assert nxt is not None
    assert nxt.threads == 8


def test_degraded_runtime_config_after_oom_halves_explicit_threads() -> None:
    """OOM ladder halves explicit thread counts down to one."""
    import trainer_hightier.utils.duckdb_runtime as _drunner
    from trainer_hightier.config import DuckDbRuntimeConfig

    c8 = DuckDbRuntimeConfig(threads=8)
    c4 = _drunner.degraded_runtime_config_after_oom(c8)
    assert c4 is not None and c4.threads == 4
    c2 = _drunner.degraded_runtime_config_after_oom(c4)
    assert c2 is not None and c2.threads == 2
    c1 = _drunner.degraded_runtime_config_after_oom(c2)
    assert c1 is not None and c1.threads == 1
    assert _drunner.degraded_runtime_config_after_oom(c1) is None
