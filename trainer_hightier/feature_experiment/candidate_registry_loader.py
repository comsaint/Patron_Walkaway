"""Training-side re-exports; implementation lives in :mod:`trainer_hightier.serving.candidate_registry_loader`."""

from trainer_hightier.serving.candidate_registry_loader import (
    CandidateRegistrySnapshot,
    FeatureRegistryEntryRow,
    baseline_features_for_main_trainer,
    candidate_features_for_group,
    default_registry_path,
    default_time_horizon_for_row,
    horizon_from_max_lookback_iso8601,
    load_candidate_registry,
    load_registry_raw_feature_dicts,
)

__all__ = [
    "CandidateRegistrySnapshot",
    "FeatureRegistryEntryRow",
    "baseline_features_for_main_trainer",
    "candidate_features_for_group",
    "default_registry_path",
    "default_time_horizon_for_row",
    "horizon_from_max_lookback_iso8601",
    "load_candidate_registry",
    "load_registry_raw_feature_dicts",
]
