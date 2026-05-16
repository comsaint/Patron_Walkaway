"""Tests for canonical-grained ``fe__*`` materialization."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.feature_experiment.materialize_fe_derived import materialize_fe_derived_parquet


def test_materialize_fe_derived_prior_15m_includes_sibling_player_id(tmp_path: Path) -> None:
    """Training bet on player B; prior bet on player A; same canonical → 15m count includes A."""
    t0 = pd.Timestamp("2024-06-01 08:00:00", tz="UTC")
    pit = t0 + pd.Timedelta(hours=2)
    day = t0.tz_convert("Asia/Hong_Kong").date()
    rows = [
        {
            "bet_id": 101.0,
            "player_id": 1,
            "session_id": 501,
            "table_id": 10,
            "gaming_day": day,
            "payout_complete_dtm": t0,
            "wager": 100.0,
            "payout_odds": 2.0,
            "casino_win": -5.0,
            "theo_win": 1.0,
            "base_ha": 0.01,
        },
        {
            "bet_id": 102.0,
            "player_id": 2,
            "session_id": 502,
            "table_id": 10,
            "gaming_day": day,
            "payout_complete_dtm": t0 + pd.Timedelta(minutes=10),
            "wager": 50.0,
            "payout_odds": 3.0,
            "casino_win": 1.0,
            "theo_win": 2.0,
            "base_ha": 0.02,
        },
    ]
    cleaned = tmp_path / "cleaned_bets.parquet"
    train = tmp_path / "training.parquet"
    cmap = tmp_path / "canonical_player_mapping.parquet"
    out = tmp_path / "fe_derived.parquet"

    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), cleaned)
    pq.write_table(pa.Table.from_pandas(pd.DataFrame([{"bet_id": 102.0}])), train)
    pq.write_table(
        pa.Table.from_pandas(
            pd.DataFrame(
                [
                    {"player_id": 1, "canonical_id": "patron_x"},
                    {"player_id": 2, "canonical_id": "patron_x"},
                ]
            )
        ),
        cmap,
    )

    materialize_fe_derived_parquet(
        cleaned_bet_parquet=cleaned,
        training_parquet_for_bet_ids=train,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )

    got = pq.read_table(out).to_pandas()
    r = got[got["bet_id"] == 102.0].iloc[0]
    assert int(r["fe__bets_cnt__w15m"]) == 1
    assert abs(float(r["fe__wager_sum__w15m"]) - 100.0) < 1e-6
    assert float(r["fe__session__bet_idx_in_session"]) == 1.0
