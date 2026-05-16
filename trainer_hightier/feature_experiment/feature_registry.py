"""Feature experiment column registry (YAML-backed).

Selections come from ``trainer_hightier/contracts/feature_candidate_registry.yaml``.
Baseline columns are rows with ``status`` in ``{active, experimental}`` and ``baseline`` in
``enabled_for``, in YAML order; see :func:`~trainer_hightier.feature_experiment.candidate_registry_loader.load_candidate_registry`.

Module constants :data:`MODEL_FEATURE_COLUMNS` (baseline), :data:`EXPERIMENTAL_NUMERIC_COLUMNS`, etc.
are provided lazily via :func:`__getattr__` (PEP 562). Call
:func:`set_candidate_registry_path` before reads if CLI needs a non-default YAML path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trainer_hightier.feature_experiment.candidate_registry_loader import (
    CandidateRegistrySnapshot,
    candidate_features_for_group,
    load_candidate_registry,
)

_candidate_registry_snapshot: CandidateRegistrySnapshot | None = None


def candidate_registry_snapshot() -> CandidateRegistrySnapshot:
    """Return the active registry snapshot (lazy default load)."""

    global _candidate_registry_snapshot
    if _candidate_registry_snapshot is None:
        _candidate_registry_snapshot = load_candidate_registry(None)
    return _candidate_registry_snapshot


def set_candidate_registry_path(path: Path | None) -> None:
    """Reload registry from ``path`` (``None`` = default contracts YAML)."""

    global _candidate_registry_snapshot
    _candidate_registry_snapshot = load_candidate_registry(path)


def __getattr__(name: str) -> Any:
    snap = candidate_registry_snapshot()
    if name == "MODEL_FEATURE_COLUMNS":
        return snap.model_feature_columns
    if name == "EXPERIMENTAL_NUMERIC_COLUMNS":
        return snap.experimental_numeric_columns
    if name == "FULL_CANDIDATE_FEATURE_COLUMNS":
        return snap.full_candidate_feature_columns
    if name == "FEATURE_GROUP_TAGS":
        return snap.feature_group_tags
    if name == "ABLATION_EXPERIMENTAL_GROUP_IDS":
        return snap.ablation_experimental_group_ids
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
