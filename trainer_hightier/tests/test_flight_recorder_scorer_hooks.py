"""Tests for scorer flight recorder hooks (no ClickHouse)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.serving.feature_supply import ScorerSupplierPlan
from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.context import RecorderContext
from trainer_hightier.serving.flight_recorder.scorer_hooks import (
    ScorerCycleRecorder,
    _ScoringBatchView,
)
from trainer_hightier.serving.flight_recorder.session import attach_scorer_recorder


def _minimal_plan() -> ScorerSupplierPlan:
    """Tiny supplier plan for provenance tests."""
    return ScorerSupplierPlan(
        baseline_cols=("wager",),
        feast_trial_cols=(),
        feast_mid_cols=("fe__bets_cnt__w1d",),
        feast_slow_cols=(),
        mid_composite_cols=(),
        short_term_cols=("wager",),
        unknown_cols=(),
    )


def test_scorer_cycle_recorder_writes_tree(tmp_path: Path) -> None:
    """One mocked cycle produces clickhouse/, stages/, and audits/ artifacts."""
    recording_root = tmp_path / "recording"
    recording_root.mkdir()
    ctx = RecorderContext.open(
        tmp_path / "bundle",
        FlightRecorderConfig(
            recording_root=str(recording_root),
            capture_scorer_stages=True,
        ),
        rel={},
        model_version="test-v1",
    )
    rec = ScorerCycleRecorder.from_recorder_context(ctx)
    rec.begin_cycle(
        high_adt_only=True,
        allowlist_size=2,
        last_etl=None,
        lookback_hours=6.0,
        limit_rows=100,
    )
    bets = pd.DataFrame(
        {
            "bet_id": ["1", "2"],
            "player_id": [10, 20],
            "payout_complete_dtm": pd.to_datetime(
                ["2026-01-01 12:00:00", "2026-01-01 12:05:00"],
                utc=True,
            ),
            "wager": [100.0, 200.0],
            "game_id": [1, 2],
        }
    )
    pool = bets.copy()
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    batch = _ScoringBatchView(bets=bets, pool=pool, cursor=pd.Series([now, now]))
    rec.capture_batch(
        batch,
        last_etl=None,
        lookback_hours=6.0,
        limit_rows=100,
        allowlist_ids=frozenset({10, 20}),
        high_adt_only=True,
        pool_window_start=now,
        pool_window_end=now,
    )
    rec.record_stage("stage_05_staged_features", bets)
    staged = bets.assign(score=0.5)
    features = pd.DataFrame({"wager": [100.0, 200.0], "fe__bets_cnt__w1d": [1.0, 2.0]})
    rec.finish_cycle(
        n_batch_rows=2,
        n_alerts=0,
        prob=pd.Series([0.5, 0.5]).to_numpy(),
        staged=staged,
        features=features,
        feature_columns=("wager", "fe__bets_cnt__w1d"),
        supplier_plan=_minimal_plan(),
        row_audits=None,
        cycle_readiness={"n_scored": 2},
    )
    assert rec.cycle_dir is not None
    ch_dir = rec.cycle_dir / "clickhouse"
    assert (ch_dir / "incremental_t_bet.final.parquet").is_file()
    assert (ch_dir / "incremental_t_bet.query.json").is_file()
    inc_query = json.loads((ch_dir / "incremental_t_bet.query.json").read_text(encoding="utf-8"))
    assert inc_query["requeryable"] is True
    assert "..." not in inc_query["sql_final"]
    assert inc_query["external_inputs"]["allowlist_player_ids"] == [10, 20]
    pool_query = json.loads((ch_dir / "short_term_pool_t_bet.query.json").read_text(encoding="utf-8"))
    assert pool_query["requeryable"] is True
    assert "..." not in pool_query["sql_final"]
    assert (rec.cycle_dir / "stages" / "stage_05_staged_features.parquet").is_file()
    assert (rec.cycle_dir / "stages" / "stage_09_scores.parquet").is_file()
    assert (rec.cycle_dir / "audits" / "feature_missing_provenance.parquet").is_file()
    assert (rec.cycle_dir / "audits" / "row_counts.json").is_file()
    manifest = json.loads((rec.cycle_dir / "cycle_manifest.json").read_text(encoding="utf-8"))
    assert manifest["model_version"] == "test-v1"


def test_attach_scorer_recorder_disabled_is_noop() -> None:
    """When recorder is None, hooks do not raise."""
    attach_scorer_recorder(None)
    from trainer_hightier.serving.flight_recorder.scorer_hooks import on_score_once_begin

    on_score_once_begin(high_adt_only=False, allowlist_ids=frozenset(), last_etl=None)
