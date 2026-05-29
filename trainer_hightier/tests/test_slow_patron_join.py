"""Tests for ``join_slow_patron_snapshot`` snapshot schema compatibility."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.serving.feature_builder import join_slow_patron_snapshot


def test_join_slow_patron_feast_bet_grain_left_merge(tmp_path: Path) -> None:
    """Trainer materializes slow parquet at bet-grain with join-on bet_id semantics."""

    slow = tmp_path / "slow_fg.parquet"
    pit = pd.to_datetime(pd.Series(["2025-06-01T12:00:00Z", "2025-06-02T08:00:00Z"]))
    synth = pit
    pd.DataFrame(
        {
            "bet_id": [10.0, 20.0],
            "prediction_visible_ts_cf": pit,
            "__etl_insert_Dtm_synthetic": synth,
            "patron__theo_win_sum__w180d_m1snap": [11.0, 22.0],
            "patron__gaming_days_cnt__w180d_m1snap": [1, 3],
            "patron__adt__w180d_m1snap": [11.0, 44.0 / 3.0],
        }
    ).to_parquet(slow, index=False)

    bets = pd.DataFrame({"bet_id": [20.0], "gaming_day_event": pd.to_datetime(["2025-07-01"])})
    got = join_slow_patron_snapshot(bets, Path(slow))
    assert len(got) == 1
    assert float(got.iloc[0]["patron__adt__w180d_m1snap"]) == pytest.approx(44.0 / 3.0)


def test_join_slow_patron_inference_anchor_column(tmp_path: Path) -> None:
    """Player-grain slow Parquet joins on ``anchor_gaming_day_event``."""

    slow = tmp_path / "slow.parquet"
    pd.DataFrame(
        {
            "player_id": [1, 1],
            "anchor_gaming_day_event": pd.to_datetime(["2025-01-01", "2025-02-01"]),
            "patron__theo_win_sum__w180d_m1snap": [10.0, 20.0],
            "patron__gaming_days_cnt__w180d_m1snap": [1, 2],
            "patron__adt__w180d_m1snap": [10.0, 10.0],
        }
    ).to_parquet(slow, index=False)

    bets = pd.DataFrame(
        {
            "bet_id": [99.0],
            "player_id": [1],
            "gaming_day_event": pd.to_datetime(["2025-02-10"]),
        }
    )

    got = join_slow_patron_snapshot(bets, Path(slow))
    assert len(got) == 1
    assert float(got.iloc[0]["patron__theo_win_sum__w180d_m1snap"]) == 20.0


def test_join_slow_patron_canonical_asof_grain(tmp_path: Path) -> None:
    """Production canonical ASOF parquet joins on canonical_id (not training bet_id)."""

    slow = tmp_path / "slow_canon.parquet"
    pd.DataFrame(
        {
            "canonical_id": ["c1", "c1"],
            "anchor_gaming_day_event": pd.to_datetime(["2025-01-01", "2025-06-01"]),
            "patron__theo_win_sum__w180d_m1snap": [10.0, 50.0],
            "patron__gaming_days_cnt__w180d_m1snap": [1, 5],
            "patron__adt__w180d_m1snap": [10.0, 10.0],
        }
    ).to_parquet(slow, index=False)

    bets = pd.DataFrame(
        {
            "bet_id": [99.0],
            "canonical_id": ["c1"],
            "gaming_day_event": pd.to_datetime(["2025-06-15"]),
        }
    )
    got = join_slow_patron_snapshot(bets, Path(slow), slow_grain="canonical_asof")
    assert float(got.iloc[0]["patron__theo_win_sum__w180d_m1snap"]) == 50.0


def test_join_slow_patron_prefers_anchor_gaming_day_event_when_present(tmp_path: Path) -> None:
    """When both anchors exist (should be rare), prefer ``anchor_gaming_day_event`` like training DSL."""

    slow = tmp_path / "slow2.parquet"
    pd.DataFrame(
        {
            "player_id": [1],
            "anchor_gaming_day_event": pd.to_datetime(["2025-06-01"]),
            "gaming_day_event": pd.to_datetime(["2025-05-01"]),
            "patron__theo_win_sum__w180d_m1snap": [123.0],
            "patron__gaming_days_cnt__w180d_m1snap": [3],
            "patron__adt__w180d_m1snap": [41.0],
        }
    ).to_parquet(slow, index=False)

    bets = pd.DataFrame(
        {
            "bet_id": [1.0],
            "player_id": [1],
            "gaming_day_event": pd.to_datetime(["2025-06-15"]),
        }
    )

    got = join_slow_patron_snapshot(bets, Path(slow))
    assert float(got.iloc[0]["patron__theo_win_sum__w180d_m1snap"]) == 123.0
