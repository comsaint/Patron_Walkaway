"""Patron-level session aggregates (canonical ADT report)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.patron_session_metrics import compile_canonical_patron_session_metrics


def _sess_row(pid: int, sid: int, theo: float, gd: date) -> dict:
    t = pd.Timestamp("2024-06-01 12:00:00")
    return {
        "session_id": sid,
        "player_id": pid,
        "casino_player_id": str(pid),
        "lud_dtm": t,
        "session_start_dtm": t,
        "session_end_dtm": t,
        "gaming_day": gd,
        "theo_win": theo,
        "is_manual": 0,
        "is_deleted": 0,
        "is_canceled": 0,
        "num_games_with_wager": 1,
        "turnover": 1.0,
    }


def test_patron_session_metrics_adt_sorted_desc(tmp_path) -> None:
    """ADT = total_theo_win / distinct gaming_days; sort high ADT first."""
    cleaned = tmp_path / "cleaned.parquet"
    mp = tmp_path / "map.parquet"
    out_pq = tmp_path / "adt.parquet"

    df_s = pd.DataFrame(
        [
            _sess_row(10, 1, 100.0, date(2024, 1, 1)),
            _sess_row(10, 2, 50.0, date(2024, 1, 2)),
            _sess_row(20, 3, 30.0, date(2024, 1, 1)),
        ]
    )
    pq.write_table(pa.Table.from_pandas(df_s), cleaned)

    df_m = pd.DataFrame(
        {
            "player_id": [10, 20],
            "canonical_id": ["HIGH_ADT", "LOW_ADT"],
        }
    )
    pq.write_table(pa.Table.from_pandas(df_m), mp)

    compile_canonical_patron_session_metrics(
        cleaned,
        mp,
        duckdb_runtime=DuckDbRuntimeConfig(),
        output_parquet=out_pq,
        duckdb_join_timeout_s=120.0,
    )

    got = pd.read_parquet(out_pq)
    assert list(got.columns) == ["canonical_id", "total_theo_win", "gaming_days", "adt"]
    assert got.iloc[0]["canonical_id"] == "HIGH_ADT"
    assert float(got.iloc[0]["total_theo_win"]) == 150.0
    assert int(got.iloc[0]["gaming_days"]) == 2
    assert abs(float(got.iloc[0]["adt"]) - 75.0) < 1e-9
    assert got.iloc[1]["canonical_id"] == "LOW_ADT"
    assert float(got.iloc[1]["adt"]) == 30.0


def test_patron_session_metrics_requires_theo_and_gaming_day(tmp_path) -> None:
    cleaned = tmp_path / "cleaned.parquet"
    mp = tmp_path / "map.parquet"
    pq.write_table(pa.Table.from_pandas(pd.DataFrame({"player_id": [1], "session_id": [1]})), cleaned)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame({"player_id": [1], "canonical_id": ["a"]})), mp)
    try:
        compile_canonical_patron_session_metrics(
            cleaned,
            mp,
            duckdb_runtime=DuckDbRuntimeConfig(),
            output_parquet=tmp_path / "out.parquet",
            duckdb_join_timeout_s=60.0,
        )
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "missing columns" in str(e).lower()
