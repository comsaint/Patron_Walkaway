"""Unit tests for session PIT materialization."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig, SESSION_PIT_FEATURE_COLUMNS
from trainer_hightier.feature_experiment.materialize_session_pit import (
    compute_session_pit_features_for_bets,
)


def test_session_pit_respects_availability_cutoff(tmp_path) -> None:
    """Session row is dropped when decision time precedes closed session availability."""

    decision = datetime(2024, 6, 1, 13, 0, 0)
    end = datetime(2024, 6, 1, 12, 0, 0)
    late_etl = datetime(2024, 6, 1, 14, 0, 0)
    bets = pd.DataFrame(
        [
            {
                "bet_id": 1,
                "session_id": 10,
                "wager": 1000.0,
                "payout_complete_dtm": decision,
            },
        ],
    )
    sessions = pd.DataFrame(
        [
            {
                "session_id": 10,
                "session_end_dtm": end,
                "__etl_insert_Dtm": late_etl,
                "__etl_insert_Dtm_synthetic": end + timedelta(seconds=636),
                "num_games_with_wager": 5,
                "num_bets": 20,
                "turnover": 50000.0,
                "theo_win": 100.0,
                "is_manual": 0,
                "is_deleted": 0,
                "is_canceled": 0,
            },
        ],
    )
    bet_path = tmp_path / "bets.parquet"
    sess_path = tmp_path / "sessions.parquet"
    pq.write_table(pa.Table.from_pandas(bets), bet_path)
    pq.write_table(pa.Table.from_pandas(sessions), sess_path)
    bets_with_pv = bets.copy()
    bets_with_pv["prediction_visible_ts_cf"] = decision
    bet_read = f"read_parquet('{str(bet_path).replace(chr(92), '/')}')"
    out = compute_session_pit_features_for_bets(
        bets_with_pv,
        cleaned_session_parquet=sess_path,
        cleaned_bet_read=bet_read,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    assert list(out.columns) == ["bet_id", *SESSION_PIT_FEATURE_COLUMNS]
    assert int(out["sess__available_flag"].iloc[0]) == 1

    early = end + timedelta(seconds=30)
    bets_early = bets_with_pv.copy()
    bets_early["prediction_visible_ts_cf"] = early
    out_early = compute_session_pit_features_for_bets(
        bets_early,
        cleaned_session_parquet=sess_path,
        cleaned_bet_read=bet_read,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    assert int(out_early["sess__available_flag"].iloc[0]) == 0
