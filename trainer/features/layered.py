"""trainer/features/layered.py
================================
Phase B PR-B2: layered (bet/run/trip/player) feature builder entrypoints.

Purpose
-------
Provide a stable, layer-aware public surface for trainer/scorer to call into,
without rewriting any feature math. Each layered builder is a thin wrapper
around the existing legacy implementation in :mod:`trainer.features.features`
(or — for run-level state — :mod:`trainer.training.trainer`):

    Layer    | Source of truth                                  | Phase B status
    ---------|--------------------------------------------------|----------------
    bet      | features.compute_track_llm_features              | wrapped here
    run      | trainer.add_track_human_features (state machine) | declared here,
             |                                                  | computation
             |                                                  | stays in trainer
    trip     | L1 passthrough joins (no compute_* yet)          | declared here,
             |                                                  | computation
             |                                                  | stays in trainer
    player   | features.join_player_profile                     | wrapped here

Rationale
---------
- Phase B reorganizes *entry points*, not math. Trainer/scorer call layered
  builders so the dependency graph is layer-shaped, but the underlying
  computations are unchanged. This keeps train-serve parity intact while we
  prepare for Phase C (PIT skip unification) and later phases.
- Run-level state-machine features and trip-level passthroughs currently live
  inside ``trainer.training.trainer`` (``add_track_human_features``) and
  pipeline joins. Lifting them into ``features`` would be a behavior-changing
  refactor; we keep them in place and just expose layer metadata here.

Backward compatibility
----------------------
- All existing callers of the legacy functions (``compute_track_llm_features``,
  ``join_player_profile``) continue to work.
- The wrappers return DataFrames *byte-identical* to the underlying function
  (no column rename, no extra metadata column). Phase B does NOT rename
  DataFrame columns from legacy to layered ids — that is deferred until the
  YAML spec runtime is layered (post Phase B).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

import pandas as pd

from trainer.features.features import (
    LAYER_NAMES,
    compute_track_llm_features as _compute_track_llm_features,
    get_chunk_replacements,
    get_feature_ids_by_layer,
    get_layer_for_feature,
    get_layered_to_legacy_map,
    get_legacy_to_layered_map,
    join_player_profile as _join_player_profile,
    load_track_to_layer_mapping,
)

logger = logging.getLogger(__name__)


LAYER_BET = "bet"
LAYER_RUN = "run"
LAYER_TRIP = "trip"
LAYER_PLAYER = "player"


__all__ = [
    "LAYER_BET",
    "LAYER_RUN",
    "LAYER_TRIP",
    "LAYER_PLAYER",
    "LAYER_NAMES",
    "compute_bet_layer_features",
    "compute_player_layer_features",
    "describe_layered_entrypoints",
    "get_chunk_replacements",
    "get_feature_ids_by_layer",
    "get_layer_for_feature",
    "get_layered_to_legacy_map",
    "get_legacy_to_layered_map",
    "load_track_to_layer_mapping",
]


# ---------------------------------------------------------------------------
# Bet layer (legacy: track_llm)
# ---------------------------------------------------------------------------

def compute_bet_layer_features(
    bets_df: pd.DataFrame,
    feature_spec: dict,
    cutoff_time: Optional[datetime] = None,
) -> pd.DataFrame:
    """Compute bet-layer features for *bets_df*.

    Phase B implementation: thin wrapper around
    :func:`trainer.features.features.compute_track_llm_features`. The output
    columns continue to use legacy ``track_llm`` ids; the layered
    ``bet__<semantic>__<scope>`` ids are exposed via the mapping helpers
    (see :func:`get_layered_to_legacy_map`) until the YAML spec runtime is
    layered.

    Parameters
    ----------
    bets_df:
        Bet-level DataFrame with at minimum ``canonical_id``,
        ``payout_complete_dtm``, ``bet_id``.
    feature_spec:
        Parsed feature spec dict (from :func:`features.load_feature_spec`).
    cutoff_time:
        Optional row-level leakage guard; rows with
        ``payout_complete_dtm > cutoff_time`` are dropped before computation.
    """
    return _compute_track_llm_features(
        bets_df,
        feature_spec=feature_spec,
        cutoff_time=cutoff_time,
    )


# ---------------------------------------------------------------------------
# Player layer (legacy: track_profile)
# ---------------------------------------------------------------------------

def compute_player_layer_features(
    bets_df: pd.DataFrame,
    profile_df: Optional[pd.DataFrame],
    feature_cols: Optional[List[str]] = None,
) -> pd.DataFrame:
    """Attach player-layer features (player_profile snapshots, PIT-safe).

    Phase B implementation: thin wrapper around
    :func:`trainer.features.features.join_player_profile`. Non-rated bets and
    bets without a prior snapshot keep the legacy behavior (zero-fill / NaN
    per the underlying function) — Phase B does not change identity admission
    rules; that lands in Phase C.
    """
    return _join_player_profile(
        bets_df,
        profile_df,
        feature_cols=feature_cols,
    )


# ---------------------------------------------------------------------------
# Run / trip layers — Phase B declares ownership only
# ---------------------------------------------------------------------------

def describe_layered_entrypoints() -> dict:
    """Return a static description of where each layer is currently computed.

    This is the authoritative Phase B answer to "which function owns layer X".
    Trainer/scorer/audit tooling can rely on this to log/verify the entrypoint
    map without depending on import-time discovery.

    Note: ``run`` and ``trip`` entries are intentionally callable-less in
    Phase B because their math has not yet been lifted out of
    ``trainer.training.trainer`` / pipeline passthrough joins.
    """
    return {
        LAYER_BET: {
            "module": "trainer.features.features",
            "function": "compute_track_llm_features",
            "wrapper": "trainer.features.layered.compute_bet_layer_features",
            "phase_b_status": "wrapped",
        },
        LAYER_RUN: {
            "module": "trainer.training.trainer",
            "function": "add_track_human_features",
            "wrapper": None,
            "phase_b_status": "in_place",
            "notes": (
                "Run-level state-machine features remain inside trainer.py "
                "and are mirrored into scorer.build_features_for_scoring. "
                "Lifted out in a later phase."
            ),
        },
        LAYER_TRIP: {
            "module": "pipelines.layered_data_assets",
            "function": "L1 passthrough joins",
            "wrapper": None,
            "phase_b_status": "in_place",
            "notes": (
                "Trip-level features come from passthrough joins on L1 trip_fact. "
                "No compute_* exists yet; deferred to a later phase."
            ),
        },
        LAYER_PLAYER: {
            "module": "trainer.features.features",
            "function": "join_player_profile",
            "wrapper": "trainer.features.layered.compute_player_layer_features",
            "phase_b_status": "wrapped",
        },
    }
