"""Time-machine window registry (shared recording root)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trainer_hightier.serving.flight_recorder.ch_capture import finalize_query_manifest

logger = logging.getLogger(__name__)

_REGISTRY_LOCK = threading.Lock()
_REPLACE_ATTEMPTS = 5
_REPLACE_INITIAL_DELAY_S = 0.02

def _registry_path(recording_root: Path) -> Path:
    """Return path to ``ch_time_machine/windows.json``."""
    return recording_root / "ch_time_machine" / "windows.json"


def _empty_registry() -> dict[str, Any]:
    """Return a fresh empty registry structure."""
    return {"windows": [], "next_id": 1}


def _load_registry_unlocked(recording_root: Path) -> dict[str, Any]:
    """Load window registry without acquiring the module lock."""
    path = _registry_path(recording_root)
    if not path.is_file():
        return _empty_registry()
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        logger.warning("window registry empty at %s; treating as new", path)
        return _empty_registry()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("window registry corrupt at %s: %s; treating as new", path, exc)
        return _empty_registry()


def load_registry(recording_root: Path) -> dict[str, Any]:
    """Load window registry or return empty structure."""
    with _REGISTRY_LOCK:
        return _load_registry_unlocked(recording_root)


def _replace_with_retry(src: Path, dest: Path) -> None:
    """Atomically replace *dest* with *src*, retrying transient Windows locks."""
    delay = _REPLACE_INITIAL_DELAY_S
    last_exc: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dest)
            return
        except OSError as exc:
            last_exc = exc
            if attempt + 1 >= _REPLACE_ATTEMPTS:
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    payload = src.read_text(encoding="utf-8")
    logger.warning(
        "window registry replace failed after %d attempts (%s); falling back to direct write path=%s",
        _REPLACE_ATTEMPTS,
        last_exc,
        dest,
    )
    dest.write_text(payload, encoding="utf-8")


def _save_registry_unlocked(recording_root: Path, data: dict[str, Any]) -> None:
    """Persist window registry atomically without acquiring the module lock."""
    path = _registry_path(recording_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, default=str)
    tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        _replace_with_retry(tmp, path)
    finally:
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass


def save_registry(recording_root: Path, data: dict[str, Any]) -> None:
    """Persist window registry atomically (scorer + validator share one file)."""
    with _REGISTRY_LOCK:
        _save_registry_unlocked(recording_root, data)

def register_window(
    recording_root: Path,
    *,
    source: str,
    fetch: str,
    query_meta: dict[str, Any],
    t0_final_parquet: str | None = None,
) -> str:
    """Register a new diagnostic window; return ``window_id``."""
    with _REGISTRY_LOCK:
        data = _load_registry_unlocked(recording_root)
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
        _save_registry_unlocked(recording_root, data)
        (recording_root / "ch_time_machine" / window_id).mkdir(parents=True, exist_ok=True)
    return window_id


def list_windows(recording_root: Path) -> list[dict[str, Any]]:
    """Return all registered windows."""
    with _REGISTRY_LOCK:
        return list(_load_registry_unlocked(recording_root).get("windows", []))

def mark_capture_done(
    recording_root: Path,
    window_id: str,
    capture_label: str,
) -> None:
    """Record that *capture_label* (e.g. ``t_plus_60m``) completed for *window_id*."""
    with _REGISTRY_LOCK:
        data = _load_registry_unlocked(recording_root)
        for win in data.get("windows", []):
            if win.get("window_id") == window_id:
                caps = win.setdefault("captures", {})
                caps[capture_label] = True
                break
        _save_registry_unlocked(recording_root, data)

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
