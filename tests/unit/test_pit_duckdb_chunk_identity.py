"""Unit tests for chunk-scoped DuckDB PIT identity join."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from trainer.training.trainer import attach_pit_identity_chunk_duckdb


def _write_session_parquet(path: Path) -> None:
    df = pd.DataFrame(
        {
            "session_id": [1, 1, 2, 3],
            "player_id": [10, 10, 10, 20],
            "casino_player_id": ["cp_old", "cp_new", "cp_new", "cp20"],
            "lud_dtm": pd.to_datetime(
                ["2026-01-01 09:00:00", "2026-01-01 10:00:00", "2026-01-01 11:00:00", "2026-01-01 09:10:00"]
            ),
            "__etl_insert_Dtm": pd.to_datetime(
                ["2026-01-01 09:00:01", "2026-01-01 10:00:01", "2026-01-01 11:00:01", "2026-01-01 09:10:01"]
            ),
            "session_end_dtm": pd.to_datetime(
                ["2026-01-01 09:30:00", "2026-01-01 10:30:00", "2026-01-01 11:30:00", "2026-01-01 09:40:00"]
            ),
            "is_manual": [0, 0, 0, 0],
            "is_deleted": [0, 0, 0, 0],
            "is_canceled": [0, 0, 0, 0],
            "turnover": [100.0, 100.0, 100.0, 100.0],
            "num_games_with_wager": [1, 1, 1, 1],
        }
    )
    df.to_parquet(path, index=False)


def test_attach_pit_identity_chunk_duckdb_maps_latest_link_asof(tmp_path: Path) -> None:
    sess_path = tmp_path / "t_session.parquet"
    _write_session_parquet(sess_path)
    bets = pd.DataFrame(
        {
            "bet_id": [1001, 1002, 1003],
            "player_id": [10, 10, 20],
            "payout_complete_dtm": pd.to_datetime(
                ["2026-01-01 10:20:00", "2026-01-01 11:50:00", "2026-01-01 10:00:00"]
            ),
        }
    )
    out = attach_pit_identity_chunk_duckdb(
        bets_df=bets,
        session_parquet_path=sess_path,
        observation_end=datetime(2026, 1, 1, 12, 0, 0),
    )
    # session_id=1 keeps latest dedup row (usable 10:37), so 10:20 is still unrated.
    assert out.loc[0, "canonical_id"] == "10"
    assert bool(out.loc[0, "_pit_rated"]) is False
    # 11:50 sees link from session_id=2 (usable 11:37) -> cp_new
    assert out.loc[1, "canonical_id"] == "cp_new"
    assert out.loc[2, "canonical_id"] == "cp20"
    assert out["_pit_rated"].tolist() == [False, True, True]


def test_attach_pit_identity_chunk_duckdb_unrated_before_first_link(tmp_path: Path) -> None:
    sess_path = tmp_path / "t_session.parquet"
    _write_session_parquet(sess_path)
    bets = pd.DataFrame(
        {
            "bet_id": [2001],
            "player_id": [10],
            "payout_complete_dtm": pd.to_datetime(["2026-01-01 08:00:00"]),
        }
    )
    out = attach_pit_identity_chunk_duckdb(
        bets_df=bets,
        session_parquet_path=sess_path,
        observation_end=datetime(2026, 1, 1, 12, 0, 0),
    )
    assert out.loc[0, "canonical_id"] == "10"
    assert bool(out.loc[0, "_pit_rated"]) is False
