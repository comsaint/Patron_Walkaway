"""Offline Parquet ingress for high-tier trainer (step 1 / skeleton).

Aligns with ``trainer.training.data_sources.LOCAL_PARQUET_DIR`` convention:
default root is ``<repo>/data`` with ``gmwds_t_bet.parquet`` and
``gmwds_t_session.parquet``.

**Pipeline order:** clean ``t_session`` first (canonical session context), then
join or process ``t_bet`` once dependencies exist. Use
:func:`validate_session_ingress_or_raise` for Step 1 before session preprocess;
use :func:`validate_offline_inputs_or_raise` when the pipeline is ready to load
bets (schema-only, no full table read).

Quality checks mirror *intent* of ``load_local_parquet`` /
``trainer.training.feature_pipeline.apply_dq`` but stay schema/metadata-only
so huge files are not scanned into RAM for QC alone.

Helpers :func:`read_parquet_row_groups_to_pandas` and
:func:`preflight_scan_parquet_row_groups` add optional tqdm over row-group
decodes when full reads are needed or progress-only scans are requested.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq


def _tqdm_range(
    seq: range,
    *,
    desc: str,
    unit: str = "row_group",
) -> Any:
    """Wrap *seq* with ``tqdm`` when available; otherwise return *seq* unchanged."""
    try:
        from tqdm import tqdm

        return tqdm(
            seq,
            desc=desc,
            unit=unit,
            mininterval=0.5,
            file=None,
        )
    except ImportError:
        return seq


def tqdm_row_group_range(
    seq: range,
    *,
    desc: str,
    unit: str = "row_group",
) -> Any:
    """Same as :func:`_tqdm_range` — public alias for ``02_preprocess`` batch loops."""
    return _tqdm_range(seq, desc=desc, unit=unit)


def read_parquet_row_groups_to_pandas(
    path: Path,
    *,
    desc: str,
    chunk_filter: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Decode Parquet into one pandas frame, showing a tqdm bar over row groups.

    Row-group iteration bounds decoder peak memory versus a single giant decode
    in some engines; the final ``concat`` still holds the full filtered table in RAM.

    Args:
        path: Parquet file path.
        desc: Progress label (e.g. ``\"[Step 2] t_session\"``).
        chunk_filter: Optional per-chunk transform (e.g. deleted/canceled gate) before concat.
        columns: Optional column projection (Parquet / PyArrow read pushdown); must exist on file.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    pf = pq.ParquetFile(p)
    nrg = int(pf.num_row_groups)
    if nrg <= 0:
        df = pd.read_parquet(p, columns=columns)
        return chunk_filter(df) if chunk_filter is not None else df

    parts: list[pd.DataFrame] = []
    for i in _tqdm_range(range(nrg), desc=desc, unit="row_group"):
        chunk = pf.read_row_group(i, columns=columns, use_threads=True).to_pandas()
        if chunk_filter is not None:
            chunk = chunk_filter(chunk)
        if len(chunk) > 0:
            parts.append(chunk)
    if not parts:
        return pd.DataFrame()
    if len(parts) == 1:
        return parts[0]
    return pd.concat(parts, ignore_index=True)


def preflight_scan_parquet_row_groups(path: Path, *, desc: str) -> None:
    """Decode every row group and discard buffers (progress only; does not retain rows).

    Use for large L0 files when the pipeline does not yet materialise a full
    DataFrame but operators want visible read progress. Adds a full-file read cost.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    pf = pq.ParquetFile(p)
    nrg = int(pf.num_row_groups)
    if nrg <= 0:
        _ = pq.read_table(p)
        return
    for i in _tqdm_range(range(nrg), desc=desc, unit="row_group"):
        _ = pf.read_row_group(i, use_threads=True)


# Mirrors ``trainer.training.data_sources``: column pushdown / contract lists.
from trainer.training.data_sources import _BET_INGEST_READ_COLS_ORDERED

_REQUIRED_BET_PARQUET_COLS: tuple[str, ...] = _BET_INGEST_READ_COLS_ORDERED

_REQUIRED_SESSION_PARQUET_COLS: tuple[str, ...] = (
    "session_id",
    "player_id",
    "casino_player_id",
    "lud_dtm",
    "session_start_dtm",
    "session_end_dtm",
    "is_manual",
    "is_deleted",
    "is_canceled",
    "num_games_with_wager",
    "turnover",
    "player_win",
    "cash_buyins",
    "num_bets",
    "__etl_insert_Dtm",
    "theo_win",
    "gaming_day",
)


def project_root() -> Path:
    """Repository root (``trainer_hightier`` parent)."""
    return Path(__file__).resolve().parents[1]


def default_data_dir() -> Path:
    """Default Parquet folder: ``<repo>/data``."""
    return project_root() / "data"


@dataclass(frozen=True)
class LocalParquetPaths:
    """Resolved paths to offline L0 bet/session Parquets."""

    data_dir: Path

    @property
    def bet_parquet(self) -> Path:
        return self.data_dir / "gmwds_t_bet.parquet"

    @property
    def session_parquet(self) -> Path:
        return self.data_dir / "gmwds_t_session.parquet"


@dataclass(frozen=True)
class ParquetInspectSummary:
    """Cheap Parquet fingerprints (schema + footer row count only)."""

    path: Path
    num_rows: int | None
    column_names: frozenset[str]


@dataclass
class OfflineDataQualityReport:
    """Aggregate QC outcome for both tables."""

    bet: ParquetInspectSummary
    session: ParquetInspectSummary
    missing_required_bet_cols: tuple[str, ...]
    missing_required_session_cols: tuple[str, ...]


@dataclass(frozen=True)
class SessionIngressReport:
    """Metadata-only QC for ``gmwds_t_session`` before ``t_bet`` is touched."""

    session: ParquetInspectSummary
    missing_required_session_cols: tuple[str, ...]


def resolve_local_parquet_paths(data_dir: Path | None = None) -> LocalParquetPaths:
    """Return standard bet/session paths under *data_dir* (default ``default_data_dir()``)."""
    root = Path(data_dir) if data_dir is not None else default_data_dir()
    return LocalParquetPaths(data_dir=root)


def assert_session_parquet_file_exists(paths: LocalParquetPaths) -> None:
    """Raise ``FileNotFoundError`` if ``gmwds_t_session.parquet`` is missing.

    Does not require ``gmwds_t_bet.parquet`` (deferred until after session clean).
    """
    if not paths.session_parquet.is_file():
        raise FileNotFoundError(
            "High-tier session ingress requires "
            f"{paths.session_parquet}"
        )


def assert_local_parquet_files_exist(paths: LocalParquetPaths) -> None:
    """Raise ``FileNotFoundError`` if expected Parquets are absent or not files."""
    missing: list[str] = []
    if not paths.bet_parquet.is_file():
        missing.append(str(paths.bet_parquet))
    if not paths.session_parquet.is_file():
        missing.append(str(paths.session_parquet))
    if missing:
        raise FileNotFoundError(
            "High-tier offline training requires local Parquet exports under "
            f"{paths.data_dir}:\n  - "
            + "\n  - ".join(missing)
        )


def parquet_inspect_summary(path: Path) -> ParquetInspectSummary:
    """Read Parquet schema + row count from metadata only (no full table load)."""
    if not path.is_file():
        raise FileNotFoundError(path)
    pf = pq.ParquetFile(path)
    names = frozenset(str(n) for n in pf.schema_arrow.names)
    meta = pf.metadata
    nrows: int | None = int(meta.num_rows) if meta is not None else None
    return ParquetInspectSummary(path=path.resolve(), num_rows=nrows, column_names=names)


def _partition_ingress_summary(
    paths: tuple[Path, ...],
    *,
    required_cols: tuple[str, ...],
    role_name: str,
) -> ParquetInspectSummary:
    """Validate one partition shard set and return aggregated metadata summary."""
    if not paths:
        raise FileNotFoundError(
            f"Partition ingress requires at least one {role_name} shard "
            f"(expected {role_name}__part_YYYYMM.parquet)."
        )
    ordered = tuple(sorted((Path(p).resolve() for p in paths), key=str))
    for p in ordered:
        if not p.is_file():
            raise FileNotFoundError(p)
    s0 = parquet_inspect_summary(ordered[0])
    missing = tuple(c for c in required_cols if c not in s0.column_names)
    if missing:
        raise ValueError(
            f"Offline {role_name} schema QC failed: first shard missing columns: "
            + ", ".join(missing)
        )
    cols0 = s0.column_names
    total_rows = int(s0.num_rows) if s0.num_rows is not None else 0
    for p in ordered[1:]:
        sn = parquet_inspect_summary(p)
        if sn.column_names != cols0:
            raise ValueError(
                f"Offline {role_name} partition schema mismatch vs first shard {ordered[0].name}: "
                f"extra={sorted(sn.column_names - cols0)!r} "
                f"missing={sorted(cols0 - sn.column_names)!r} in {p.name}"
            )
        if sn.num_rows is not None:
            total_rows += int(sn.num_rows)
    return ParquetInspectSummary(
        path=ordered[0],
        num_rows=total_rows if total_rows > 0 else s0.num_rows,
        column_names=cols0,
    )


def validate_partition_session_ingress_or_raise(
    partition_session_paths: tuple[Path, ...],
) -> SessionIngressReport:
    """Partition-first Step 1 QC for session shards only (no monolith lookup)."""
    summary = _partition_ingress_summary(
        partition_session_paths,
        required_cols=_REQUIRED_SESSION_PARQUET_COLS,
        role_name="t_session",
    )
    return SessionIngressReport(session=summary, missing_required_session_cols=())


def validate_partition_bet_ingress_or_raise(
    partition_bet_paths: tuple[Path, ...],
) -> ParquetInspectSummary:
    """Partition-first QC for bet shards only (schema + metadata, no row scan)."""
    return _partition_ingress_summary(
        partition_bet_paths,
        required_cols=_REQUIRED_BET_PARQUET_COLS,
        role_name="t_bet",
    )


def run_offline_schema_quality_checks_session_first(
    paths: LocalParquetPaths,
) -> SessionIngressReport:
    """Schema + footer row count for ``t_session`` only (no ``t_bet`` read)."""
    assert_session_parquet_file_exists(paths)
    sess_sum = parquet_inspect_summary(paths.session_parquet)
    ms = tuple(c for c in _REQUIRED_SESSION_PARQUET_COLS if c not in sess_sum.column_names)
    return SessionIngressReport(session=sess_sum, missing_required_session_cols=ms)


def validate_session_ingress_or_raise(paths: LocalParquetPaths) -> SessionIngressReport:
    """Step 1 before session preprocess: session file + required columns (metadata only).

    ``gmwds_t_bet`` is intentionally not opened; validate it later with
    :func:`validate_offline_inputs_or_raise` when joining or ingesting bets.
    """
    report = run_offline_schema_quality_checks_session_first(paths)
    if report.missing_required_session_cols:
        raise ValueError(
            "Offline session schema QC failed: gmwds_t_session missing columns: "
            + ", ".join(report.missing_required_session_cols)
        )
    return report


def validate_bet_ingress_or_raise(paths: LocalParquetPaths) -> ParquetInspectSummary:
    """``gmwds_t_bet`` exists with required columns (schema + footer only)."""
    if not paths.bet_parquet.is_file():
        raise FileNotFoundError(f"Bet preprocess requires {paths.bet_parquet}")
    bet_sum = parquet_inspect_summary(paths.bet_parquet)
    mb = tuple(c for c in _REQUIRED_BET_PARQUET_COLS if c not in bet_sum.column_names)
    if mb:
        raise ValueError(
            "Offline bet schema QC failed: gmwds_t_bet missing columns: "
            + ", ".join(mb)
        )
    return bet_sum


def run_offline_schema_quality_checks(paths: LocalParquetPaths) -> OfflineDataQualityReport:
    """Verify required columns exist on both Parquets (schema-only).

    Returns:
        OfflineDataQualityReport with missing-column tuples (empty when OK).

    Note:
        Row-level DQ (windows, wagering, dedup) belongs in ``02_preprocess`` via
        ``trainer.training.feature_pipeline.apply_dq`` once frames are loaded.
    """
    assert_local_parquet_files_exist(paths)
    bet_sum = parquet_inspect_summary(paths.bet_parquet)
    sess_sum = parquet_inspect_summary(paths.session_parquet)

    mb = tuple(c for c in _REQUIRED_BET_PARQUET_COLS if c not in bet_sum.column_names)
    ms = tuple(c for c in _REQUIRED_SESSION_PARQUET_COLS if c not in sess_sum.column_names)
    return OfflineDataQualityReport(
        bet=bet_sum,
        session=sess_sum,
        missing_required_bet_cols=mb,
        missing_required_session_cols=ms,
    )


def validate_offline_inputs_or_raise(paths: LocalParquetPaths) -> OfflineDataQualityReport:
    """Existence + schema checks for **both** ``t_bet`` and ``t_session`` (metadata only).

    Use after the session-clean / canonical-ID leg when ingesting bets, or for
    full offline acceptance tests. For Step 1 before session preprocess alone,
    use :func:`validate_session_ingress_or_raise`.
    """
    report = run_offline_schema_quality_checks(paths)
    if report.missing_required_bet_cols or report.missing_required_session_cols:
        parts: list[str] = []
        if report.missing_required_bet_cols:
            parts.append(
                "gmwds_t_bet missing columns: "
                + ", ".join(report.missing_required_bet_cols)
            )
        if report.missing_required_session_cols:
            parts.append(
                "gmwds_t_session missing columns: "
                + ", ".join(report.missing_required_session_cols)
            )
        raise ValueError("Offline schema QC failed: " + "; ".join(parts))
    return report
