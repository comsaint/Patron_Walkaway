"""Tests for time-machine window registry."""

from __future__ import annotations

from pathlib import Path

from trainer_hightier.serving.flight_recorder.window_registry import (
    list_windows,
    mark_capture_done,
    pending_capture_labels,
    register_window,
)


def test_register_and_pending_labels(tmp_path: Path) -> None:
    """Register window and track pending capture labels."""
    root = tmp_path / "recording"
    root.mkdir()
    wid = register_window(
        root,
        source="cycles/scorer/cycle_000001/clickhouse",
        fetch="fetch_bets_incremental",
        query_meta={"fetch": "fetch_bets_incremental", "final": True, "parameters": {}},
        t0_final_parquet="cycles/scorer/cycle_000001/clickhouse/incremental_t_bet.final.parquet",
    )
    windows = list_windows(root)
    assert len(windows) == 1
    assert windows[0]["window_id"] == wid
    pending = pending_capture_labels(windows[0], (0, 15, 60))
    assert "t0" not in pending
    assert "t_plus_15m" in pending
    mark_capture_done(root, wid, "t_plus_15m")
    pending2 = pending_capture_labels(list_windows(root)[0], (0, 15, 60))
    assert "t_plus_15m" not in pending2
