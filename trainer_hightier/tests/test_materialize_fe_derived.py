"""Tests for canonical-grained ``fe__*`` materialization."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decimal import Decimal

import pytest

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.feature_experiment.materialize_fe_derived import (
    compute_fe_derived_features_from_pool,
    materialize_fe_derived_parquet,
)


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
            "gaming_day_event": day,
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
            "gaming_day_event": day,
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


def test_compute_fe_derived_same_pcd_tie_break_by_bet_id() -> None:
    """Same ``pcd`` for two bets: ``ORDER BY pcd, bet_id`` makes LAG/window counts deterministic."""
    hk = "Asia/Hong_Kong"
    t0 = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    pool = pd.DataFrame(
        {
            "bet_id": [100.0, 200.0, 300.0],
            "player_id": [10, 10, 10],
            "canonical_id": ["c10", "c10", "c10"],
            "session_id": [1, 1, 1],
            "table_id": [1, 1, 1],
            "gaming_day_event": pd.to_datetime(["2025-06-01"] * 3),
            "payout_complete_dtm": [t0, t0, t0 + pd.Timedelta(minutes=5)],
            "wager": [10.0, 20.0, 30.0],
            "payout_odds": [2.0, 2.0, 2.0],
            "casino_win": [0.0, 0.0, 0.0],
        }
    )
    got = compute_fe_derived_features_from_pool(pool, pool["bet_id"])
    by_id = got.set_index("bet_id")
    # bet 200 is second at t0; prior count should include bet 100 only.
    assert float(by_id.loc[200.0, "fe__canonical__bets_cnt__today"]) == 1.0
    assert float(by_id.loc[200.0, "fe__canonical__wager_sum__today"]) == pytest.approx(10.0)
    assert float(by_id.loc[300.0, "fe__canonical__bets_cnt__today"]) == 2.0
    assert float(by_id.loc[300.0, "fe__canonical__wager_sum__today"]) == pytest.approx(30.0)


def test_compute_fe_derived_from_pool_schema_max_payout_odds() -> None:
    """``t_bet.payout_odds`` is Decimal(19,4) with metadata max 100.0000 (schema §4)."""
    hk = "Asia/Hong_Kong"
    t0 = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    pool = pd.DataFrame(
        {
            "bet_id": [1.0, 2.0],
            "player_id": [10, 10],
            "canonical_id": ["c10", "c10"],
            "session_id": [1, 1],
            "table_id": [1, 1],
            "gaming_day_event": pd.to_datetime(["2025-06-01", "2025-06-01"]),
            "payout_complete_dtm": [t0, t0 + pd.Timedelta(minutes=5)],
            "wager": [Decimal("50.0000"), Decimal("100.0000")],
            "payout_odds": [Decimal("2.0000"), Decimal("100.0000")],
            "casino_win": [Decimal("0.0000"), Decimal("0.0000")],
        }
    )
    got = compute_fe_derived_features_from_pool(pool, pool.loc[[1], "bet_id"])
    assert not got.empty
    ratio = float(got.iloc[0]["fe__odds__payout_odds_to_recent_max_ratio__w1h"])
    assert ratio == pytest.approx(50.0)


def test_compute_fe_derived_same_pcd_peer_casino_win_inclusive_minus_self() -> None:
    """Same-pcd siblings contribute to outcome momentum (inclusive window minus scored bet)."""
    hk = "Asia/Hong_Kong"
    t0 = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    pool = pd.DataFrame(
        {
            "bet_id": [100.0, 200.0],
            "player_id": [10, 10],
            "canonical_id": ["c10", "c10"],
            "session_id": [1, 1],
            "table_id": [1, 1],
            "gaming_day_event": pd.to_datetime(["2025-06-01", "2025-06-01"]),
            "payout_complete_dtm": [t0, t0],
            "wager": [10.0, 20.0],
            "payout_odds": [2.0, 2.0],
            "casino_win": [10.0, 20.0],
            "theo_win": [5.0, 8.0],
        }
    )
    got = compute_fe_derived_features_from_pool(pool, pd.Series([200.0]))
    row = got.iloc[0]
    assert float(row["fe__outcome__casino_win_sum__w15m"]) == pytest.approx(10.0)
    assert float(row["fe__outcome__casino_win_sum__w1h"]) == pytest.approx(10.0)
    assert float(row["fe__outcome__casino_win_to_theo_ratio__w1h"]) == pytest.approx(2.0)


def test_ensure_etl_observed_at_for_pit_preserves_real_etl() -> None:
    """Offline staging must not overwrite populated ``__etl_insert_Dtm`` with payout time."""
    from trainer_hightier.serving.feature_builder import ensure_etl_observed_at_for_pit

    hk = "Asia/Hong_Kong"
    pcd = pd.Timestamp("2025-06-01 10:00:00", tz=hk)
    etl = pcd + pd.Timedelta(minutes=5)
    bets = pd.DataFrame(
        {
            "bet_id": [1.0],
            "payout_complete_dtm": [pcd],
            "__etl_insert_Dtm": [etl],
        }
    )
    got = ensure_etl_observed_at_for_pit(bets)
    assert pd.Timestamp(got.iloc[0]["__etl_insert_Dtm"]).tz_convert(hk) == etl
