"""Tests for bounded hot-pool short-term training materialization."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.feature_experiment.materialize_fe_derived import (
    materialize_fe_derived_short_term_parquet,
)
from trainer_hightier.serving.offline_serving_backtest import resolve_hot_pool_player_ids
from trainer_hightier.utils.trial_bet_behavior_1h import materialize_trial_bet_behavior_1h


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
            "gaming_day": day,
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
            "gaming_day": day,
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
            "gaming_day": day,
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
            "gaming_day": day,
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
            "gaming_day": day,
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
            "gaming_day": day,
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
