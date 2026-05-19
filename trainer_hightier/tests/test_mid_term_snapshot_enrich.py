"""Tests for mid-term daily snapshot materializer and ASOF enrich."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.feature_experiment.dataset_enrich import enrich_training_parquet_with_cadence_suppliers
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    MID_TERM_SNAPSHOT_OUTPUT_COLUMNS,
    materialize_mid_term_daily_snapshot,
)


def _write_cleaned_bet(root: Path, rows: list[dict[str, object]]) -> None:
    part = root / "gaming_day=2026-05-18"
    part.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(part / "data.parquet", index=False)


def _write_mapping(path: Path) -> None:
    pd.DataFrame({"player_id": [1], "canonical_id": ["c1"]}).to_parquet(path, index=False)


def test_mid_term_snapshot_grain_is_canonical_anchor_day(tmp_path: Path) -> None:
    """Mid-term artifact must expose canonical_id + anchor_gaming_day (not bet_id)."""

    cleaned = tmp_path / "cleaned"
    _write_cleaned_bet(
        cleaned,
        [
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T10:00:00Z"),
                "wager": 100.0,
                "payout_odds": 2.0,
            },
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-17"),
                "payout_complete_dtm": pd.Timestamp("2026-05-17T12:00:00Z"),
                "wager": 50.0,
                "payout_odds": 1.5,
            },
        ],
    )
    cmap = tmp_path / "map.parquet"
    _write_mapping(cmap)
    out = tmp_path / "mid_snap.parquet"
    materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    schema = duckdb.sql(f"DESCRIBE SELECT * FROM read_parquet('{out.as_posix()}')").fetchdf()["column_name"]
    assert "canonical_id" in schema.tolist()
    assert "anchor_gaming_day" in schema.tolist()
    assert "bet_id" not in schema.tolist()
    for col in MID_TERM_SNAPSHOT_OUTPUT_COLUMNS:
        assert col in schema.tolist()


def test_asof_enrich_uses_prior_gaming_day_snapshot(tmp_path: Path) -> None:
    """Target gaming_day D must join anchor_gaming_day < D (typically D-1)."""

    base = tmp_path / "base.parquet"
    short = tmp_path / "short.parquet"
    mid = tmp_path / "mid.parquet"
    out = tmp_path / "enriched.parquet"

    pd.DataFrame(
        {
            "bet_id": [10.0],
            "canonical_id": ["c1"],
            "gaming_day": pd.Timestamp("2026-05-19"),
            "payout_odds": [2.5],
        }
    ).to_parquet(base, index=False)

    pd.DataFrame(
        {
            "bet_id": [10.0],
            "fe__wager_sum__w15m": [30.0],
            "fe__time_since_last_bet_sec": [120.0],
        }
    ).to_parquet(short, index=False)

    pd.DataFrame(
        {
            "canonical_id": ["c1", "c1"],
            "anchor_gaming_day": pd.to_datetime(["2026-05-17", "2026-05-18"]),
            "fe__bets_cnt__w1d": [1, 2],
            "fe__wager_sum__w1d": [50.0, 100.0],
            "fe__prior_odds_mean_w30d": [1.8, 1.9],
            "fe__prior_odds_std_w30d": [0.2, 0.2],
            "fe__std_wager_w7d": [10.0, 20.0],
            "fe__avg_abs_wager_w7d": [50.0, 50.0],
            "fe__interarrival_avg_w7d": [100.0, 100.0],
            "fe__interarrival_std_w7d": [10.0, 10.0],
        }
    ).to_parquet(mid, index=False)

    enrich_training_parquet_with_cadence_suppliers(
        base_training_parquet=base,
        fe_short_term_parquet=short,
        mid_term_snapshot_parquet=mid,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        short_term_columns=("fe__wager_sum__w15m", "fe__time_since_last_bet_sec"),
        mid_term_columns=(
            "fe__bets_cnt__w1d",
            "fe__wager_sum__w15m_over_w1d",
            "fe__wager_cv_w7d",
            "fe__payout_odds_z_prior_w30d",
            "fe__interarrival__last_gap_z__w7d",
        ),
    )
    got = pd.read_parquet(out)
    assert pd.Timestamp(got.iloc[0]["mid_term_anchor_gaming_day"]) == pd.Timestamp("2026-05-18")
    assert int(got.iloc[0]["mid_term_snapshot_age_days"]) == 1
    assert float(got.iloc[0]["fe__bets_cnt__w1d"]) == pytest.approx(2.0)
    assert float(got.iloc[0]["fe__wager_sum__w15m_over_w1d"]) == pytest.approx(0.3)
    assert float(got.iloc[0]["fe__wager_cv_w7d"]) == pytest.approx(0.4)
    assert float(got.iloc[0]["fe__interarrival__last_gap_z__w7d"]) == pytest.approx(2.0)


def test_asof_enrich_does_not_use_target_day_snapshot(tmp_path: Path) -> None:
    """Same-day anchor rows must not satisfy ASOF join for target gaming_day."""

    base = tmp_path / "base2.parquet"
    mid = tmp_path / "mid2.parquet"
    out = tmp_path / "enriched2.parquet"
    pd.DataFrame(
        {
            "bet_id": [11.0],
            "canonical_id": ["c2"],
            "gaming_day": pd.Timestamp("2026-05-19"),
            "payout_odds": [2.0],
        }
    ).to_parquet(base, index=False)
    pd.DataFrame(
        {
            "canonical_id": ["c2"],
            "anchor_gaming_day": pd.to_datetime(["2026-05-19"]),
            "fe__bets_cnt__w1d": [99],
            "fe__wager_sum__w1d": [999.0],
            "fe__prior_odds_mean_w30d": [1.0],
            "fe__prior_odds_std_w30d": [0.1],
            "fe__std_wager_w7d": [1.0],
            "fe__avg_abs_wager_w7d": [1.0],
            "fe__interarrival_avg_w7d": [1.0],
            "fe__interarrival_std_w7d": [1.0],
        }
    ).to_parquet(mid, index=False)

    enrich_training_parquet_with_cadence_suppliers(
        base_training_parquet=base,
        fe_short_term_parquet=None,
        mid_term_snapshot_parquet=mid,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        short_term_columns=(),
        mid_term_columns=("fe__bets_cnt__w1d",),
    )
    got = pd.read_parquet(out)
    assert pd.isna(got.iloc[0]["mid_term_anchor_gaming_day"])
    assert int(got.iloc[0]["mid_term_snapshot_missing_flag"]) == 1
