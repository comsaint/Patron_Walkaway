"""Tests for time-machine window registry."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from trainer_hightier.serving.flight_recorder.window_registry import (
    _save_registry_unlocked,
    list_windows,
    load_registry,
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


def test_load_registry_treats_empty_file_as_new(tmp_path: Path) -> None:
    """Empty windows.json must not crash concurrent readers."""
    root = tmp_path / "recording"
    path = root / "ch_time_machine" / "windows.json"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    data = load_registry(root)
    assert data == {"windows": [], "next_id": 1}


def test_concurrent_register_window_is_safe(tmp_path: Path) -> None:
    """Scorer and validator threads must not corrupt shared registry."""
    root = tmp_path / "recording"
    root.mkdir()
    meta = {"fetch": "fetch_bets_incremental", "final": True, "parameters": {}}
    errors: list[BaseException] = []

    def _register(source: str) -> None:
        try:
            register_window(
                root,
                source=source,
                fetch="fetch_bets_incremental",
                query_meta=meta,
                t0_final_parquet=f"{source}/incremental_t_bet.final.parquet",
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_register, args=(f"cycles/scorer/cycle_{i:06d}/clickhouse",))
        for i in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    windows = list_windows(root)
    assert len(windows) == 8
    window_ids = {win["window_id"] for win in windows}
    assert len(window_ids) == 8
    raw = json.loads((root / "ch_time_machine" / "windows.json").read_text(encoding="utf-8"))
    assert int(raw["next_id"]) == 9


def test_save_registry_falls_back_when_replace_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Windows-style replace lock should not fail the recorder."""
    root = tmp_path / "recording"
    root.mkdir()
    data = {"windows": [], "next_id": 1}

    def _always_blocked(_src: str | os.PathLike[str], _dest: str | os.PathLike[str]) -> None:
        raise PermissionError("[WinError 32] file in use")

    monkeypatch.setattr(
        "trainer_hightier.serving.flight_recorder.window_registry.os.replace",
        _always_blocked,
    )
    _save_registry_unlocked(root, data)
    loaded = load_registry(root)
    assert loaded == data
