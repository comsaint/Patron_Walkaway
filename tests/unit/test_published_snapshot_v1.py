"""Unit tests for ``published_snapshot_v1`` (LDA-E2-04 MVP)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipelines.layered_data_assets.io.published_snapshot_v1 import (
    publish_layered_snapshot_v1,
    read_current_pointer,
    validate_published_snapshot_id,
)


def test_validate_published_snapshot_id() -> None:
    assert validate_published_snapshot_id("pub_smoke_001") == "pub_smoke_001"
    with pytest.raises(ValueError):
        validate_published_snapshot_id("bad")
    with pytest.raises(ValueError):
        validate_published_snapshot_id("pub_/evil")


def test_publish_chain_infers_previous_from_current(tmp_path: Path) -> None:
    data_root = tmp_path
    snap = "snap_chainfixture1"
    p1, cur1 = publish_layered_snapshot_v1(
        data_root=data_root,
        published_snapshot_id="pub_chainfixture_a",
        source_snapshot_id=snap,
        inherit_previous_from_pointer=False,
    )
    assert p1.name == "published_snapshot.json"
    d1 = json.loads(p1.read_text(encoding="utf-8"))
    assert d1["previous_published_snapshot_id"] is None
    assert d1["l1_relative_root"] == f"l1_layered/{snap}"
    cur_doc = json.loads(cur1.read_text(encoding="utf-8"))
    assert cur_doc["active_published_snapshot_id"] == "pub_chainfixture_a"

    p2, _cur2 = publish_layered_snapshot_v1(
        data_root=data_root,
        published_snapshot_id="pub_chainfixture_b",
        source_snapshot_id=snap,
    )
    d2 = json.loads(p2.read_text(encoding="utf-8"))
    assert d2["previous_published_snapshot_id"] == "pub_chainfixture_a"

    ptr = read_current_pointer(data_root)
    assert ptr is not None
    assert ptr["active_published_snapshot_id"] == "pub_chainfixture_b"
    assert ptr["previous_active_published_snapshot_id"] == "pub_chainfixture_a"
