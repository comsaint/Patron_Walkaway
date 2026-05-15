"""High-tier patron objective: fixed precision target → report alert rate.

This package is intentionally small vs ``trainer/``. Wire real IO and
segmentation in later steps.

**DuckDB:** :class:`DuckDbRuntimeConfig` is the package-local SSOT for ephemeral
connection PRAGMAs used across this package. Values are **not** read from
``trainer/`` (e.g. ``trainer.core.config.get_duckdb_memory_config``).
Other packages may still *call* shared helpers; only **config** stays local here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

# Repo root (parent of ``trainer_hightier/``); used for default DuckDB spill path only.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Upper bound when auto-doubling ``dedup_hash_buckets`` after DuckDB OOM (semantics-preserving).
PREPROCESS_DEDUP_BUCKET_ESCALATION_CEILING: Final[int] = 256


@dataclass(frozen=True)
class DuckDbRuntimeConfig:
    """PRAGMA defaults for ``duckdb.connect(...)`` in ``trainer_hightier``.

    Use :func:`trainer_hightier.utils.duckdb_runtime.apply_duckdb_runtime_pragmas`
    after opening a connection. Tuning is intentionally string/Path based to
    match DuckDB's PRAGMA surface (e.g. ``memory_limit='4GB'``).
    """

    memory_limit: str = "16GB"
    temp_directory: Path | None = _PROJECT_ROOT / "tmp" / "duckdb_spill"
    threads: int | None = None
    # Cap DuckDB spill files under ``temp_directory`` (or OS default temp drive).
    # Example: ``'200GiB'`` when the default inferred cap is too low for large windows.
    max_temp_directory_size: str | None = None
    # ``false`` reduces temp spill for heavy sorts/windows (DuckDB perf guide); rare
    # FND-01 ties on all ORDER BY keys may pick a different survivor vs insertion order.
    preserve_insertion_order: bool = False


@dataclass(frozen=True)
class SessionPreprocessConfig:
    """L0 ``t_session`` → cleaned Parquet: engine choice and pandas-shard batching only.

    DuckDB memory / threads / spill path: set :class:`DuckDbRuntimeConfig` on
    ``HighTierTrainArgs.duckdb_runtime`` (or pass ``duckdb_runtime=`` into
    :func:`trainer_hightier.02_preprocess.preprocess_sessions_from_parquet_streaming`).
    """

    # ``duckdb``: one ``COPY`` from raw Parquet (DuckDB pipelines).
    # ``pandas_shards``: row-group batches → temp shard Parquets → DuckDB merge.
    engine: str = "duckdb"
    # ``dedup_hash_buckets`` (``engine=\"duckdb\"`` only): split FND-01 ``ROW_NUMBER`` dedup by
    # ``mod(abs(hash(session_id)), N)`` to cap peak RAM on huge tables. ``1`` disables bucketing.
    dedup_hash_buckets: int = 8
    # Only for ``pandas_shards``: concatenate this many row groups per shard file.
    row_groups_per_shard: int = 8


@dataclass(frozen=True)
class BetPreprocessConfig:
    """L0 ``t_bet`` → cleaned Parquet (DQ + registry synthetic observed-at + bulk episode tags).

    Only ``engine=\"duckdb\"`` is implemented (full-table single ``COPY``, like session default).
    ``preprocess_registry_yaml`` defaults to ``<repo>/schema/preprocess_l0_data_contract_registry.yaml`` when omitted.

    ``dedup_hash_buckets``: split ``ROW_NUMBER`` dedup by ``mod(abs(hash(bet_id)), N)`` so each
    bucket processes ~1/N of keys at a time (lower peak RAM on huge tables). ``1`` disables bucketing.

    **ADT patron segment:** when ``adt_filter_quantile`` is set (e.g. ``0.99``), keep only bets whose
    ``player_id`` appears in ``adt_allowed_players_parquet`` (one row per allowed ``player_id``, written
    upstream from ``patron_profile_csv`` + ``canonical_mapping_parquet`` via ADT quantile threshold).
    ``patron_profile_csv`` / ``canonical_mapping_parquet`` remain on the config for DuckDB joins and
    trainer orchestration; the bet disk-cache fingerprint does **not** bind cleaned session stats.
    Paths are normally injected by :func:`trainer_hightier.trainer.prepare_training_frame`.
    """

    engine: str = "duckdb"
    preprocess_registry_yaml: Path | None = None
    dedup_hash_buckets: int = 8
    adt_filter_quantile: float | None = None
    patron_profile_csv: Path | None = None
    canonical_mapping_parquet: Path | None = None
    adt_allowed_players_parquet: Path | None = None


@dataclass(frozen=True)
class CanonicalMappingConfig:
    """``player_id`` → ``canonical_id`` built from cleaned session Parquet (trainer D2).

    When ``cutoff_dtm`` is None, cutoff is inferred as
    ``MAX(session_end_dtm)`` then ``MAX(lud_dtm)`` over the cleaned file (HK-naive
    normalisation after inference).
    """

    enabled: bool = True
    cutoff_dtm: datetime | None = None
    legacy_coalesce_cutoff: bool = False
    # After mapping Parquet is written: aggregate ``theo_win`` / ``gaming_day`` → ADT report.
    compile_patron_session_metrics: bool = True
    # Rich per-canonical profile CSV under ``trainer_hightier/artifacts/profile/``.
    compile_patron_profile_csv: bool = True


@dataclass(frozen=True)
class HighTierObjectiveConfig:
    """Defaults for high-tier segment + precision-floor reporting."""

    # Quantile in (0, 1) on patron **ADT** (from ``canonical_patron_profile.csv``): bet preprocess keeps
    # only bets tied (via canonical mapping) to patrons at or above this ADT quantile (~top ``1 - q``).
    # Align naming with ``trainer.training.high_roller_segmentation`` when wiring segment thresholds.
    theo_train_quantile: float = 0.99
    # Require precision >= this value on the **segment** when choosing a score threshold.
    min_precision: float = 0.80
    # Placeholder paths for later steps (Parquet / DuckDB exports).
    segment_scores_parquet: Path | None = None
    labels_parquet: Path | None = None


@dataclass(frozen=True)
class HighTierRunProfile:
    """One-place preset: DuckDB resource PRAGMAs + preprocess dedup bucket counts.

    Select a profile in code (``DEFAULT_RUN_PROFILE_NAME`` / :func:`get_run_profile`) when wiring runs.
    """

    memory_limit: str
    temp_directory: Path | None
    threads: int | None
    max_temp_directory_size: str | None
    preserve_insertion_order: bool
    session_engine: str
    session_dedup_hash_buckets: int
    row_groups_per_shard: int
    bet_dedup_hash_buckets: int


def _default_paths_temp_dir() -> Path | None:
    return _PROJECT_ROOT / "tmp" / "duckdb_spill"


#: Presets keyed by ``--run-profile`` (see :func:`get_run_profile`).
RUN_PROFILES: Final[dict[str, HighTierRunProfile]] = {
    "default": HighTierRunProfile(
        memory_limit="16GB",
        temp_directory=_default_paths_temp_dir(),
        threads=2,
        max_temp_directory_size=None,
        preserve_insertion_order=False,
        session_engine="duckdb",
        session_dedup_hash_buckets=8,
        row_groups_per_shard=8,
        bet_dedup_hash_buckets=32,
    ),
    "laptop_8g": HighTierRunProfile(
        memory_limit="4GB",
        temp_directory=_default_paths_temp_dir(),
        threads=1,
        max_temp_directory_size="20GiB",
        preserve_insertion_order=False,
        session_engine="duckdb",
        session_dedup_hash_buckets=16,
        row_groups_per_shard=8,
        bet_dedup_hash_buckets=32,
    ),
    # Large union of monthly t_bet shards: heavy ROW_NUMBER dedup; lower threads + more hash
    # buckets reduce per-operator RAM vs ``default`` (see DuckDB perf guide).
    "low_peak_memory": HighTierRunProfile(
        memory_limit="10GB",
        temp_directory=_default_paths_temp_dir(),
        threads=1,
        max_temp_directory_size="250GiB",
        preserve_insertion_order=False,
        session_engine="duckdb",
        session_dedup_hash_buckets=16,
        row_groups_per_shard=8,
        bet_dedup_hash_buckets=32,
    ),
    "workstation_64g": HighTierRunProfile(
        memory_limit="48GB",
        temp_directory=_default_paths_temp_dir(),
        threads=8,
        max_temp_directory_size=None,
        preserve_insertion_order=False,
        session_engine="duckdb",
        session_dedup_hash_buckets=8,
        row_groups_per_shard=8,
        bet_dedup_hash_buckets=8,
    ),
}

DEFAULT_RUN_PROFILE_NAME: Final[str] = "default"


@dataclass(frozen=True)
class Step4SplitConfig:
    """Deterministic train/val/test split on distinct ``gaming_day`` (calendar order).

    Fractions apply to the **ordered distinct** gaming days, not row counts.
    ``test`` receives the remainder after train and val.
    """

    train_day_fraction: float = 0.70
    val_day_fraction: float = 0.15
    #: When ``None``, defaults to ``trainer_hightier/artifacts/training_data/splits``.
    splits_output_dir: Path | None = None


def list_run_profile_names() -> tuple[str, ...]:
    """Sorted :data:`RUN_PROFILES` keys (for validation UX)."""
    return tuple(sorted(RUN_PROFILES))


def get_run_profile(name: str) -> HighTierRunProfile:
    """Return the named :class:`HighTierRunProfile` or raise ``ValueError``."""
    key = str(name).strip()
    if key not in RUN_PROFILES:
        names = ", ".join(list_run_profile_names())
        raise ValueError(f"Unknown run profile {key!r}; expected one of: {names}")
    return RUN_PROFILES[key]


def configs_from_run_profile(
    profile: HighTierRunProfile,
) -> tuple[DuckDbRuntimeConfig, SessionPreprocessConfig, BetPreprocessConfig]:
    """Expand a profile into configs used by :class:`~trainer_hightier.trainer.HighTierTrainArgs`."""
    ddb = DuckDbRuntimeConfig(
        memory_limit=profile.memory_limit,
        temp_directory=profile.temp_directory,
        threads=profile.threads,
        max_temp_directory_size=profile.max_temp_directory_size,
        preserve_insertion_order=profile.preserve_insertion_order,
    )
    sess = SessionPreprocessConfig(
        engine=profile.session_engine,
        dedup_hash_buckets=profile.session_dedup_hash_buckets,
        row_groups_per_shard=profile.row_groups_per_shard,
    )
    bet = BetPreprocessConfig(dedup_hash_buckets=profile.bet_dedup_hash_buckets)
    return ddb, sess, bet
