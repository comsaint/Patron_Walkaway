"""Phase B: layered mapping helpers + train-serve entrypoint parity (smoke)."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CANDIDATES = _REPO / "trainer" / "feature_spec" / "feature_spec.yaml"
_MAPPING = _REPO / "trainer" / "feature_spec" / "track_to_layer_mapping.yaml"


def _candidate_feature_ids_from_spec(spec: dict) -> set[str]:
    out: set[str] = set()
    for track in ("track_llm", "track_human", "track_profile"):
        raw = ((spec.get(track) or {}).get("candidates"))
        cands = raw if isinstance(raw, list) else []
        for c in cands:
            if isinstance(c, dict) and c.get("feature_id"):
                out.add(c["feature_id"])
    return out


def test_mapping_covers_all_legacy_candidates() -> None:
    spec = yaml.safe_load(_CANDIDATES.read_text(encoding="utf-8"))
    mapping = yaml.safe_load(_MAPPING.read_text(encoding="utf-8"))
    legacy_yaml = mapping.get("legacy_features") or []
    entries = [e for e in legacy_yaml if isinstance(e, dict) and e.get("old_feature_id")]
    mapped_old = {e["old_feature_id"] for e in entries}
    expected = _candidate_feature_ids_from_spec(spec)
    assert mapped_old == expected, (
            f"mapping legacy_features must match candidates exactly; "
            f"missing={sorted(expected - mapped_old)}, extra={sorted(mapped_old - expected)}"
        )


def test_mapping_old_ids_unique_and_new_id_naming_rules() -> None:
    mapping = yaml.safe_load(_MAPPING.read_text(encoding="utf-8"))
    entries = [
        e for e in (mapping.get("legacy_features") or [])
        if isinstance(e, dict) and e.get("old_feature_id")
    ]
    old_ids = [e["old_feature_id"] for e in entries]
    assert len(old_ids) == len(set(old_ids)), "duplicate old_feature_id in mapping"
    # Multiple legacy rows may intentionally map to the same new_feature_id
    # (e.g. track_human vs track_llm naming convergence).
    for e in entries:
        n = e.get("new_feature_id") or ""
        if not n:
            continue
        assert n == n.lower(), f"new_feature_id must be lowercase: {n!r}"
        assert "-" not in n, f"hyphen disallowed in new_feature_id: {n!r}"
        parts = n.split("__")
        assert 2 <= len(parts) <= 3, f"expected 2-3 __ segments, got {n!r}"
        assert parts[0] in {"bet", "run", "trip", "player"}, f"bad layer prefix: {n!r}"
        bad = ("sofar", "so_far")
        for b in bad:
            assert b not in n, f"redundant {b} in feature_id: {n!r}"


def test_chunk_replacements_subset_of_deprecated() -> None:
    mapping = yaml.safe_load(_MAPPING.read_text(encoding="utf-8"))
    legacy = [
        e for e in (mapping.get("legacy_features") or [])
        if isinstance(e, dict) and e.get("old_feature_id")
    ]
    deprecated_old = {
        e["old_feature_id"] for e in legacy
        if (e.get("status") or "").lower() == "deprecated"
    }
    chunk = [
        e for e in (mapping.get("chunk_dependent_replacements") or [])
        if isinstance(e, dict) and e.get("old_feature_id")
    ]
    for e in chunk:
        oid = e["old_feature_id"]
        assert oid in deprecated_old, (
            f"chunk_dependent_replacements entry {oid!r} must be deprecated in legacy_features"
        )
        rep = e.get("replacement_feature_id") or ""
        assert rep and "__" in rep, f"invalid replacement_feature_id for {oid!r}"


@pytest.mark.parametrize("mod_name", ["trainer.training.trainer", "trainer.serving.scorer"])
def test_phase_b_entrypoints_importable(mod_name: str) -> None:
    os.environ.pop("MODEL_DIR", None)
    __import__(mod_name)


def test_bet_player_wrappers_match_legacy_byte_identical() -> None:
    """Gate-B3 smoke: layered wrappers == underlying legacy (no DuckDB path).

    Uses empty ``track_llm.candidates`` and ``feature_cols=[]`` so the legacy
    functions return early without requiring the full bet schema / wager alias.
    """
    os.environ.pop("MODEL_DIR", None)
    from trainer.features import features as F
    from trainer.features import layered as L

    spec: dict = {"track_llm": {"candidates": []}}
    now = pd.Timestamp("2024-01-15 12:00:00")
    bets = pd.DataFrame(
        {
            "canonical_id": [1, 1],
            "bet_id": [101, 102],
            "payout_complete_dtm": [now - pd.Timedelta(minutes=10), now - pd.Timedelta(minutes=5)],
        }
    )
    out_legacy = F.compute_track_llm_features(bets, feature_spec=spec, cutoff_time=now)
    out_layered = L.compute_bet_layer_features(bets, feature_spec=spec, cutoff_time=now)
    pd.testing.assert_frame_equal(out_legacy, out_layered)

    base = bets.copy()
    prof_legacy = F.join_player_profile(base, None, feature_cols=[])
    prof_layered = L.compute_player_layer_features(base, None, feature_cols=[])
    pd.testing.assert_frame_equal(prof_legacy, prof_layered)


def test_phase_b_gate_entrypoint_map() -> None:
    """Gate-B1 static check: describe_layered documents trainer-owned run/trip."""
    os.environ.pop("MODEL_DIR", None)
    from trainer.features import layered as L

    desc = L.describe_layered_entrypoints()
    assert set(desc) == {"bet", "run", "trip", "player"}
    assert desc["bet"]["phase_b_status"] == "wrapped"
    assert desc["player"]["phase_b_status"] == "wrapped"
    assert desc["run"]["phase_b_status"] == "in_place"
    assert desc["trip"]["phase_b_status"] == "in_place"
    assert "trainer.training.trainer" in desc["run"]["module"]
