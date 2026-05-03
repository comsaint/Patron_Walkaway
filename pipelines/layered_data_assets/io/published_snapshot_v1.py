"""L1 published snapshot pointer (LDA-E2-04 MVP): ``published_snapshot.json`` + ``current.json``."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipelines.layered_data_assets.io.l0_paths import validate_source_snapshot_id

_SCHEMA_PUBLISHED = "published_snapshot_v1"
_SCHEMA_POINTER = "published_current_pointer_v1"


def validate_published_snapshot_id(value: str) -> str:
    """Return stripped ``pub_*`` id safe for path segments."""
    s = value.strip()
    if len(s) < 8 or not s.startswith("pub_"):
        raise ValueError(f"published_snapshot_id must start with 'pub_' and length >= 8, got {value!r}")
    if not re.fullmatch(r"pub_[A-Za-z0-9_\-]+", s):
        raise ValueError(f"published_snapshot_id has invalid characters: {value!r}")
    if ".." in s or "/" in s or "\\" in s:
        raise ValueError(f"published_snapshot_id must not contain path segments: {value!r}")
    return s


def published_snapshots_root(data_root: Path) -> Path:
    """``<data_root>/l1_layered/published/snapshots``."""
    return data_root.resolve() / "l1_layered" / "published" / "snapshots"


def published_current_pointer_path(data_root: Path) -> Path:
    """``<data_root>/l1_layered/published/current.json``."""
    return data_root.resolve() / "l1_layered" / "published" / "current.json"


def published_snapshot_file(data_root: Path, published_snapshot_id: str) -> Path:
    """Path to one snapshot's ``published_snapshot.json``."""
    pid = validate_published_snapshot_id(published_snapshot_id)
    return published_snapshots_root(data_root) / pid / "published_snapshot.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_current_pointer(data_root: Path) -> dict[str, Any] | None:
    """Load ``current.json`` if present; else ``None``."""
    p = published_current_pointer_path(data_root)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def build_published_snapshot_dict(
    *,
    published_snapshot_id: str,
    source_snapshot_id: str,
    previous_published_snapshot_id: str | None,
    l1_relative_root: str,
    created_at: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Build ``published_snapshot_v1`` document."""
    validate_published_snapshot_id(published_snapshot_id)
    validate_source_snapshot_id(source_snapshot_id)
    sid = source_snapshot_id.strip()
    prev = None if previous_published_snapshot_id is None else validate_published_snapshot_id(
        previous_published_snapshot_id
    )
    ts = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, Any] = {
        "schema_version": _SCHEMA_PUBLISHED,
        "published_snapshot_id": validate_published_snapshot_id(published_snapshot_id),
        "source_snapshot_id": sid,
        "previous_published_snapshot_id": prev,
        "created_at": ts,
        "l1_relative_root": str(l1_relative_root).strip().replace("\\", "/"),
    }
    if notes is not None and str(notes).strip():
        out["notes"] = str(notes).strip()
    return out


def build_current_pointer_dict(
    *,
    data_root: Path,
    published_snapshot_id: str,
    previous_active_published_snapshot_id: str | None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Build ``published_current_pointer_v1`` for ``current.json``."""
    pid = validate_published_snapshot_id(published_snapshot_id)
    snap_file = published_snapshot_file(data_root, pid)
    dr = data_root.resolve()
    rel = snap_file.resolve().relative_to(dr).as_posix()
    ts = updated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prev = (
        None
        if previous_active_published_snapshot_id is None
        else validate_published_snapshot_id(previous_active_published_snapshot_id)
    )
    return {
        "schema_version": _SCHEMA_POINTER,
        "active_published_snapshot_id": pid,
        "active_published_snapshot_relpath": rel,
        "previous_active_published_snapshot_id": prev,
        "updated_at": ts,
    }


def publish_layered_snapshot_v1(
    *,
    data_root: Path,
    published_snapshot_id: str,
    source_snapshot_id: str,
    previous_published_snapshot_id: str | None = None,
    inherit_previous_from_pointer: bool = True,
    notes: str | None = None,
) -> tuple[Path, Path]:
    """Write snapshot JSON and refresh ``current.json``. Returns ``(snapshot_path, current_path)``."""
    validate_source_snapshot_id(source_snapshot_id)
    pid = validate_published_snapshot_id(published_snapshot_id)
    l1_rel = f"l1_layered/{source_snapshot_id.strip()}"
    prior_pub = previous_published_snapshot_id
    if prior_pub is None and inherit_previous_from_pointer:
        cur = read_current_pointer(data_root)
        if isinstance(cur, dict):
            aid = cur.get("active_published_snapshot_id")
            if isinstance(aid, str) and aid.strip():
                prior_pub = aid.strip()
    snap_doc = build_published_snapshot_dict(
        published_snapshot_id=pid,
        source_snapshot_id=source_snapshot_id,
        previous_published_snapshot_id=prior_pub,
        l1_relative_root=l1_rel,
        notes=notes,
    )
    snap_path = published_snapshot_file(data_root, pid)
    _write_json_atomic(snap_path, snap_doc)
    ptr_doc = build_current_pointer_dict(
        data_root=data_root,
        published_snapshot_id=pid,
        previous_active_published_snapshot_id=prior_pub,
    )
    cur_path = published_current_pointer_path(data_root)
    _write_json_atomic(cur_path, ptr_doc)
    return snap_path, cur_path
