"""Unit tests for scorer allowlist chunk merge and SQL safety (no ``t_bet.casino_player_id``)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from trainer_hightier.config import HightierServingConfig


def test_split_allowlist_player_id_chunks_sorted_and_sized() -> None:
    from trainer_hightier.serving.scorer import split_allowlist_player_id_chunks

    assert split_allowlist_player_id_chunks(frozenset(), 500) == []
    ch = split_allowlist_player_id_chunks(frozenset({3, 1, 2}), 2)
    assert ch == [[1, 2], [3]]
    with pytest.raises(ValueError, match="chunk_size"):
        split_allowlist_player_id_chunks(frozenset({1}), 0)


def test_merge_incremental_chunk_frames_dedupes_and_orders() -> None:
    from trainer_hightier.serving.scorer import merge_incremental_chunk_frames

    hk = ZoneInfo("Asia/Hong_Kong")
    t1 = pd.Timestamp("2025-01-01 10:00:00", tz=hk)
    t2 = pd.Timestamp("2025-01-01 10:00:01", tz=hk)
    t3 = pd.Timestamp("2025-01-01 09:59:00", tz=hk)
    a = pd.DataFrame(
        {
            "bet_id": [2, 1],
            "__etl_insert_Dtm": [t2, t1],
            "x": ["b", "a"],
        }
    )
    b = pd.DataFrame(
        {
            "bet_id": [1, 3],
            "__etl_insert_Dtm": [t1, t3],
            "x": ["dup", "c"],
        }
    )
    out = merge_incremental_chunk_frames([a, b], limit_rows=2)
    assert list(out["bet_id"]) == [3, 1]
    assert list(out["x"]) == ["c", "a"]


def test_fetch_bets_incremental_global_query_no_tbets_casino_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    from trainer_hightier.serving import scorer as scorer_mod

    sql_holder: list[str] = []

    class _FC:
        def query_df(self, sql: str, parameters=None):
            sql_holder.append(sql)
            return pd.DataFrame()

    monkeypatch.setattr(scorer_mod, "get_clickhouse_client", lambda: _FC())
    scorer_mod.fetch_bets_incremental(
        None,
        lookback_hours=1.0,
        limit_rows=5,
        allowlist_player_ids=None,
    )
    assert len(sql_holder) == 1
    q = sql_holder[0]
    assert "CAST(NULL AS Nullable(String))" in q
    assert "CAST(wager AS Float64) AS wager" in q
    assert "CAST(casino_win AS Float64) AS casino_win" in q
    assert "trim(casino_player_id)" not in q.replace(" ", "").lower()


def test_fetch_bets_incremental_allowlist_uses_short_in_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    from trainer_hightier.serving import scorer as scorer_mod

    cfg = replace(HightierServingConfig(), hightier_scorer_player_id_chunk_size=2)
    monkeypatch.setattr(scorer_mod, "default_hightier_serving_config", lambda: cfg)

    sqls: list[str] = []

    class _FC:
        def query_df(self, sql: str, parameters=None):
            sqls.append(sql)
            return pd.DataFrame()

    monkeypatch.setattr(scorer_mod, "get_clickhouse_client", lambda: _FC())
    scorer_mod.fetch_bets_incremental(
        None,
        lookback_hours=1.0,
        limit_rows=10,
        allowlist_player_ids=frozenset({10, 20, 30}),
    )
    assert len(sqls) == 2
    for q in sqls:
        assert "player_id IN (" in q
        assert "CAST(wager AS Float64) AS wager" in q
        assert "CAST(casino_win AS Float64) AS casino_win" in q
        assert "trim(casino_player_id)" not in q.replace(" ", "").lower()


def test_fetch_bets_incremental_merge_row_cap_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from trainer_hightier.serving import scorer as scorer_mod

    cfg = replace(
        HightierServingConfig(),
        hightier_scorer_player_id_chunk_size=1,
        hightier_scorer_chunk_merge_row_cap=1,
    )
    monkeypatch.setattr(scorer_mod, "default_hightier_serving_config", lambda: cfg)

    class _FC:
        def query_df(self, sql: str, parameters=None):
            return pd.DataFrame({"bet_id": [1], "__etl_insert_Dtm": [pd.Timestamp.utcnow()]})

    monkeypatch.setattr(scorer_mod, "get_clickhouse_client", lambda: _FC())
    with pytest.raises(RuntimeError, match="chunk merge exceeds"):
        scorer_mod.fetch_bets_incremental(
            None,
            lookback_hours=1.0,
            limit_rows=10,
            allowlist_player_ids=frozenset({1, 2}),
        )


def test_fetch_bet_pool_window_chunks_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from trainer_hightier.serving import scorer as scorer_mod

    cfg = replace(HightierServingConfig(), hightier_scorer_player_id_chunk_size=1)
    monkeypatch.setattr(scorer_mod, "default_hightier_serving_config", lambda: cfg)

    hk = ZoneInfo("Asia/Hong_Kong")
    ws = pd.Timestamp("2025-01-01 08:00:00", tz=hk)
    we = pd.Timestamp("2025-01-01 12:00:00", tz=hk)
    sqls: list[str] = []

    class _FC:
        def __init__(self) -> None:
            self.i = 0

        def query_df(self, sql: str, parameters=None):
            sqls.append(sql)
            self.i += 1
            pid = 100 + self.i
            return pd.DataFrame(
                {
                    "bet_id": [pid],
                    "is_back_bet": [0],
                    "bet_type": ["BANKER"],
                    "type_of_bet": ["MAIN_BET"],
                    "__etl_insert_Dtm": [ws],
                    "payout_complete_dtm": [ws + pd.Timedelta(minutes=self.i)],
                    "gaming_day": [pd.Timestamp("2025-01-01").date()],
                    "session_id": [1],
                    "player_id": [pid],
                    "table_id": [1],
                    "position_idx": [1],
                    "wager": [100.0],
                    "casino_win": [0.0],
                    "payout_odds": [1.0],
                    "status": [1],
                }
            )

    monkeypatch.setattr(scorer_mod, "get_clickhouse_client", lambda: _FC())
    out = scorer_mod.fetch_bet_pool_window(player_ids=[1, 2, 3], window_start=ws, window_end=we)
    assert len(out) == 3
    assert len(sqls) == 3
    for q in sqls:
        assert "CAST(wager AS Float64) AS wager" in q
        assert "CAST(casino_win AS Float64) AS casino_win" in q


def test_append_hightier_prediction_log_writes_rows(tmp_path) -> None:
    import json
    import sqlite3

    from trainer_hightier.serving.prediction_log import append_hightier_prediction_log

    db = tmp_path / "prediction_log.db"
    staged = pd.DataFrame(
        {
            "bet_id": [1, 2],
            "session_id": ["a", "b"],
            "player_id": [10, 20],
            "canonical_id": ["c1", "c2"],
            "casino_player_id": ["x", None],
            "table_id": [1, 2],
        }
    )
    features = pd.DataFrame(
        {
            "wager": [100.0, 200.0],
            "fe__bets_cnt__w15m": [3.0, None],
        }
    )
    prob = np.array([0.9, 0.6], dtype=np.float64)
    append_hightier_prediction_log(
        db,
        scored_at="2025-01-01T00:00:00+08:00",
        model_version="mv1",
        staged=staged,
        prob=prob,
        threshold=0.5,
        features=features,
        feature_columns=("wager", "fe__bets_cnt__w15m"),
    )
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            """
            SELECT bet_id, is_alert, is_rated_obs, margin, threshold,
                   features_json, fe_features_missing
            FROM prediction_log ORDER BY bet_id
            """
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][:2] == ("1", 1)
    assert rows[0][2] == 1
    assert rows[1][:2] == ("2", 0)
    assert rows[1][2] == 0
    assert rows[0][4] == 0.5
    feat0 = json.loads(rows[0][5])
    assert feat0["wager"] == 100.0
    assert feat0["fe__bets_cnt__w15m"] == 3.0
    assert rows[0][6] == 0
    feat1 = json.loads(rows[1][5])
    assert feat1["fe__bets_cnt__w15m"] is None
    assert rows[1][6] == 1


def test_append_hightier_prediction_log_disabled_no_file(tmp_path) -> None:
    from trainer_hightier.serving.prediction_log import append_hightier_prediction_log

    p = tmp_path / "nope.db"
    append_hightier_prediction_log(
        None,
        scored_at="2025-01-01T00:00:00+08:00",
        model_version="mv",
        staged=pd.DataFrame({"bet_id": [1]}),
        prob=np.array([0.5]),
        threshold=0.5,
    )
    assert not p.is_file()


def test_init_prediction_log_db_idempotent(tmp_path) -> None:
    import sqlite3

    from trainer_hightier.serving.prediction_log import init_prediction_log_db

    db = tmp_path / "prediction_log.db"
    assert init_prediction_log_db(db) == db.resolve()
    init_prediction_log_db(db)
    assert db.is_file()
    with sqlite3.connect(db) as conn:
        n = int(conn.execute("SELECT COUNT(*) FROM prediction_log").fetchone()[0])
        cols = [r[1] for r in conn.execute("PRAGMA table_info(prediction_log)").fetchall()]
    assert n == 0
    assert "scored_at" in cols
    assert "features_json" in cols
    assert "fe_features_missing" in cols
    assert init_prediction_log_db(None) is None


def test_attach_canonical_id_fills_casino_player_id(tmp_path) -> None:
    """Rated rows get ``casino_player_id`` from three-column mapping parquet."""
    from trainer_hightier.serving.feature_builder import attach_canonical_id

    map_pq = tmp_path / "map.parquet"
    pd.DataFrame(
        {"player_id": [900], "canonical_id": ["card_new"], "casino_player_id": ["card_new"]}
    ).to_parquet(map_pq, index=False)
    bets = pd.DataFrame({"player_id": pd.Series([900], dtype="Int64"), "bet_id": [1]})
    out = attach_canonical_id(bets, mapping_parquet=map_pq)
    assert str(out["canonical_id"].iloc[0]) == "card_new"
    assert str(out["casino_player_id"].iloc[0]) == "card_new"


def test_attach_canonical_id_legacy_two_column_parquet(tmp_path) -> None:
    """Serving fills ``casino_player_id`` from ``canonical_id`` when parquet lacks the column."""
    from trainer_hightier.serving.feature_builder import attach_canonical_id

    map_pq = tmp_path / "legacy.parquet"
    pd.DataFrame({"player_id": [900], "canonical_id": ["legacy_card"]}).to_parquet(map_pq, index=False)
    bets = pd.DataFrame({"player_id": pd.Series([900], dtype="Int64"), "bet_id": [1]})
    out = attach_canonical_id(bets, mapping_parquet=map_pq)
    assert str(out["casino_player_id"].iloc[0]) == "legacy_card"


def test_attach_canonical_id_drops_placeholder_and_unmapped_null(tmp_path) -> None:
    """Avoid merge suffix clash from ClickHouse NULL ``casino_player_id``; unmapped loyalty stays NA."""
    from trainer_hightier.serving.feature_builder import attach_canonical_id

    map_pq = tmp_path / "map.parquet"
    pd.DataFrame({"player_id": [900], "canonical_id": ["z"], "casino_player_id": ["z"]}).to_parquet(
        map_pq, index=False
    )
    bets = pd.DataFrame(
        {
            "player_id": pd.Series([900, 999], dtype="Int64"),
            "bet_id": [1, 2],
            "casino_player_id": [np.nan, np.nan],
        }
    )
    out = attach_canonical_id(bets, mapping_parquet=map_pq)
    assert str(out.loc[out["player_id"] == 900, "casino_player_id"].iloc[0]) == "z"
    assert pd.isna(out.loc[out["player_id"] == 999, "casino_player_id"].iloc[0])
    assert out.loc[out["player_id"] == 999, "canonical_id"].iloc[0] == "999"


def test_alerts_protocol_preserves_casino_player_id() -> None:
    """``_alerts_to_protocol_records`` exposes ML API ``casino_player_id`` when present in SQLite row."""
    from trainer_hightier.serving.api_server import _alerts_to_protocol_records

    hk = ZoneInfo("Asia/Hong_Kong")
    df = pd.DataFrame(
        {
            "ts_dt": [pd.Timestamp("2025-06-01 12:00:00", tz=hk)],
            "bet_id": ["1"],
            "bet_ts": [None],
            "player_id": [900],
            "casino_player_id": ["CARD99"],
            "table_id": [1],
            "position_idx": [1.0],
            "session_id": [10],
            "visit_avg_bet": [100.0],
            "is_rated_obs": [1],
        }
    )
    rec = _alerts_to_protocol_records(df)
    assert len(rec) == 1
    assert rec[0]["casino_player_id"] == "CARD99"
    assert rec[0]["is_known_player"] == 1
