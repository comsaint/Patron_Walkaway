"""Tests for production flight recorder fail-fast policy."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.serving.flight_recorder.config import FlightRecorderConfig
from trainer_hightier.serving.flight_recorder.context import RecorderContext
from trainer_hightier.serving.flight_recorder.failure import (
    FlightRecorderFatalError,
    validate_recording_root_writable,
)
from trainer_hightier.serving.flight_recorder.init_recording import init_recording_root
from trainer_hightier.serving.flight_recorder.scorer_hooks import (
    ScorerCycleRecorder,
    _ScoringBatchView,
)


def test_config_defaults_fail_fast_production_grade() -> None:
    """Production recorder config defaults to fail-fast with production evidence."""
    cfg = FlightRecorderConfig()
    assert cfg.fail_fast is True
    assert cfg.evidence_grade == "production"


def test_config_fail_open_sets_debug_grade() -> None:
    """Explicit fail-open config marks evidence as debug-only."""
    cfg = FlightRecorderConfig.from_mapping({"fail_fast": False})
    assert cfg.fail_fast is False
    assert cfg.evidence_grade == "debug_only"


def test_init_raises_when_recording_root_not_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-fast init must halt before serving when recording root cannot be written."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "deploy_bundle_paths.json").write_text("{}", encoding="utf-8")
    (bundle / "models").mkdir()
    (bundle / "models" / "model_version").write_text("test-v1", encoding="utf-8")
    config = FlightRecorderConfig(fail_fast=True, recording_root="local_state/flight_recording")

    def _probe_fail(_root: Path, *, fail_fast: bool) -> None:
        if fail_fast:
            raise FlightRecorderFatalError("recording root not writable: mocked")

    monkeypatch.setattr(
        "trainer_hightier.serving.flight_recorder.init_recording.validate_recording_root_writable",
        _probe_fail,
    )
    with pytest.raises(FlightRecorderFatalError, match="not writable"):
        init_recording_root(bundle, config, write_default_config=False, export_sqlite=False)


def test_scorer_hook_fail_fast_raises_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Required scorer artifact failure stops the cycle when fail_fast=true."""
    ctx = RecorderContext.open(
        tmp_path / "bundle",
        FlightRecorderConfig(
            recording_root=str(tmp_path / "recording"),
            capture_scorer_stages=True,
            fail_fast=True,
        ),
        rel={},
        model_version="test-v1",
    )
    rec = ScorerCycleRecorder.from_recorder_context(ctx)
    rec.begin_cycle(
        high_adt_only=False,
        allowlist_size=0,
        last_etl=None,
        lookback_hours=6.0,
        limit_rows=100,
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "trainer_hightier.serving.flight_recorder.scorer_hooks.write_parquet_safe",
        _boom,
    )
    bets = pd.DataFrame({"bet_id": ["1"], "player_id": [1], "game_id": [1]})
    with pytest.raises(FlightRecorderFatalError, match="capture_batch"):
        rec.capture_batch(
            _ScoringBatchView(bets=bets, pool=bets, cursor=pd.Series([pd.Timestamp.utcnow()])),
            last_etl=None,
            lookback_hours=6.0,
            limit_rows=100,
            allowlist_ids=frozenset(),
            high_adt_only=False,
            pool_window_start=pd.Timestamp.utcnow().to_pydatetime(),
            pool_window_end=pd.Timestamp.utcnow().to_pydatetime(),
        )


def test_scorer_hook_fail_open_marks_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Debug fail-open runs may continue with recorder_partial=true."""
    ctx = RecorderContext.open(
        tmp_path / "bundle",
        FlightRecorderConfig(
            recording_root=str(tmp_path / "recording"),
            capture_scorer_stages=True,
            fail_fast=False,
        ),
        rel={},
        model_version="test-v1",
    )
    rec = ScorerCycleRecorder.from_recorder_context(ctx)
    rec.begin_cycle(
        high_adt_only=False,
        allowlist_size=0,
        last_etl=None,
        lookback_hours=6.0,
        limit_rows=100,
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "trainer_hightier.serving.flight_recorder.scorer_hooks.write_parquet_safe",
        _boom,
    )
    bets = pd.DataFrame({"bet_id": ["1"], "player_id": [1], "game_id": [1]})
    rec.capture_batch(
        _ScoringBatchView(bets=bets, pool=bets, cursor=pd.Series([pd.Timestamp.utcnow()])),
        last_etl=None,
        lookback_hours=6.0,
        limit_rows=100,
        allowlist_ids=frozenset(),
        high_adt_only=False,
        pool_window_start=pd.Timestamp.utcnow().to_pydatetime(),
        pool_window_end=pd.Timestamp.utcnow().to_pydatetime(),
    )
    assert rec.partial is True
    assert rec.failed_steps


def test_validate_recording_root_writable_ok(tmp_path: Path) -> None:
    """Writable roots pass the startup probe."""
    root = tmp_path / "recording"
    validate_recording_root_writable(root, fail_fast=True)
