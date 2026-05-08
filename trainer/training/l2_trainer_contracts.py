"""Frozen L2→trainer consumption contracts (GitHub #16 / impl plan §11).

This module is the **code-side SSOT** for column names and audit keys that
``trainer`` must read when the L2 assembled path becomes the default.  It does
not perform I/O.

See also: ``implementation plan/layered_data_assets_run_trip_implementation_plan.md``
§11, ``ssot/layered_data_assets_run_trip_ssot.md`` §4.3, GitHub issue #17 for
snapshot/manifest governance.
"""

from __future__ import annotations

from typing import Final, FrozenSet, Tuple

# --- Issue #17 read-path (trainer consumes; pipeline publishes) ----------------

MANIFEST_OPTIONAL_TRAINER_KEYS: Final[Tuple[str, ...]] = (
    "snapshot_id",
    "source_snapshot_id",
    "published_at",
    "artifact_root_uri",
    "lineage_upstream_manifest_uris",
)

# Trainer MUST reject ambiguous reads if both exist and disagree (fail-closed).
SNAPSHOT_ID_ALIASES: Final[Tuple[str, ...]] = ("snapshot_id", "source_snapshot_id")

# --- L2 assembled training matrix (minimal; extend in PRs that add readers) ---

L2_ROW_ID_KEYS: Final[Tuple[str, ...]] = ("bet_id",)

# --- label_asset (L2 reusable labels) -----------------------------------------

LABEL_ASSET_REQUIRED_COLUMNS: Final[Tuple[str, ...]] = (
    "bet_id",
    "canonical_id",
    "label",
    "is_censored",
    "label_definition_version",
    "source_snapshot_id",
    "computed_at",
    "coverage_end",
)

LABEL_INVALIDATION_SEMANTIC_KEYS: Final[Tuple[str, ...]] = (
    "label_definition_version",
    "source_snapshot_id",
    "coverage_end",
    "identity_mapping_revision",
    "censoring_policy_id",
)

# --- Train window / split audit (pipeline_diagnostics + model_metadata) --------

TRAIN_END_SOURCE_CHUNK_SPLIT: Final[str] = "chunk_level_train_end"
TRAIN_END_SOURCE_ROW_LEVEL: Final[str] = "row_level_train_max_payout_complete_dtm"
TRAIN_END_SOURCE_L2_MANIFEST: Final[str] = "l2_manifest_train_end"

SPLIT_SAMPLING_CONTRACT_VERSION: Final[str] = "issue16-2026-05-08-post-step7"

# Flags written by trainer when split semantics are asserted (L2 path sets these).
KEY_VALID_FULL_UNSAMPLED: Final[str] = "valid_full_unsampled"
KEY_TEST_FULL_UNSAMPLED: Final[str] = "test_full_unsampled"
KEY_TRAIN_SAMPLING_APPLIED: Final[str] = "train_sampling_applied"
KEY_TRAIN_SAMPLING_SEED: Final[str] = "train_sampling_seed"
KEY_TRAIN_SAMPLING_FRACTION: Final[str] = "train_sampling_fraction"
KEY_L2_SNAPSHOT_ID: Final[str] = "l2_snapshot_id"

REQUIRED_UNSAMPLED_SPLITS: Final[FrozenSet[str]] = frozenset({"valid", "test"})

# --- OOM / Step 7 預估策略（#16 遷移後改 split-bytes） ---------------------------

OOM_ESTIMATE_STRATEGY_CHUNK_LEGACY: Final[str] = "chunk_parquet_concat"
OOM_ESTIMATE_STRATEGY_L2_SPLIT_FILES: Final[str] = "l2_train_valid_test_parquet_bytes"


def label_asset_column_set() -> FrozenSet[str]:
    """Return the frozen set of required ``label_asset`` column names."""
    return frozenset(LABEL_ASSET_REQUIRED_COLUMNS)


def assert_label_asset_columns_present(columns: FrozenSet[str]) -> None:
    """Raise ``ValueError`` if any required label_asset column is missing.

    Args:
        columns: Set of column names present in the parquet / frame header.
    """
    missing = label_asset_column_set() - columns
    if missing:
        raise ValueError(
            "label_asset schema mismatch: missing columns "
            f"{sorted(missing)}; expected at least {sorted(LABEL_ASSET_REQUIRED_COLUMNS)}"
        )
