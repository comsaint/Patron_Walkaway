"""Phase B: per-candidate target_layer + chunk replacements (single spec SSOT)."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CANDIDATES = _REPO / "trainer" / "feature_spec" / "feature_candidates.yaml"

LAYER_NAMES = frozenset({"bet", "run", "trip", "player"})


def _candidate_rows(spec: dict) -> list[dict]:
    rows: list[dict] = []
    for track in ("bet_duckdb_window", "run_state_machine", "player_run_asset", "trip_asset_materialized"):
        for c in ((spec.get(track) or {}).get("candidates") or []):
            if isinstance(c, dict) and c.get("feature_id"):
                rows.append({"track": track, **c})
    return rows


def test_every_candidate_has_valid_target_layer() -> None:
    spec = yaml.safe_load(_CANDIDATES.read_text(encoding="utf-8"))
    missing: list[str] = []
    bad: list[str] = []
    for row in _candidate_rows(spec):
        fid = row["feature_id"]
        tl = row.get("target_layer")
        if tl is None or tl == "":
            missing.append(f"{row['track']}:{fid}")
        elif str(tl) not in LAYER_NAMES:
            bad.append(f"{row['track']}:{fid}={tl!r}")
    assert not missing, f"missing target_layer: {missing[:20]}"
    assert not bad, f"invalid target_layer: {bad[:20]}"


def test_feature_ids_unique_across_tracks() -> None:
    spec = yaml.safe_load(_CANDIDATES.read_text(encoding="utf-8"))
    seen: set[str] = set()
    dups: list[str] = []
    for row in _candidate_rows(spec):
        fid = str(row["feature_id"])
        if fid in seen:
            dups.append(fid)
        seen.add(fid)
    assert not dups, f"duplicate feature_id: {dups}"


def test_chunk_replacements_well_formed() -> None:
    spec = yaml.safe_load(_CANDIDATES.read_text(encoding="utf-8"))
    lf = spec.get("layered_framework") or {}
    chunk = lf.get("chunk_dependent_replacements") or []
    assert isinstance(chunk, list) and chunk, "chunk_dependent_replacements must be non-empty list"
    for e in chunk:
        assert isinstance(e, dict), chunk
        oid = e.get("old_feature_id")
        rep = e.get("replacement_feature_id") or ""
        assert oid, f"chunk entry missing old_feature_id: {e!r}"
        assert rep, f"missing replacement_feature_id for {oid!r}"
        rc = (e.get("risk_class") or "").upper()
        assert rc in {"A", "B", "D"}, f"bad risk_class for {oid!r}: {rc!r}"


def test_load_track_to_layer_mapping_matches_spec_shape() -> None:
    from trainer.features import features as F

    F._TRACK_TO_LAYER_MAPPING_CACHE = None  # type: ignore[attr-defined]
    m = F.load_track_to_layer_mapping()
    legacy = m.get("legacy_features") or []
    assert len(legacy) >= 10
    assert all(isinstance(x, dict) and x.get("old_feature_id") for x in legacy)
    chunk = m.get("chunk_dependent_replacements") or []
    assert len(chunk) >= 1


@pytest.mark.parametrize("mod_name", ["trainer.training.trainer", "trainer.serving.scorer"])
def test_phase_b_entrypoints_importable(mod_name: str) -> None:
    os.environ.pop("MODEL_DIR", None)
    __import__(mod_name)


def test_bet_player_wrappers_match_legacy_byte_identical() -> None:
    """Gate-B3 smoke: layered wrappers == underlying canonical functions."""
    os.environ.pop("MODEL_DIR", None)
    from trainer.features import features as F
    from trainer.features import layered as L

    spec: dict = {"bet_duckdb_window": {"candidates": []}}
    now = pd.Timestamp("2024-01-15 12:00:00")
    bets = pd.DataFrame(
        {
            "canonical_id": [1, 1],
            "bet_id": [101, 102],
            "payout_complete_dtm": [now - pd.Timedelta(minutes=10), now - pd.Timedelta(minutes=5)],
        }
    )
    out_legacy = F.compute_bet_duckdb_window_features(bets, feature_spec=spec, cutoff_time=now)
    out_layered = L.compute_bet_layer_features(bets, feature_spec=spec, cutoff_time=now)
    pd.testing.assert_frame_equal(out_legacy, out_layered)

    base = bets.copy()
    prof_legacy = F.join_player_profile(base, None, feature_cols=[])
    prof_layered = L.compute_player_layer_features(base, None, feature_cols=[])
    pd.testing.assert_frame_equal(prof_legacy, prof_layered)


def test_phase_b_gate_entrypoint_map() -> None:
    """Gate-B1 static check: describe_layered documents run/trip entrypoints."""
    os.environ.pop("MODEL_DIR", None)
    from trainer.features import layered as L

    desc = L.describe_layered_entrypoints()
    assert set(desc) == {"bet", "run", "trip", "player"}
    assert desc["bet"]["phase_b_status"] == "wrapped"
    assert desc["player"]["phase_b_status"] == "wrapped"
    assert desc["run"]["phase_b_status"] == "wrapped"
    assert desc["trip"]["phase_b_status"] == "wrapped"
    assert "trainer.features.trip_materializer" in desc["trip"]["module"]
    assert "trainer.training.feature_pipeline" in desc["run"]["module"]
