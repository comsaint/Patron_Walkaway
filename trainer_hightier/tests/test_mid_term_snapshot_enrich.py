"""Tests for mid-term daily snapshot materializer and ASOF enrich."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    MID_TERM_SNAPSHOT_SCOPE_TRAINING,
)
from trainer_hightier.feature_experiment.dataset_enrich import enrich_training_parquet_with_cadence_suppliers
from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
    MID_TERM_SNAPSHOT_OUTPUT_COLUMNS,
    compute_training_mid_term_bounds,
    materialize_mid_term_daily_snapshot,
    mid_term_snapshot_production_safe,
    write_training_canonical_universe_parquet,
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


def test_training_snapshot_scope_metadata(tmp_path: Path) -> None:
    """Training-scoped materialization must record scope in sidecar metadata."""

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
        ],
    )
    cmap = tmp_path / "map.parquet"
    _write_mapping(cmap)
    training = tmp_path / "train.parquet"
    pd.DataFrame(
        {
            "bet_id": [1.0],
            "canonical_id": ["c1"],
            "gaming_day": pd.Timestamp("2026-05-19"),
        }
    ).to_parquet(training, index=False)
    universe = tmp_path / "universe.parquet"
    write_training_canonical_universe_parquet(
        training,
        universe,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    anchor_start, anchor_end, bets_start, bets_end = compute_training_mid_term_bounds(
        training,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    out = tmp_path / "mid_snap.parquet"
    _, meta = materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        canonical_universe_parquet=universe,
        anchor_gaming_day_start=anchor_start,
        anchor_gaming_day_end=anchor_end,
        bets_gaming_day_start=bets_start,
        bets_gaming_day_end=bets_end,
        snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_TRAINING,
    )
    assert meta["snapshot_scope"] == MID_TERM_SNAPSHOT_SCOPE_TRAINING
    sidecar = json.loads((tmp_path / "mid_snap.meta.json").read_text(encoding="utf-8"))
    assert sidecar["snapshot_scope"] == MID_TERM_SNAPSHOT_SCOPE_TRAINING
    assert not mid_term_snapshot_production_safe(meta)


def test_canonical_universe_filters_snapshot_rows(tmp_path: Path) -> None:
    """Universe semi-join must exclude canonical_ids outside training scope."""

    cleaned = tmp_path / "cleaned2"
    part = cleaned / "gaming_day=2026-05-18"
    part.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T10:00:00Z"),
                "wager": 100.0,
                "payout_odds": 2.0,
            },
            {
                "player_id": 2,
                "gaming_day": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T11:00:00Z"),
                "wager": 50.0,
                "payout_odds": 1.5,
            },
        ]
    ).to_parquet(part / "data.parquet", index=False)
    cmap = tmp_path / "map2.parquet"
    pd.DataFrame(
        {
            "player_id": [1, 2],
            "canonical_id": ["c1", "c2"],
        }
    ).to_parquet(cmap, index=False)
    universe = tmp_path / "universe2.parquet"
    pd.DataFrame({"canonical_id": ["c1"]}).to_parquet(universe, index=False)
    out = tmp_path / "mid_filtered.parquet"
    materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        canonical_universe_parquet=universe,
        anchor_gaming_day_end=pd.Timestamp("2026-05-18").date(),
        bets_gaming_day_end=pd.Timestamp("2026-05-18").date(),
        snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_TRAINING,
    )
    ids = duckdb.sql(
        f"SELECT DISTINCT canonical_id FROM read_parquet('{out.as_posix()}')",
    ).fetchdf()["canonical_id"].tolist()
    assert ids == ["c1"]


def test_production_scope_metadata_is_deploy_safe() -> None:
    """Production scope metadata must pass deploy guardrail helper."""

    meta = {"snapshot_scope": MID_TERM_SNAPSHOT_SCOPE_PRODUCTION}
    assert mid_term_snapshot_production_safe(meta)
    assert not mid_term_snapshot_production_safe({"snapshot_scope": MID_TERM_SNAPSHOT_SCOPE_TRAINING})
    assert not mid_term_snapshot_production_safe(None)


def test_day_end_snapshot_uses_last_bet_inclusive_state(tmp_path: Path) -> None:
    """Day-end snapshot must include all same-day bets (inclusive rolling state)."""

    cleaned = tmp_path / "cleaned_day_end"
    part17 = cleaned / "gaming_day=2026-05-17"
    part18 = cleaned / "gaming_day=2026-05-18"
    part17.mkdir(parents=True)
    part18.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-17"),
                "payout_complete_dtm": pd.Timestamp("2026-05-17T12:00:00Z"),
                "wager": 50.0,
                "payout_odds": 1.5,
            },
        ]
    ).to_parquet(part17 / "data.parquet", index=False)
    pd.DataFrame(
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
                "gaming_day": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T15:00:00Z"),
                "wager": 200.0,
                "payout_odds": 2.5,
            },
        ]
    ).to_parquet(part18 / "data.parquet", index=False)
    cmap = tmp_path / "map_day_end.parquet"
    _write_mapping(cmap)
    out = tmp_path / "mid_day_end.parquet"
    materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
    )
    got = duckdb.sql(
        f"""
        SELECT *
        FROM read_parquet('{out.as_posix()}')
        WHERE canonical_id = 'c1'
          AND CAST(anchor_gaming_day AS DATE) = DATE '2026-05-18'
        """
    ).fetchdf()
    assert len(got) == 1
    row = got.iloc[0]
    assert int(row["fe__bets_cnt__w1d"]) == 2
    assert float(row["fe__wager_sum__w1d"]) == pytest.approx(300.0)
    assert int(row["fe__bets_cnt__w7d"]) == 3
    assert float(row["fe__wager_sum__w7d"]) == pytest.approx(350.0)
    assert float(row["fe__payout_odds_avg_w7d"]) == pytest.approx(2.0)
    assert float(row["fe__payout_odds_std_w7d"]) == pytest.approx(0.408248290463863, rel=1e-6)


def test_enrich_training_payout_odds_z_w7d_from_mid_snapshot(tmp_path: Path) -> None:
    """7d odds z-score training enrich uses prior-day mid snapshot stats + bet odds."""

    cleaned = tmp_path / "cleaned_odds_z"
    _write_cleaned_bet(
        cleaned,
        [
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-17"),
                "payout_complete_dtm": pd.Timestamp("2026-05-17T12:00:00Z"),
                "wager": 50.0,
                "payout_odds": 1.5,
            },
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T10:00:00Z"),
                "wager": 100.0,
                "payout_odds": 2.0,
            },
        ],
    )
    cmap = tmp_path / "map_odds_z.parquet"
    _write_mapping(cmap)
    mid = tmp_path / "mid_odds_z.parquet"
    materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned,
        out_parquet=mid,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    )
    base = tmp_path / "base_odds_z.parquet"
    pd.DataFrame(
        {
            "bet_id": [42.0],
            "canonical_id": ["c1"],
            "gaming_day": pd.Timestamp("2026-05-19"),
            "payout_odds": [2.5],
        }
    ).to_parquet(base, index=False)
    train_out = tmp_path / "train_odds_z.parquet"
    enrich_training_parquet_with_cadence_suppliers(
        base_training_parquet=base,
        fe_short_term_parquet=None,
        mid_term_snapshot_parquet=mid,
        out_parquet=train_out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        short_term_columns=(),
        mid_term_columns=("fe__odds__payout_odds_z__w7d",),
    )
    got = pd.read_parquet(train_out)
    avg = float(
        duckdb.sql(
            f"""
            SELECT fe__payout_odds_avg_w7d
            FROM read_parquet('{mid.as_posix()}')
            WHERE canonical_id = 'c1'
              AND CAST(anchor_gaming_day AS DATE) = DATE '2026-05-18'
            """
        ).fetchone()[0]
    )
    std = float(
        duckdb.sql(
            f"""
            SELECT fe__payout_odds_std_w7d
            FROM read_parquet('{mid.as_posix()}')
            WHERE canonical_id = 'c1'
              AND CAST(anchor_gaming_day AS DATE) = DATE '2026-05-18'
            """
        ).fetchone()[0]
    )
    assert float(got.iloc[0]["fe__odds__payout_odds_z__w7d"]) == pytest.approx((2.5 - avg) / std)


def test_asof_enrich_nulls_anchor_older_than_backfill_window(tmp_path: Path) -> None:
    """Anchor older than N=30 gaming days must not satisfy bounded ASOF join."""

    base = tmp_path / "base_old.parquet"
    mid = tmp_path / "mid_old.parquet"
    out = tmp_path / "enriched_old.parquet"
    pd.DataFrame(
        {
            "bet_id": [20.0],
            "canonical_id": ["c_old"],
            "gaming_day": pd.Timestamp("2026-05-19"),
            "payout_odds": [2.0],
        }
    ).to_parquet(base, index=False)
    pd.DataFrame(
        {
            "canonical_id": ["c_old"],
            "anchor_gaming_day": pd.to_datetime(["2026-03-01"]),
            "fe__bets_cnt__w1d": [7],
            "fe__wager_sum__w1d": [70.0],
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
    assert pd.isna(got.iloc[0]["fe__bets_cnt__w1d"])
    assert int(got.iloc[0]["mid_term_snapshot_missing_flag"]) == 1


def test_train_serve_mid_term_asof_parity(tmp_path: Path) -> None:
    """Training enrich and production ASOF join must pick the same mid-term anchor/value."""

    from trainer_hightier.serving.feature_builder import join_production_fe_suppliers

    cleaned = tmp_path / "cleaned_parity"
    _write_cleaned_bet(
        cleaned,
        [
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-17"),
                "payout_complete_dtm": pd.Timestamp("2026-05-17T12:00:00Z"),
                "wager": 50.0,
                "payout_odds": 1.5,
            },
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T10:00:00Z"),
                "wager": 100.0,
                "payout_odds": 2.0,
            },
        ],
    )
    cmap = tmp_path / "map_parity.parquet"
    _write_mapping(cmap)
    mid = tmp_path / "mid_parity.parquet"
    materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned,
        out_parquet=mid,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_PRODUCTION,
    )
    base = tmp_path / "base_parity.parquet"
    pd.DataFrame(
        {
            "bet_id": [42.0],
            "canonical_id": ["c1"],
            "gaming_day": pd.Timestamp("2026-05-19"),
            "payout_odds": [2.5],
        }
    ).to_parquet(base, index=False)
    train_out = tmp_path / "train_enriched.parquet"
    enrich_training_parquet_with_cadence_suppliers(
        base_training_parquet=base,
        fe_short_term_parquet=None,
        mid_term_snapshot_parquet=mid,
        out_parquet=train_out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        short_term_columns=(),
        mid_term_columns=("fe__bets_cnt__w1d",),
    )
    serve_bets = pd.read_parquet(base)
    serve_out = join_production_fe_suppliers(
        serve_bets,
        fe_short_term_parquet=None,
        mid_term_snapshot_parquet=mid,
        short_term_columns=(),
        mid_term_columns=("fe__bets_cnt__w1d",),
    )
    train_val = float(pd.read_parquet(train_out).iloc[0]["fe__bets_cnt__w1d"])
    serve_val = float(serve_out.iloc[0]["fe__bets_cnt__w1d"])
    assert train_val == serve_val == pytest.approx(1.0)


def test_mid_term_snapshot_cache_reuse(tmp_path: Path) -> None:
    """Unchanged inputs should reuse cached training mid-term snapshot."""

    cleaned = tmp_path / "cleaned3"
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
        ],
    )
    cmap = tmp_path / "map3.parquet"
    _write_mapping(cmap)
    training = tmp_path / "train3.parquet"
    pd.DataFrame(
        {
            "bet_id": [1.0],
            "canonical_id": ["c1"],
            "gaming_day": pd.Timestamp("2026-05-19"),
        }
    ).to_parquet(training, index=False)
    universe = tmp_path / "universe3.parquet"
    write_training_canonical_universe_parquet(
        training,
        universe,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    anchor_start, anchor_end, bets_start, bets_end = compute_training_mid_term_bounds(
        training,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )
    out = tmp_path / "mid_cache.parquet"
    from trainer_hightier.feature_experiment.materialize_mid_term_daily_snapshot import (
        try_reuse_mid_term_snapshot_cache,
    )

    materialize_mid_term_daily_snapshot(
        cleaned_bet_parquet=cleaned,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        canonical_mapping_parquet=cmap,
        canonical_universe_parquet=universe,
        anchor_gaming_day_start=anchor_start,
        anchor_gaming_day_end=anchor_end,
        bets_gaming_day_start=bets_start,
        bets_gaming_day_end=bets_end,
        snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_TRAINING,
    )
    cached = try_reuse_mid_term_snapshot_cache(
        out,
        snapshot_scope=MID_TERM_SNAPSHOT_SCOPE_TRAINING,
        cleaned_bet_parquet=cleaned,
        canonical_mapping_parquet=cmap,
        canonical_universe_parquet=universe,
        lookback_days=32,
        anchor_gaming_day_start=anchor_start,
        anchor_gaming_day_end=anchor_end,
    )
    assert cached is not None
    _, meta = cached
    assert meta.get("cache_hit") is True
