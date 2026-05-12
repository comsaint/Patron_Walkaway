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
from pathlib import Path


@dataclass(frozen=True)
class DuckDbRuntimeConfig:
    """PRAGMA defaults for ``duckdb.connect(...)`` in ``trainer_hightier``.

    Use :func:`trainer_hightier.utils.duckdb_runtime.apply_duckdb_runtime_pragmas`
    after opening a connection. Tuning is intentionally string/Path based to
    match DuckDB's PRAGMA surface (e.g. ``memory_limit='4GB'``).
    """

    memory_limit: str = "6GB"
    temp_directory: Path | None = None
    threads: int | None = None


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
    # Only for ``pandas_shards``: concatenate this many row groups per shard file.
    row_groups_per_shard: int = 8


@dataclass(frozen=True)
class HighTierObjectiveConfig:
    """Defaults for high-tier segment + precision-floor reporting."""

    # Theo quantile in (0, 1): segment = rated rows with theo >= train quantile cutoff.
    # Align naming with ``trainer.training.high_roller_segmentation`` when wiring data.
    theo_train_quantile: float = 0.90
    # Require precision >= this value on the **segment** when choosing a score threshold.
    min_precision: float = 0.80
    # Placeholder paths for later steps (Parquet / DuckDB exports).
    segment_scores_parquet: Path | None = None
    labels_parquet: Path | None = None
