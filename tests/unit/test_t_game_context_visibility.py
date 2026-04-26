"""Unit tests for t_game context metadata pushdown and visibility gate."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from trainer.features.t_game_context import (
    join_t_game_features_for_bets,
    materialize_resolved_t_games,
)


def _write_t_game_parquet(path: Path) -> None:
    """Write a compact synthetic t_game parquet for visibility and dedupe tests."""
    df = pd.DataFrame(
        {
            "game_id": [100, 100, 101, 102, 201, 202],
            "table_id": [10, 10, 10, 10, 20, 20],
            "payout_complete_dtm": pd.to_datetime(
                [
                    "2026-01-01 09:00:00",
                    "2026-01-01 09:00:00",
                    "2026-01-01 09:05:00",
                    "2026-01-01 09:08:00",
                    "2026-01-01 09:07:00",
                    "2026-01-01 09:09:00",
                ]
            ),
            "__etl_insert_Dtm": pd.to_datetime(
                [
                    "2026-01-01 09:00:02",
                    "2026-01-01 09:00:03",
                    "2026-01-01 09:20:00",
                    "2026-01-01 09:08:05",
                    "2026-01-01 09:07:05",
                    "2026-01-01 09:09:05",
                ]
            ),
            "__ts_ms": [1000, 2000, 3000, 4000, 5000, 6000],
            "outcome": ["PLAYER", "BANKER", "BANKER", "PLAYER", "BANKER", "PLAYER"],
            "game_status": ["RESOLVED", "RESOLVED", "RESOLVED", "RESOLVED", "RESOLVED", "RESOLVED"],
            "num_players": [3, 4, 5, 2, 2, 1],
            "total_turnover": [100.0, 200.0, 300.0, 400.0, 150.0, 250.0],
            "casino_win": [10.0, -5.0, 20.0, -10.0, 5.0, -15.0],
        }
    )
    df.to_parquet(path, index=False)


def test_materialize_resolved_t_games_dedup_prefers_latest_ts_and_etl(tmp_path: Path) -> None:
    """Duplicate game_id should keep the row with latest __ts_ms/__etl_insert_Dtm."""
    p = tmp_path / "gmwds_t_game.parquet"
    _write_t_game_parquet(p)
    out = materialize_resolved_t_games(
        parquet_path=p,
        table_ids=[10],
        t_min=pd.Timestamp("2026-01-01 08:00:00"),
        t_max=pd.Timestamp("2026-01-01 10:00:00"),
    )
    row = out[out["game_id"] == 100].iloc[0]
    assert int(row["__ts_ms"]) == 2000
    assert str(row["outcome"]).upper() == "BANKER"


def test_join_t_game_features_visibility_uses_etl_insert_time(tmp_path: Path) -> None:
    """A game is not visible before __etl_insert_Dtm even if event time is earlier."""
    p = tmp_path / "gmwds_t_game.parquet"
    _write_t_game_parquet(p)
    bets = pd.DataFrame(
        {
            "bet_id": [1, 2],
            "table_id": [10, 10],
            "payout_complete_dtm": pd.to_datetime(
                ["2026-01-01 09:10:00", "2026-01-01 09:21:00"]
            ),
        }
    )
    out = join_t_game_features_for_bets(
        bets_df=bets,
        t_game_parquet=p,
        window_start=pd.Timestamp("2026-01-01 08:00:00"),
        window_end=pd.Timestamp("2026-01-01 10:00:00"),
    )
    # 09:10 cannot see game_id=101 because __etl_insert_Dtm is 09:20.
    assert float(out.loc[0, "current_outcome_streak_len"]) == 2.0
    # 09:21 can see game_id=101 (previous rows: 100,101) and gets streak of 1.
    assert float(out.loc[1, "current_outcome_streak_len"]) == 1.0


def test_join_t_game_features_respects_table_id_partition(tmp_path: Path) -> None:
    """ASOF join should use visibility time within the same table_id only."""
    p = tmp_path / "gmwds_t_game.parquet"
    _write_t_game_parquet(p)
    bets = pd.DataFrame(
        {
            "bet_id": [11, 12],
            "table_id": [10, 20],
            "payout_complete_dtm": pd.to_datetime(
                ["2026-01-01 09:21:00", "2026-01-01 09:10:00"]
            ),
        }
    )
    out = join_t_game_features_for_bets(
        bets_df=bets,
        t_game_parquet=p,
        window_start=pd.Timestamp("2026-01-01 08:00:00"),
        window_end=pd.Timestamp("2026-01-01 10:00:00"),
    )
    assert float(out.loc[0, "table_num_players"]) == 4.0
    assert float(out.loc[1, "table_num_players"]) == 2.0


def test_join_t_game_features_handles_tz_aware_bet_times(tmp_path: Path) -> None:
    """Bet timestamps with timezone should still merge via normalized HK-naive keys."""
    p = tmp_path / "gmwds_t_game.parquet"
    _write_t_game_parquet(p)
    bets = pd.DataFrame(
        {
            "bet_id": [21],
            "table_id": [10],
            "payout_complete_dtm": pd.to_datetime(["2026-01-01 09:21:00"]).tz_localize("Asia/Macau"),
        }
    )
    out = join_t_game_features_for_bets(
        bets_df=bets,
        t_game_parquet=p,
        window_start=pd.Timestamp("2026-01-01 08:00:00"),
        window_end=pd.Timestamp("2026-01-01 10:00:00"),
    )
    assert float(out.loc[0, "table_num_players"]) == 4.0
