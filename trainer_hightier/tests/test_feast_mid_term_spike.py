"""Unit tests for Feast mid-term feasibility spike (no ClickHouse)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.feature_experiment.feast_mid_term_spike import (
    SPIKE_MID_TERM_FEATURE_COLUMNS,
    FeastMidTermSpikeConfig,
    _add_event_timestamp_column,
    compute_mid_term_spike_snapshot,
    run_spike,
)


def _write_cleaned_bet(root: Path, rows: list[dict[str, object]]) -> None:
    part = root / "gaming_day=2026-05-18"
    part.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(part / "data.parquet", index=False)


def _write_mapping(path: Path) -> None:
    pd.DataFrame({"player_id": [1, 2], "canonical_id": ["c1", "c2"]}).to_parquet(path, index=False)


def test_add_event_timestamp_collapses_to_latest_anchor(tmp_path: Path) -> None:
    full = tmp_path / "full.parquet"
    pd.DataFrame(
        {
            "canonical_id": ["c1", "c1"],
            "anchor_gaming_day": [pd.Timestamp("2026-05-17"), pd.Timestamp("2026-05-18")],
            "fe__wager_sum__w7d": [10.0, 20.0],
            "fe__wager_sum__w30d": [100.0, 200.0],
            "fe__prior_wager_mean_w30d": [1.0, 2.0],
        }
    ).to_parquet(full, index=False)
    out = tmp_path / "feast.parquet"
    nrows = _add_event_timestamp_column(full, out)
    assert nrows == 1
    df = pd.read_parquet(out)
    assert list(df.columns) == ["canonical_id", *SPIKE_MID_TERM_FEATURE_COLUMNS, "event_timestamp"]
    assert float(df.iloc[0]["fe__wager_sum__w7d"]) == 20.0


def test_compute_mid_term_spike_snapshot_local(tmp_path: Path) -> None:
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
    feast_art = tmp_path / "feast"
    cfg = FeastMidTermSpikeConfig(
        feast_repo=tmp_path / "feast_repo",
        spike_parquet=feast_art / "mid_term_spike_canonical.parquet",
        staging_dir=feast_art / "staging",
        canonical_mapping_parquet=cmap,
        duckdb_runtime=DuckDbRuntimeConfig(),
        anchor_days=1,
    )
    out, meta = compute_mid_term_spike_snapshot(
        cleaned_bet_path=cleaned,
        cfg=cfg,
        canonical_universe_parquet=None,
        anchor_start=pd.Timestamp("2026-05-17").date(),
        anchor_end=pd.Timestamp("2026-05-17").date(),
        bets_gday_start=pd.Timestamp("2026-04-01").date(),
        bets_gday_end=pd.Timestamp("2026-05-17").date(),
    )
    assert out.is_file()
    assert meta["feast_spike_rows"] >= 1
    df = pd.read_parquet(out)
    for col in SPIKE_MID_TERM_FEATURE_COLUMNS:
        assert col in df.columns


def test_run_spike_end_to_end_local_mock_feast(tmp_path: Path) -> None:
    cleaned = tmp_path / "cleaned"
    _write_cleaned_bet(
        cleaned,
        [
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-18"),
                "payout_complete_dtm": pd.Timestamp("2026-05-18T10:00:00Z"),
                "wager": 80.0,
                "payout_odds": 2.0,
            },
            {
                "player_id": 1,
                "gaming_day": pd.Timestamp("2026-05-17"),
                "payout_complete_dtm": pd.Timestamp("2026-05-17T11:00:00Z"),
                "wager": 40.0,
                "payout_odds": 1.5,
            },
        ],
    )
    cmap = tmp_path / "map.parquet"
    _write_mapping(cmap)
    feast_repo = Path(__file__).resolve().parents[1] / "feast_repo"
    feast_art = tmp_path / "feast"
    report_path = feast_art / "report.json"
    cfg = FeastMidTermSpikeConfig(
        feast_repo=feast_repo,
        spike_parquet=feast_art / "mid_term_spike_canonical.parquet",
        staging_dir=feast_art / "staging",
        report_path=report_path,
        canonical_mapping_parquet=cmap,
        local_cleaned_bet=cleaned,
        bet_source="local_cleaned",
        sample_mode="wider_sample",
        anchor_days=1,
        duckdb_runtime=DuckDbRuntimeConfig(),
    )

    def _fake_apply(repo: Path) -> float:
        return 0.01

    def _fake_materialize(repo: Path, *, start, end) -> float:
        return 0.02

    def _fake_lookup(repo: Path, *, canonical_ids: list[str], batch_size: int) -> dict:
        return {
            "lookup_batch_size": min(len(canonical_ids), batch_size),
            "lookup_ok_rows": len(canonical_ids),
            "lookup_missing_by_feature": {c: 0 for c in SPIKE_MID_TERM_FEATURE_COLUMNS},
            "lookup_latency_ms_p50": 1.0,
            "lookup_latency_ms_p95": 2.0,
        }

    bounds = (
        date(2026, 5, 17),
        date(2026, 5, 17),
        date(2026, 4, 1),
        date(2026, 5, 17),
    )
    with (
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike._anchor_bounds",
            return_value=bounds,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike.run_feast_apply",
            side_effect=_fake_apply,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike.run_feast_materialize",
            side_effect=_fake_materialize,
        ),
        patch(
            "trainer_hightier.feature_experiment.feast_mid_term_spike.run_online_lookup_smoke",
            side_effect=_fake_lookup,
        ),
    ):
        report = run_spike(cfg)

    assert report["verdict"] in ("pass", "marginal", "fail")
    assert report_path.is_file()
    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert loaded["bet_source"] == "local_cleaned"
    assert "compute_seconds_total" in loaded
