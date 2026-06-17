"""Tests for bounded hot-pool short-term training materialization."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.feature_experiment.materialize_fe_derived import (
    materialize_fe_derived_short_term_parquet,
)
from trainer_hightier.serving.offline_serving_backtest import resolve_hot_pool_player_ids
from trainer_hightier.utils.cleaned_bet_pool_read import (
    cleaned_bet_pool_game_id_sql,
    cleaned_bet_pool_read_parquet_sql,
    cleaned_bet_pool_source_has_column,
    gaming_months_for_bounded_pool,
    open_month_hot_pool_session,
    pool_source_select_sql,
)
from trainer_hightier.utils.trial_bet_behavior_1h import materialize_trial_bet_behavior_1h


def test_gaming_months_for_bounded_pool_payout_month_prunes_neighbors() -> None:
    """Month-sharded materialize should only need prior + target partition months."""
    got = gaming_months_for_bounded_pool(
        pool_start=pd.Timestamp("2025-01-15 12:00:00", tz="UTC").to_pydatetime(),
        pool_end=pd.Timestamp("2025-01-31 23:00:00", tz="UTC").to_pydatetime(),
        payout_yyyymm="202501",
    )
    assert got == ("202412", "202501")


def test_cleaned_bet_pool_read_parquet_sql_scopes_partitions(tmp_path: Path) -> None:
    """Pool reads should target ``gaming_month`` dirs, not the full cleaned hive."""
    root = tmp_path / "cleaned"
    for ym in ("202412", "202501", "202502"):
        (root / f"gaming_month={ym}" / "gaming_day_key=2025-01-01").mkdir(parents=True)
        pq.write_table(
            pa.table({"bet_id": pa.array([1.0], type=pa.float64())}),
            root / f"gaming_month={ym}" / "gaming_day_key=2025-01-01" / "part.parquet",
        )
    sql = cleaned_bet_pool_read_parquet_sql(
        root,
        pool_start=pd.Timestamp("2025-01-15", tz="UTC").to_pydatetime(),
        pool_end=pd.Timestamp("2025-01-31", tz="UTC").to_pydatetime(),
        payout_yyyymm="202501",
    )
    assert "gaming_month=202412" in sql
    assert "gaming_month=202501" in sql
    assert "gaming_month=202502" not in sql
    assert "hive_partitioning=false" in sql


def _legacy_pool_bet_row(*, bet_id: float, payout: pd.Timestamp, game_id: float | None = None) -> dict:
    row = {
        "bet_id": bet_id,
        "player_id": 10,
        "session_id": 1,
        "table_id": 1,
        "gaming_day_event": pd.Timestamp("2025-01-01"),
        "payout_complete_dtm": payout,
        "wager": 10.0,
        "is_back_bet": 0,
        "payout_odds": 2.0,
        "casino_win": 0.0,
        "theo_win": 1.0,
        "base_ha": 0.01,
        "bet_type": "PLAYER",
        "type_of_bet": "MAIN",
    }
    if game_id is not None:
        row["game_id"] = game_id
    return row


def test_pool_source_select_sql_null_game_id_when_column_missing(tmp_path: Path) -> None:
    """Legacy cleaned-bet fixtures without game_id must still open month hot pools."""
    parquet = tmp_path / "legacy_bets.parquet"
    payout = pd.Timestamp("2025-01-15 12:00:00", tz="UTC")
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame([_legacy_pool_bet_row(bet_id=1.0, payout=payout)])),
        parquet,
    )
    bet_from = f"read_parquet('{parquet.as_posix()}')"
    conn = duckdb.connect(database=":memory:")
    try:
        assert cleaned_bet_pool_source_has_column(conn, bet_from, "game_id") is False
        assert "CAST(NULL AS DOUBLE) AS game_id" in cleaned_bet_pool_game_id_sql(conn, bet_from)
        pool_sql = f"{pool_source_select_sql(conn, bet_from)} FROM {bet_from} AS b"
        got = conn.execute(pool_sql).fetchdf()
        assert "game_id" in got.columns
        assert got["game_id"].isna().all()
    finally:
        conn.close()


def test_pool_source_select_sql_preserves_game_id_when_present(tmp_path: Path) -> None:
    """When game_id exists in the source parquet, pool projection must pass it through."""
    parquet = tmp_path / "modern_bets.parquet"
    payout = pd.Timestamp("2025-01-15 12:00:00", tz="UTC")
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame([_legacy_pool_bet_row(bet_id=1.0, payout=payout, game_id=42.0)]),
        ),
        parquet,
    )
    bet_from = f"read_parquet('{parquet.as_posix()}')"
    conn = duckdb.connect(database=":memory:")
    try:
        assert cleaned_bet_pool_source_has_column(conn, bet_from, "game_id") is True
        assert cleaned_bet_pool_game_id_sql(conn, bet_from) == "b.game_id"
        pool_sql = f"{pool_source_select_sql(conn, bet_from)} FROM {bet_from} AS b"
        got = conn.execute(pool_sql).fetchdf()
        assert float(got["game_id"].iloc[0]) == 42.0
    finally:
        conn.close()


def test_open_month_hot_pool_session_loads_once(tmp_path: Path) -> None:
    """Month pool session should load partition-pruned rows into a reusable DuckDB table."""
    root = tmp_path / "cleaned"
    for ym in ("202412", "202501"):
        part_dir = root / f"gaming_month={ym}" / "gaming_day_key=2025-01-01"
        part_dir.mkdir(parents=True)
        pq.write_table(
            pa.Table.from_pandas(
                pd.DataFrame(
                    [
                        {
                            "bet_id": 1.0 if ym == "202412" else 2.0,
                            "player_id": 10,
                            "session_id": 1,
                            "table_id": 1,
                            "gaming_day_event": pd.Timestamp("2025-01-01"),
                            "payout_complete_dtm": pd.Timestamp(
                                "2024-12-15 12:00:00" if ym == "202412" else "2025-01-15 12:00:00",
                                tz="UTC",
                            ),
                            "wager": 10.0,
                            "is_back_bet": 0,
                            "payout_odds": 2.0,
                            "casino_win": 0.0,
                            "theo_win": 1.0,
                            "base_ha": 0.01,
                            "bet_type": "PLAYER",
                            "type_of_bet": "MAIN",
                        },
                    ],
                ),
            ),
            part_dir / "part.parquet",
        )
    session = open_month_hot_pool_session(
        root,
        payout_yyyymm="202501",
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    try:
        assert session.row_count == 2
        got = session.conn.execute(
            f"SELECT bet_id FROM {session.table_name} ORDER BY bet_id",
        ).fetchall()
        assert [row[0] for row in got] == [1.0, 2.0]
    finally:
        session.close()


def test_open_month_hot_pool_session_tolerates_missing_game_id(tmp_path: Path) -> None:
    """Month pool materialization must not fail when legacy parquet omits game_id."""
    root = tmp_path / "cleaned"
    part_dir = root / "gaming_month=202501" / "gaming_day_key=2025-01-01"
    part_dir.mkdir(parents=True)
    payout = pd.Timestamp("2025-01-15 12:00:00", tz="UTC")
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame([_legacy_pool_bet_row(bet_id=1.0, payout=payout)])),
        part_dir / "part.parquet",
    )
    session = open_month_hot_pool_session(
        root,
        payout_yyyymm="202501",
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    try:
        assert session.row_count == 1
        cols = {
            str(row[0])
            for row in session.conn.execute(f"DESCRIBE {session.table_name}").fetchall()
        }
        assert "game_id" in cols
        game_id = session.conn.execute(
            f"SELECT game_id FROM {session.table_name}",
        ).fetchone()[0]
        assert game_id is None
    finally:
        session.close()


def test_build_pool_uses_month_hot_pool_without_parquet_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When month pool is attached, per-batch pool queries must not rescan parquet."""
    from trainer_hightier.config import default_hightier_serving_config
    from trainer_hightier.serving.offline_serving_backtest import build_pool_from_cleaned_parquet
    import trainer_hightier.utils.cleaned_bet_pool_read as pool_read

    root = tmp_path / "cleaned"
    part_dir = root / "gaming_month=202501" / "gaming_day_key=2025-01-01"
    part_dir.mkdir(parents=True)
    t0 = pd.Timestamp("2025-01-15 12:00:00", tz="UTC")
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    {
                        "bet_id": 1.0,
                        "player_id": 10,
                        "session_id": 1,
                        "table_id": 1,
                        "gaming_day_event": pd.Timestamp("2025-01-15"),
                        "payout_complete_dtm": t0,
                        "wager": 10.0,
                        "is_back_bet": 0,
                        "payout_odds": 2.0,
                        "casino_win": 0.0,
                        "theo_win": 1.0,
                        "base_ha": 0.01,
                        "bet_type": "PLAYER",
                        "type_of_bet": "MAIN",
                    },
                ],
            ),
        ),
        part_dir / "part.parquet",
    )
    cmap = tmp_path / "map.parquet"
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame([{"player_id": 10, "canonical_id": "c1"}])),
        cmap,
    )
    session = open_month_hot_pool_session(
        root,
        payout_yyyymm="202501",
        duckdb_runtime=DuckDbRuntimeConfig(),
    )

    def _forbidden_parquet_sql(*args: object, **kwargs: object) -> str:
        raise AssertionError("cleaned_bet_pool_read_parquet_sql must not run with month pool attached")

    monkeypatch.setattr(pool_read, "cleaned_bet_pool_read_parquet_sql", _forbidden_parquet_sql)
    bets = pd.DataFrame(
        [
            {
                "bet_id": 1.0,
                "player_id": 10,
                "payout_complete_dtm": t0,
                "gaming_day_event": pd.Timestamp("2025-01-15"),
            },
        ],
    )
    try:
        pool = build_pool_from_cleaned_parquet(
            bets,
            cleaned_root=root,
            cfg=default_hightier_serving_config(),
            mapping_parquet=cmap,
            payout_yyyymm="202501",
            month_pool_conn=session.conn,
            month_pool_table=session.table_name,
        )
        assert len(pool) == 1
    finally:
        session.close()


def test_resolve_hot_pool_player_ids_no_alias_expansion(tmp_path: Path) -> None:
    """Training path must not fan out to sibling player_ids on the same canonical."""

    cmap = tmp_path / "map.parquet"
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    {"player_id": 1, "canonical_id": "c1"},
                    {"player_id": 2, "canonical_id": "c1"},
                ],
            ),
        ),
        cmap,
    )
    bets = pd.DataFrame({"player_id": [1]})
    expanded = resolve_hot_pool_player_ids(bets, cmap, expand_canonical_aliases=True)
    narrow = resolve_hot_pool_player_ids(bets, cmap, expand_canonical_aliases=False)
    assert expanded == [1, 2]
    assert narrow == [1]


def test_bounded_short_term_differs_from_full_history_trial(tmp_path: Path) -> None:
    """Bounded pool can under-count prior-hour bets vs full-history trial materialization."""

    t0 = pd.Timestamp("2024-06-01 06:30:00", tz="UTC")
    t1 = t0 + pd.Timedelta(minutes=30)
    t2 = t0 + pd.Timedelta(minutes=45)
    day = t0.tz_convert("Asia/Hong_Kong").date()
    rows = [
        {
            "bet_id": 1.0,
            "player_id": 10,
            "session_id": 1,
            "table_id": 1,
            "gaming_day_event": day,
            "payout_complete_dtm": t0,
            "wager": 100.0,
            "is_back_bet": 0,
            "payout_odds": 2.0,
            "casino_win": 0.0,
            "theo_win": 1.0,
            "base_ha": 0.01,
            "bet_type": "PLAYER",
            "type_of_bet": "MAIN",
            "prediction_visible_ts_cf": t0,
            "__etl_insert_Dtm_synthetic": t0,
        },
        {
            "bet_id": 2.0,
            "player_id": 10,
            "session_id": 1,
            "table_id": 1,
            "gaming_day_event": day,
            "payout_complete_dtm": t1,
            "wager": 50.0,
            "is_back_bet": 1,
            "payout_odds": 2.0,
            "casino_win": 0.0,
            "theo_win": 0.5,
            "base_ha": 0.01,
            "bet_type": "PLAYER",
            "type_of_bet": "MAIN",
            "prediction_visible_ts_cf": t1,
            "__etl_insert_Dtm_synthetic": t1,
        },
        {
            "bet_id": 3.0,
            "player_id": 10,
            "session_id": 1,
            "table_id": 1,
            "gaming_day_event": day,
            "payout_complete_dtm": t2,
            "wager": 25.0,
            "is_back_bet": 0,
            "payout_odds": 2.0,
            "casino_win": 0.0,
            "theo_win": 0.25,
            "base_ha": 0.01,
            "bet_type": "PLAYER",
            "type_of_bet": "MAIN",
            "prediction_visible_ts_cf": t2,
            "__etl_insert_Dtm_synthetic": t2,
        },
    ]
    cleaned = tmp_path / "cleaned"
    cleaned.mkdir()
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), cleaned / "bets.parquet")
    cmap = tmp_path / "map.parquet"
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame([{"player_id": 10, "canonical_id": "c1"}])),
        cmap,
    )
    train = tmp_path / "train.parquet"
    train_row = {k: rows[2][k] for k in rows[2]}
    pq.write_table(pa.Table.from_pandas(pd.DataFrame([train_row])), train)

    full_trial = tmp_path / "full_trial.parquet"
    materialize_trial_bet_behavior_1h(
        cleaned_bet_parquet=cleaned,
        out_parquet=full_trial,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    full_df = pq.read_table(full_trial).to_pandas()
    full_cnt = int(full_df.loc[full_df["bet_id"] == 3.0, "bet__bets_cnt__w1h"].iloc[0])

    bounded = tmp_path / "bounded.parquet"
    materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=bounded,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        short_term_columns=(),
        trial_columns=("bet__bets_cnt__w1h",),
    )
    bounded_cnt = int(
        pq.read_table(bounded, columns=["bet__bets_cnt__w1h"])
        .column("bet__bets_cnt__w1h")
        .to_pylist()[0],
    )
    assert full_cnt == 2
    assert bounded_cnt == 2


def test_bounded_short_term_undercounts_when_pool_truncated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the hot pool omits prior rows, bounded trial counts fall below full-history."""

    t0 = pd.Timestamp("2024-06-01 06:30:00", tz="UTC")
    t1 = t0 + pd.Timedelta(minutes=30)
    t2 = t0 + pd.Timedelta(minutes=45)
    day = t0.tz_convert("Asia/Hong_Kong").date()
    rows = [
        {
            "bet_id": 1.0,
            "player_id": 10,
            "session_id": 1,
            "table_id": 1,
            "gaming_day_event": day,
            "payout_complete_dtm": t0,
            "wager": 100.0,
            "is_back_bet": 0,
            "payout_odds": 2.0,
            "casino_win": 0.0,
            "theo_win": 1.0,
            "base_ha": 0.01,
            "bet_type": "PLAYER",
            "type_of_bet": "MAIN",
            "prediction_visible_ts_cf": t0,
            "__etl_insert_Dtm_synthetic": t0,
        },
        {
            "bet_id": 2.0,
            "player_id": 10,
            "session_id": 1,
            "table_id": 1,
            "gaming_day_event": day,
            "payout_complete_dtm": t1,
            "wager": 50.0,
            "is_back_bet": 1,
            "payout_odds": 2.0,
            "casino_win": 0.0,
            "theo_win": 0.5,
            "base_ha": 0.01,
            "bet_type": "PLAYER",
            "type_of_bet": "MAIN",
            "prediction_visible_ts_cf": t1,
            "__etl_insert_Dtm_synthetic": t1,
        },
        {
            "bet_id": 3.0,
            "player_id": 10,
            "session_id": 1,
            "table_id": 1,
            "gaming_day_event": day,
            "payout_complete_dtm": t2,
            "wager": 25.0,
            "is_back_bet": 0,
            "payout_odds": 2.0,
            "casino_win": 0.0,
            "theo_win": 0.25,
            "base_ha": 0.01,
            "bet_type": "PLAYER",
            "type_of_bet": "MAIN",
            "prediction_visible_ts_cf": t2,
            "__etl_insert_Dtm_synthetic": t2,
        },
    ]
    cleaned = tmp_path / "cleaned"
    cleaned.mkdir()
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), cleaned / "bets.parquet")
    cmap = tmp_path / "map.parquet"
    pq.write_table(
        pa.Table.from_pandas(pd.DataFrame([{"player_id": 10, "canonical_id": "c1"}])),
        cmap,
    )
    train = tmp_path / "train.parquet"
    train_row = {k: rows[2][k] for k in rows[2]}
    pq.write_table(pa.Table.from_pandas(pd.DataFrame([train_row])), train)

    from trainer_hightier.serving import offline_serving_backtest as ob

    real_build = ob.build_pool_from_cleaned_parquet

    def _truncated_pool(bets: pd.DataFrame, **kwargs: object) -> pd.DataFrame:
        pool = real_build(bets, **kwargs)
        return pool.loc[pool["bet_id"] == 3.0].copy()

    monkeypatch.setattr(ob, "build_pool_from_cleaned_parquet", _truncated_pool)

    bounded = tmp_path / "bounded.parquet"
    materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=bounded,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        short_term_columns=(),
        trial_columns=("bet__bets_cnt__w1h",),
    )
    bounded_cnt = int(
        pq.read_table(bounded, columns=["bet__bets_cnt__w1h"])
        .column("bet__bets_cnt__w1h")
        .to_pylist()[0],
    )
    assert bounded_cnt == 0


def test_build_training_data_fail_closed_without_month_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """walkaway_bet_trial_v1 without decomposed cache must not use full Feast service retrieval."""

    import importlib

    bt3 = importlib.import_module("trainer_hightier.03_build_training_data")

    class _Cfg:
        feast_repo = Path(".")
        cleaned_bet_parquet = Path("x")
        labels_parquet = Path("y")
        output_parquet = Path("z/out.parquet")
        feature_service_name = bt3.DEFAULT_FEATURE_SERVICE
        materialize_derived_features = False
        max_entity_rows = None
        duckdb_runtime = DuckDbRuntimeConfig()
        feast_entity_batch_by_calendar_month = False
        training_set_keep_last_n_versions = 1
        feast_retrieval_cache_enabled = True
        auto_feast_apply = False

    monkeypatch.setattr(bt3, "ensure_feast_registry_ready", lambda *a, **k: None)
    monkeypatch.setattr(bt3, "_validate_prereqs", lambda **k: None)
    monkeypatch.setattr(bt3, "_maybe_materialize_derived", lambda cfg: None)
    with pytest.raises(ValueError, match="month-batch decomposed Feast cache"):
        bt3.build_training_data(_Cfg())
