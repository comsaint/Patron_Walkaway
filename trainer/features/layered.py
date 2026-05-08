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
    trip     | trip_materializer on ``bets_df`` (LDA passthrough) | wrapped here
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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

import numpy as np
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
from trainer.features.trip_materializer import materialize_trip_layer_features as _materialize_trip_layer_features

logger = logging.getLogger(__name__)


LAYER_BET = "bet"
LAYER_RUN = "run"
LAYER_TRIP = "trip"
LAYER_PLAYER = "player"


# ---------------------------------------------------------------------------
# Phase C PR-C1: unified prediction_skip admission contract
# ---------------------------------------------------------------------------
# Mirror of `feature_spec.layered_framework.admission_rule.skip_reason_codes`.
# ``skip_reason_code`` (singular) is the admission contract; do NOT confuse with
# ``reason_codes`` (plural, JSON list) emitted by SHAP — they are different
# concerns and live in different output columns.

SKIP_REASON_PIT_UNAVAILABLE_SOURCE = "PIT_UNAVAILABLE_SOURCE"
SKIP_REASON_IDENTITY_UNMATCHED = "IDENTITY_UNMATCHED"
SKIP_REASON_MISSING_REQUIRED_INPUT = "MISSING_REQUIRED_INPUT"

VALID_SKIP_REASON_CODES: frozenset = frozenset(
    {
        SKIP_REASON_PIT_UNAVAILABLE_SOURCE,
        SKIP_REASON_IDENTITY_UNMATCHED,
        SKIP_REASON_MISSING_REQUIRED_INPUT,
    }
)


__all__ = [
    "LAYER_BET",
    "LAYER_RUN",
    "LAYER_TRIP",
    "LAYER_PLAYER",
    "LAYER_NAMES",
    "SKIP_REASON_PIT_UNAVAILABLE_SOURCE",
    "SKIP_REASON_IDENTITY_UNMATCHED",
    "SKIP_REASON_MISSING_REQUIRED_INPUT",
    "VALID_SKIP_REASON_CODES",
    "AdmissionResult",
    "compute_bet_layer_features",
    "compute_bet_duckdb_window_features",
    "compute_player_layer_features",
    "add_run_state_machine_features",
    "describe_layered_entrypoints",
    "evaluate_pit_admission",
    "get_admission_rule_from_spec",
    "get_chunk_replacements",
    "get_feature_ids_by_layer",
    "get_layer_for_feature",
    "get_layered_to_legacy_map",
    "get_legacy_to_layered_map",
    "load_track_to_layer_mapping",
    "validate_admission_rule_against_spec",
    "compute_trip_layer_features",
]


@dataclass
class AdmissionResult:
    """Outcome of one prediction_skip admission evaluation pass.

    Attributes
    ----------
    admitted:
        Subset of the input DataFrame that passed admission. Order preserved.
    skip_counts:
        ``{skip_reason_code: count}``. Reason codes are restricted to
        :data:`VALID_SKIP_REASON_CODES`. Reasons with zero rows are omitted.
    total_input_rows:
        Total number of input rows seen.
    """

    admitted: pd.DataFrame
    skip_counts: dict = field(default_factory=dict)
    total_input_rows: int = 0

    @property
    def admitted_rows(self) -> int:
        return int(len(self.admitted))

    @property
    def skipped_rows(self) -> int:
        return int(self.total_input_rows - self.admitted_rows)

    def to_log_dict(self) -> dict:
        """Serializable summary for log lines / metrics payloads."""
        return {
            "total_input_rows": int(self.total_input_rows),
            "admitted_rows": self.admitted_rows,
            "skipped_rows": self.skipped_rows,
            "skip_counts": {str(k): int(v) for k, v in self.skip_counts.items()},
        }


def get_admission_rule_from_spec(spec: Optional[dict]) -> dict:
    """Read ``layered_framework.admission_rule`` from a feature spec.

    Returns an empty dict when the section is absent. Phase A wrote this block
    into both candidates and deploy specs; Phase C makes it authoritative.
    """
    if not isinstance(spec, dict):
        return {}
    lf = spec.get("layered_framework") or {}
    ar = lf.get("admission_rule") or {}
    return ar if isinstance(ar, dict) else {}


def validate_admission_rule_against_spec(spec: Optional[dict]) -> List[str]:
    """Return a list of unknown skip_reason_codes in the spec.

    Empty list means the spec's ``skip_reason_codes`` are a subset of
    :data:`VALID_SKIP_REASON_CODES`. Used by tests / startup checks.
    """
    rule = get_admission_rule_from_spec(spec)
    raw = rule.get("skip_reason_codes") or []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(c) for c in raw if str(c) not in VALID_SKIP_REASON_CODES]


def _required_input_mask(
    df: pd.DataFrame,
    required_cols: Iterable[str],
) -> np.ndarray:
    """Return a boolean array (len == len(df)) marking rows missing any required input.

    A row is "missing required input" if any required column is absent from
    the frame, or its value is null in that row. Empty ``required_cols`` ->
    all-False mask (nothing to skip on).
    """
    n = len(df)
    miss = np.zeros(n, dtype=bool)
    cols = list(required_cols or [])
    if not cols:
        return miss
    for c in cols:
        if c not in df.columns:
            miss[:] = True
            return miss
        miss = miss | df[c].isna().to_numpy()
    return miss


def evaluate_pit_admission(
    bets_df: pd.DataFrame,
    *,
    pit_rated_col: str = "_pit_rated",
    canonical_id_col: str = "canonical_id",
    rated_canonical_ids: Optional[Iterable[str]] = None,
    required_input_cols: Iterable[str] = (),
    drop_pit_marker: bool = True,
) -> AdmissionResult:
    """Apply the unified prediction_skip rule to a bet-level DataFrame.

    Order of evaluation per row (first match wins):

    1. ``MISSING_REQUIRED_INPUT`` — any of ``required_input_cols`` missing
       from the frame, or null in that row.
    2. ``PIT_UNAVAILABLE_SOURCE`` — ``pit_rated_col`` is present in the frame
       and its value is False / null. Reflects a PIT identity asof miss.
    3. ``IDENTITY_UNMATCHED`` — ``rated_canonical_ids`` is provided and the
       row's ``canonical_id`` is not in that set. Used by the cutoff-window
       fallback path where there is no ``_pit_rated`` column.

    Rows that fall through all three checks are admitted.

    Notes
    -----
    - This helper does not mutate the input frame.
    - The returned ``admitted`` frame preserves original row order. When
      ``drop_pit_marker`` is True (default), the ``_pit_rated`` column is
      dropped from the admitted frame for cleanliness.
    - ``rated_canonical_ids`` may be any iterable (list / set / Series).
    """
    n = int(len(bets_df))
    if n == 0:
        return AdmissionResult(
            admitted=bets_df.copy(),
            skip_counts={},
            total_input_rows=0,
        )

    skip_reason = np.full(n, "", dtype=object)
    miss = _required_input_mask(bets_df, required_input_cols)
    if miss.any():
        skip_reason[miss] = SKIP_REASON_MISSING_REQUIRED_INPUT

    if pit_rated_col in bets_df.columns:
        not_set = skip_reason == ""
        pit_vals = bets_df[pit_rated_col].to_numpy()
        bad_pit = np.array(
            [
                (v is None) or (isinstance(v, float) and np.isnan(v)) or (not bool(v))
                for v in pit_vals
            ],
            dtype=bool,
        )
        skip_reason[not_set & bad_pit] = SKIP_REASON_PIT_UNAVAILABLE_SOURCE

    if rated_canonical_ids is not None and canonical_id_col in bets_df.columns:
        rated_set: set = (
            rated_canonical_ids
            if isinstance(rated_canonical_ids, set)
            else set(map(str, rated_canonical_ids))
        )
        not_set = skip_reason == ""
        cid_str = bets_df[canonical_id_col].astype(str).to_numpy()
        unmatched = np.array(
            [c not in rated_set for c in cid_str],
            dtype=bool,
        )
        skip_reason[not_set & unmatched] = SKIP_REASON_IDENTITY_UNMATCHED

    admit_mask = skip_reason == ""
    admitted = bets_df.loc[admit_mask].copy()
    if drop_pit_marker and pit_rated_col in admitted.columns:
        admitted = admitted.drop(columns=[pit_rated_col])

    skip_counts: dict = {}
    for code in VALID_SKIP_REASON_CODES:
        cnt = int((skip_reason == code).sum())
        if cnt > 0:
            skip_counts[code] = cnt

    return AdmissionResult(
        admitted=admitted,
        skip_counts=skip_counts,
        total_input_rows=n,
    )


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


def compute_bet_duckdb_window_features(
    bets_df: pd.DataFrame,
    feature_spec: dict,
    cutoff_time: Optional[datetime] = None,
) -> pd.DataFrame:
    """Bet-layer DuckDB window features (layer+method name; legacy Track LLM).

    Thin alias of :func:`compute_bet_layer_features` — same columns and math.
    """
    return compute_bet_layer_features(bets_df, feature_spec, cutoff_time=cutoff_time)


def add_run_state_machine_features(
    bets: pd.DataFrame,
    canonical_map: pd.DataFrame,
    window_end: datetime,
    lookback_hours: Optional[float] = None,
) -> pd.DataFrame:
    """Run-level state-machine features (layer+method name; legacy Track Human).

    Delegates to :func:`trainer.training.feature_pipeline.add_track_human_features`.
    """
    try:
        from trainer.training.feature_pipeline import add_track_human_features as _impl
    except ModuleNotFoundError:  # pragma: no cover — bare trainer/ on sys.path
        from training.feature_pipeline import add_track_human_features as _impl  # type: ignore[import-not-found,no-redef]

    return _impl(bets, canonical_map, window_end, lookback_hours=lookback_hours)


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


def compute_trip_layer_features(
    bets_df: pd.DataFrame,
    *,
    fail_closed: bool = False,
) -> pd.DataFrame:
    """Trip-layer passthrough / contract check (v0 materializer).

    Thin wrapper around :func:`trainer.features.trip_materializer.materialize_trip_layer_features`.
    """
    return _materialize_trip_layer_features(bets_df, fail_closed=fail_closed)


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
            "wrapper": "trainer.features.layered.compute_bet_duckdb_window_features",
            "phase_b_status": "wrapped",
            "layer_method_name": "bet_duckdb_window",
        },
        LAYER_RUN: {
            "module": "trainer.training.feature_pipeline",
            "function": "add_track_human_features",
            "wrapper": "trainer.features.layered.add_run_state_machine_features",
            "phase_b_status": "wrapped",
            "layer_method_name": "run_state_machine",
            "notes": (
                "Run-level state-machine features live in feature_pipeline.py; "
                "trainer/scorer call layered entrypoints for train–serve parity."
            ),
        },
        LAYER_TRIP: {
            "module": "trainer.features.trip_materializer",
            "function": "materialize_trip_layer_features",
            "wrapper": "trainer.features.layered.compute_trip_layer_features",
            "phase_b_status": "wrapped",
            "layer_method_name": "trip_lda_materialized",
            "notes": (
                "Trip-level v1: optional ``lda_*`` bridge columns coerced to float32; "
                "full trip_fact kernel deferred."
            ),
        },
        LAYER_PLAYER: {
            "module": "trainer.features.features",
            "function": "join_player_profile",
            "wrapper": "trainer.features.layered.compute_player_layer_features",
            "phase_b_status": "wrapped",
        },
    }
