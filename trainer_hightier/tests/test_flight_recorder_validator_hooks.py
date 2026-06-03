"""Tests for validator flight recorder hooks."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.context import RecorderContext
from trainer_hightier.serving.flight_recorder.validator_hooks import ValidatorCycleRecorder


def test_validator_cycle_recorder_writes_tree(tmp_path: Path) -> None:
    """One mocked validator cycle produces clickhouse/, alerts/, decisions/."""
    ctx = RecorderContext.open(
        tmp_path / "bundle",
        FlightRecorderConfig(
            recording_root=str(tmp_path / "recording"),
            capture_validator_stages=True,
        ),
        rel={},
        model_version="test-v1",
    )
    rec = ValidatorCycleRecorder.from_recorder_context(ctx)
    pending = pd.DataFrame(
        {
            "bet_id": ["101"],
            "player_id": [1],
            "ts": ["2026-01-01T12:00:00+08:00"],
            "bet_ts": ["2026-01-01T12:00:00+08:00"],
            "score": [0.9],
        }
    )
    rec.begin_cycle(n_alerts=1, n_pending=1)
    rec.capture_pending_alerts(pending)
    now = datetime.now(timezone.utc)
    ch_df = pd.DataFrame(
        {"player_id": [1], "payout_complete_dtm": [now]},
    )
    rec.capture_canonical_fetch([ch_df], n_players=1, fetch_start=now, fetch_end=now)
    rec.record_decision(
        {
            "bet_id": "101",
            "result": True,
            "reason": "MATCH",
            "gap_start": None,
            "gap_minutes": 30,
            "validated_at": now.isoformat(),
        }
    )
    rec.finish_cycle(verified_count=1)
    assert rec.cycle_dir is not None
    assert (rec.cycle_dir / "clickhouse" / "fetch_bets_by_canonical_id.final.parquet").is_file()
    assert (rec.cycle_dir / "alerts" / "pending_alerts.parquet").is_file()
    assert (rec.cycle_dir / "decisions" / "decision_trace.parquet").is_file()
    trace = pd.read_parquet(rec.cycle_dir / "decisions" / "decision_trace.parquet")
    assert trace.iloc[0]["reason"] == "MATCH"
    manifest = json.loads((rec.cycle_dir / "cycle_manifest.json").read_text(encoding="utf-8"))
    assert manifest["verified_this_cycle"] == 1
