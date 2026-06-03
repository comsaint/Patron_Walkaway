"""Time-machine window registry (shared recording root)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trainer_hightier.serving.flight_recorder.ch_capture import finalize_query_manifest


def _registry_path(recording_root: Path) -> Path:
    """Return path to ``ch_time_machine/windows.json``."""
    return recording_root / "ch_time_machine" / "windows.json"


def load_registry(recording_root: Path) -> dict[str, Any]:
    """Load window registry or return empty structure."""
    path = _registry_path(recording_root)
    if not path.is_file():
        return {"windows": [], "next_id": 1}
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(recording_root: Path, data: dict[str, Any]) -> None:
    """Persist window registry."""
    path = _registry_path(recording_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def register_window(
    recording_root: Path,
    *,
    source: str,
    fetch: str,
    query_meta: dict[str, Any],
    t0_final_parquet: str | None = None,
) -> str:
    """Register a new diagnostic window; return ``window_id``."""
    data = load_registry(recording_root)
    next_id = int(data.get("next_id", 1))
    window_id = f"window_{next_id:06d}"
    entry = {
        "window_id": window_id,
        "source": source,
        "fetch": fetch,
        "registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "query_meta": finalize_query_manifest({**query_meta, "fetch": fetch}),
        "t0_final_parquet": t0_final_parquet,
        "captures": {"t0": True} if t0_final_parquet else {},
    }
    windows = list(data.get("windows", []))
    windows.append(entry)
    data["windows"] = windows
    data["next_id"] = next_id + 1
    save_registry(recording_root, data)
    (recording_root / "ch_time_machine" / window_id).mkdir(parents=True, exist_ok=True)
    return window_id


def list_windows(recording_root: Path) -> list[dict[str, Any]]:
    """Return all registered windows."""
    return list(load_registry(recording_root).get("windows", []))


def mark_capture_done(
    recording_root: Path,
    window_id: str,
    capture_label: str,
) -> None:
    """Record that *capture_label* (e.g. ``t_plus_60m``) completed for *window_id*."""
    data = load_registry(recording_root)
    for win in data.get("windows", []):
        if win.get("window_id") == window_id:
            caps = win.setdefault("captures", {})
            caps[capture_label] = True
            break
    save_registry(recording_root, data)


def pending_capture_labels(
    window: dict[str, Any],
    schedule_minutes: tuple[int, ...],
) -> list[str]:
    """Return schedule labels not yet captured (excluding t0 if already present)."""
    captures = window.get("captures") or {}
    pending: list[str] = []
    for minutes in schedule_minutes:
        label = "t0" if minutes == 0 else f"t_plus_{minutes}m"
        if not captures.get(label):
            pending.append(label)
    return pending
