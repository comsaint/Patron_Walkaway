"""Unit tests for ``run_id_v1`` deterministic hashing."""

import hashlib
import json
from datetime import datetime

from layered_data_assets.run_id_v1 import derive_run_id, run_start_ts_canonical


def test_run_start_ts_canonical_microseconds() -> None:
    dt = datetime(2026, 1, 15, 10, 0, 0)
    assert run_start_ts_canonical(dt) == "2026-01-15T10:00:00.000000"


def test_derive_run_id_stable_vector() -> None:
    rid = derive_run_id(
        player_id=100,
        run_start_ts=datetime(2026, 1, 15, 10, 0, 0),
        first_bet_id=1,
        run_definition_version="run_boundary_v1",
        source_namespace="layered_data_assets_l1",
    )
    payload = {
        "first_bet_id": "1",
        "player_id": 100,
        "run_definition_version": "run_boundary_v1",
        "run_start_ts": "2026-01-15T10:00:00.000000",
        "source_namespace": "layered_data_assets_l1",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    expect = "run_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    assert rid == expect


def test_derive_run_id_differs_when_first_bet_changes() -> None:
    base = dict(
        player_id=100,
        run_start_ts=datetime(2026, 1, 15, 10, 0, 0),
        run_definition_version="run_boundary_v1",
        source_namespace="layered_data_assets_l1",
    )
    a = derive_run_id(first_bet_id=1, **base)
    b = derive_run_id(first_bet_id=2, **base)
    assert a != b
