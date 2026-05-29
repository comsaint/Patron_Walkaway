"""Feature experimentation utilities (offline gate-1 comparisons)."""

from __future__ import annotations

from typing import Any

__all__ = ["FEATURE_GROUP_TAGS"]


def __getattr__(name: str) -> Any:
    """Lazy re-export so :func:`~feature_registry.set_candidate_registry_path` stays effective."""

    if name == "FEATURE_GROUP_TAGS":
        import trainer_hightier.feature_experiment.feature_registry as _fr

        return _fr.FEATURE_GROUP_TAGS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
