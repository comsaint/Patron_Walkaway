"""Mirror legacy ``track_*`` YAML keys with layer+method aliases (Issue #16 naming).

Canonical layer+method section names (YAML)::

    bet_duckdb_window      ↔ legacy ``track_llm``
    run_state_machine      ↔ legacy ``track_human``
    player_profile_snapshot ↔ legacy ``track_profile``

``load_feature_spec`` calls :func:`mirror_layer_method_track_keys_inplace` **before**
static validation so specs may use either vocabulary without duplicate validation
errors.
"""

from __future__ import annotations

import copy
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

_TRACK_SECTION_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("track_llm", "bet_duckdb_window"),
    ("track_human", "run_state_machine"),
    ("track_profile", "player_profile_snapshot"),
)

_TRACKS_ENABLED_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("track_llm", "bet_duckdb_window"),
    ("track_human", "run_state_machine"),
    ("track_profile", "player_profile_snapshot"),
)


def _candidate_feature_ids(track: dict) -> List[str]:
    raw = track.get("candidates")
    cands = raw if isinstance(raw, list) else []
    out: List[str] = []
    for c in cands:
        if isinstance(c, dict) and c.get("feature_id"):
            out.append(str(c["feature_id"]))
    return out


def _tracks_conflict(legacy: dict, new: dict) -> bool:
    a = set(_candidate_feature_ids(legacy))
    b = set(_candidate_feature_ids(new))
    if not a or not b:
        return False
    return a != b


def mirror_layer_method_track_keys_inplace(spec: dict) -> None:
    """Populate missing legacy / layer-method keys from the other side (in-place).

    If both sides of a pair are present with non-empty candidate lists whose
    ``feature_id`` sets differ, raises ``ValueError`` (ambiguous spec).
    """
    if not isinstance(spec, dict):
        return

    for legacy_key, new_key in _TRACK_SECTION_PAIRS:
        leg = spec.get(legacy_key)
        neu = spec.get(new_key)
        leg_d = leg if isinstance(leg, dict) else {}
        neu_d = neu if isinstance(neu, dict) else {}
        has_leg = bool(leg_d.get("candidates"))
        has_neu = bool(neu_d.get("candidates"))

        if has_leg and has_neu:
            if _tracks_conflict(leg_d, neu_d):
                raise ValueError(
                    f"Feature spec: '{legacy_key}' and '{new_key}' both define candidates "
                    "with different feature_id sets; keep only one section or align ids."
                )
            continue
        if has_leg and not has_neu:
            spec[new_key] = copy.deepcopy(leg_d)
            logger.debug("Feature spec: mirrored %s -> %s", legacy_key, new_key)
        elif has_neu and not has_leg:
            spec[legacy_key] = copy.deepcopy(neu_d)
            logger.debug("Feature spec: mirrored %s -> %s", new_key, legacy_key)

    te = spec.get("tracks_enabled")
    if isinstance(te, dict):
        for legacy_key, new_key in _TRACKS_ENABLED_ALIASES:
            lv = te.get(legacy_key)
            nv = te.get(new_key)
            if lv is not None and nv is None:
                te[new_key] = lv
            elif nv is not None and lv is None:
                te[legacy_key] = nv
